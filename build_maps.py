#!/usr/bin/env python3
"""
Build high-resolution model map data for a few fixed sites.

Runs inside GitHub Actions (see .github/workflows/maps.yml). For each model it finds the newest
complete run, downloads only the fields we need (byte-range requests using the .idx/.index files),
crops/regrids them around each site, and writes compact data files the web page draws in the browser:

  <out>/<site>.json            manifest: run time, grid bounds, valid times, per-variable files
  <out>/<site>/<MODEL>_<var>.bin   uint16 little-endian, frames x rows(south->north) x cols(west->east)
  <out>/status.json            what was built, what failed

Variables (key: units):  t 2 m temp (F) · d 2 m dew point (F) · tp total precip since run start (in)
                         g surface wind gust (mph) · c total cloud cover (%) · r composite reflectivity (dBZ)

Data: NOAA HRRR / NAM CONUS nest / GFS 0.25 from the NOAA Open Data buckets on AWS (NOMADS as fallback),
ECMWF IFS 0.25 from ECMWF open data (CC-BY-4.0). No API keys.
"""
import argparse, concurrent.futures as cf, datetime as dt, json, math, os, pathlib, re, shutil, sys, time, traceback

import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------------------------
# Sites: keep in sync with SITES in index.html
# --------------------------------------------------------------------------------------------
SITES = {
    "nashville": {"name": "Nashville, TN", "lat": 36.1627, "lon": -86.7816},
    "alamo":     {"name": "Alamo, TN",     "lat": 35.7848, "lon": -89.1170},
    "dresden":   {"name": "Dresden, TN",   "lat": 36.2909, "lon": -88.7081},
}
RADIUS_MI = 75          # data box half-size; the page shows a 50-mile radius
HIRES_STEP = 0.03       # output grid spacing (deg) for the 3 km models (~3 km)

ENC = {  # var: (offset, scale) for uint16 encoding; 65535 = missing
    "t": (-100.0, 0.01), "d": (-100.0, 0.01), "tp": (0.0, 0.001),
    "g": (0.0, 0.01), "c": (0.0, 0.01), "r": (-40.0, 0.01),
}
MISSING = 65535

# idx text after "num:offset:d=date:" -> our var key. First match wins (APCP collects all).
IDX_PATTERNS = [
    ("t",  re.compile(r"^TMP:2 m above ground:")),
    ("d",  re.compile(r"^DPT:2 m above ground:")),
    ("tp", re.compile(r"^APCP:surface:")),
    ("g",  re.compile(r"^GUST:surface:")),
    ("c",  re.compile(r"^TCDC:entire atmosphere")),
    ("r",  re.compile(r"^REFC:entire atmosphere")),
]

NOAA_MODELS = {
    "HRRR": {
        "cycles": [0, 6, 12, 18], "fhours": list(range(0, 49)), "hires": True,
        "urls": [
            "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{ymd}/conus/hrrr.t{hh}z.wrfsfcf{fh:02d}.grib2",
            "https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/hrrr.{ymd}/conus/hrrr.t{hh}z.wrfsfcf{fh:02d}.grib2",
        ],
    },
    "NAM": {
        "cycles": [0, 6, 12, 18], "fhours": list(range(0, 61)), "hires": True,
        "urls": [
            "https://noaa-nam-pds.s3.amazonaws.com/nam.{ymd}/nam.t{hh}z.conusnest.hiresf{fh:02d}.tm00.grib2",
            "https://nomads.ncep.noaa.gov/pub/data/nccf/com/nam/prod/nam.{ymd}/nam.t{hh}z.conusnest.hiresf{fh:02d}.tm00.grib2",
        ],
    },
    "GFS": {
        "cycles": [0, 6, 12, 18], "fhours": list(range(0, 121, 3)), "hires": False,
        "urls": [
            "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{ymd}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f{fh:03d}",
            "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/gfs.{ymd}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f{fh:03d}",
        ],
    },
}
ECMWF_STEPS = list(range(0, 121, 3))
ECMWF_PARAMS = {"2t": "t", "2d": "d", "tp": "tp", "tcc": "c", "10fg": "g"}

# --------------------------------------------------------------------------------------------
def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

_session = None
def session():
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(total=4, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET", "HEAD"])
        s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=32))
        s.headers["User-Agent"] = "weather-dashboard-maps (github actions)"
        _session = s
    return _session

def http_get(url, byte_range=None, timeout=60):
    """Return bytes, or None on 404/403. Raises on other errors. Tests replace this function."""
    headers = {"Range": f"bytes={byte_range[0]}-{'' if byte_range[1] is None else byte_range[1]}"} if byte_range else {}
    r = session().get(url, headers=headers, timeout=timeout)
    if r.status_code in (403, 404):
        return None
    r.raise_for_status()
    return r.content

def site_box(site, mi=RADIUS_MI):
    dlat = mi / 69.0
    dlon = mi / (69.0 * math.cos(math.radians(site["lat"])))
    return {"s": site["lat"] - dlat, "n": site["lat"] + dlat, "w": site["lon"] - dlon, "e": site["lon"] + dlon}

def to_units(var, v):
    if var in ("t", "d"):
        return (v - 273.15) * 9.0 / 5.0 + 32.0
    if var == "tp":
        return v / 25.4            # kg m-2 == mm -> inches
    if var == "g":
        return v * 2.236936        # m/s -> mph
    return v

def encode(var, arr):
    off, sc = ENC[var]
    a = np.asarray(arr, dtype=np.float64)
    q = np.round((a - off) / sc)
    q = np.where(np.isfinite(q), np.clip(q, 0, MISSING - 1), MISSING)
    return q.astype("<u2")

# --------------------------------------------------------------------------------------------
# idx parsing
# --------------------------------------------------------------------------------------------
ACC_RE = re.compile(r"(\d+)-(\d+) (hour|day) acc")

def parse_idx(text):
    """-> list of (offset, end_or_None, desc) where desc = 'VAR:LEVEL:fcst:'"""
    rows = []
    for line in text.strip().splitlines():
        p = line.split(":")
        if len(p) < 5:
            continue
        try:
            rows.append([int(p[1]), ":".join(p[3:])])
        except ValueError:
            continue
    out = []
    for i, (off, desc) in enumerate(rows):
        end = rows[i + 1][0] - 1 if i + 1 < len(rows) else None
        out.append((off, end, desc))
    return out

def select_records(idx_rows):
    """-> list of (var, offset, end, desc). One per var except tp (all APCP)."""
    got, out = set(), []
    for off, end, desc in idx_rows:
        for var, pat in IDX_PATTERNS:
            if pat.match(desc):
                if var == "tp" or var not in got:
                    out.append((var, off, end, desc))
                    got.add(var)
                break
    return out

def acc_range(desc):
    m = ACC_RE.search(desc)
    if not m:
        return None
    a, b, unit = int(m.group(1)), int(m.group(2)), m.group(3)
    k = 24 if unit == "day" else 1
    return a * k, b * k

# --------------------------------------------------------------------------------------------
# GRIB decode + spatial mapping
# --------------------------------------------------------------------------------------------
import threading
_grib_lock = threading.Lock()   # pygrib/ecCodes is not thread-safe: downloads run in parallel, decoding doesn't

def decode(grib_bytes, want_latlon=False):
    with _grib_lock:
        return _decode(grib_bytes, want_latlon)

def _decode(grib_bytes, want_latlon=False):
    import pygrib
    msg = pygrib.fromstring(grib_bytes)
    vals = msg.values
    vals = np.ma.filled(vals.astype(np.float64), np.nan) if np.ma.isMaskedArray(vals) else np.asarray(vals, dtype=np.float64)
    if want_latlon:
        lats, lons = msg.latlons()
        lons = ((np.asarray(lons) + 180.0) % 360.0) - 180.0
        return vals, np.asarray(lats), lons
    return vals, None, None

class RegularCrop:
    """For regular lat/lon grids (GFS, ECMWF): crop native points around the site, south->north."""
    def __init__(self, lats, lons, box, margin=0.3):
        lat1 = lats[:, 0] if lats.ndim == 2 else lats
        lon1 = lons[0, :] if lons.ndim == 2 else lons
        rows = np.where((lat1 >= box["s"] - margin) & (lat1 <= box["n"] + margin))[0]
        cols = np.where((lon1 >= box["w"] - margin) & (lon1 <= box["e"] + margin))[0]
        if len(rows) < 2 or len(cols) < 2:
            raise ValueError("site outside model grid")
        rows = rows[np.argsort(lat1[rows])]
        cols = cols[np.argsort(lon1[cols])]
        self.rows, self.cols = rows, cols
        self.bounds = {"s": float(lat1[rows[0]]), "n": float(lat1[rows[-1]]), "w": float(lon1[cols[0]]), "e": float(lon1[cols[-1]])}
        self.ny, self.nx = len(rows), len(cols)
    def __call__(self, vals):
        return vals[np.ix_(self.rows, self.cols)]

class HiresRegrid:
    """For projected grids (HRRR, NAM Lambert): linear interpolation onto a regular lat/lon grid."""
    def __init__(self, lats, lons, box, step=HIRES_STEP, margin=0.2):
        from scipy.spatial import Delaunay
        m = (lats >= box["s"] - margin) & (lats <= box["n"] + margin) & (lons >= box["w"] - margin) & (lons <= box["e"] + margin)
        if m.sum() < 16:
            raise ValueError("site outside model grid")
        self.mask = m
        self.tri = Delaunay(np.c_[lons[m], lats[m]])
        ty = np.arange(box["s"], box["n"] + step / 2, step)
        tx = np.arange(box["w"], box["e"] + step / 2, step)
        self.ny, self.nx = len(ty), len(tx)
        X, Y = np.meshgrid(tx, ty)
        self.targets = np.c_[X.ravel(), Y.ravel()]
        self.bounds = {"s": float(ty[0]), "n": float(ty[-1]), "w": float(tx[0]), "e": float(tx[-1])}
    def __call__(self, vals):
        from scipy.interpolate import LinearNDInterpolator
        f = LinearNDInterpolator(self.tri, vals[self.mask])
        return f(self.targets).reshape(self.ny, self.nx)

def make_mappers(lats, lons, hires):
    mappers = {}
    for key, site in SITES.items():
        box = site_box(site)
        try:
            mappers[key] = HiresRegrid(lats, lons, box) if hires else RegularCrop(lats, lons, box)
        except Exception as e:
            log(f"  {key}: no coverage ({e})")
    return mappers

# --------------------------------------------------------------------------------------------
# NOAA models
# --------------------------------------------------------------------------------------------
def find_noaa_run(name, cfg, now):
    base = now.replace(minute=0, second=0, microsecond=0)
    base -= dt.timedelta(hours=base.hour % 6)
    last_fh = cfg["fhours"][-1]
    for k in range(0, 10):
        t = base - dt.timedelta(hours=6 * k)
        if t.hour not in cfg["cycles"]:
            continue
        for tmpl in cfg["urls"]:
            url = tmpl.format(ymd=t.strftime("%Y%m%d"), hh=f"{t.hour:02d}", fh=last_fh) + ".idx"
            try:
                if http_get(url, timeout=30) is not None:
                    log(f"{name}: newest complete run {t:%Y-%m-%d %HZ} via {url.split('/')[2]}")
                    return t, tmpl
            except Exception as e:
                log(f"{name}: check failed {url}: {e}")
    return None, None

def fetch_noaa_frame(tmpl, run, fh):
    url = tmpl.format(ymd=run.strftime("%Y%m%d"), hh=f"{run.hour:02d}", fh=fh)
    idx = http_get(url + ".idx")
    if idx is None:
        raise FileNotFoundError(url + ".idx")
    recs = select_records(parse_idx(idx.decode("utf-8", "replace")))
    out = []
    for var, off, end, desc in recs:
        data = http_get(url, (off, end))
        if data is None:
            continue
        out.append((var, desc, data))
    return fh, out

def build_noaa(name, cfg, now):
    run, tmpl = find_noaa_run(name, cfg, now)
    if run is None:
        raise RuntimeError("no complete run found")
    return run, (lambda: process_noaa(name, cfg, run, tmpl))

def process_noaa(name, cfg, run, tmpl):
    fhours = cfg["fhours"]
    # grid geometry from the first frame's temperature record
    _, recs0 = fetch_noaa_frame(tmpl, run, fhours[0])
    first = next((r for r in recs0 if r[0] == "t"), None)
    if first is None:
        raise RuntimeError("no TMP record in first frame")
    _, lats, lons = decode(first[2], want_latlon=True)
    mappers = make_mappers(lats, lons, cfg["hires"])
    if not mappers:
        raise RuntimeError("no site inside grid")
    log(f"{name}: grid ready; sites {list(mappers)}; downloading {len(fhours)} frames")

    frames = {}  # fh -> {var: {site: arr}} ; tp -> list of (a,b,{site:arr})
    def work(fh):
        _, recs = (fhours[0], recs0) if fh == fhours[0] else fetch_noaa_frame(tmpl, run, fh)
        res = {"tp": []}
        for var, desc, data in recs:
            vals, _, _ = decode(data)
            per_site = {k: to_units(var, mp(vals)) for k, mp in mappers.items()}
            if var == "tp":
                rng = acc_range(desc)
                if rng:
                    res["tp"].append((rng[0], rng[1], per_site))
            else:
                res[var] = per_site
        return fh, res

    errors = 0
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(work, fh) for fh in fhours]
        for f in cf.as_completed(futs):
            try:
                fh, res = f.result()
                frames[fh] = res
            except Exception as e:
                errors += 1
                log(f"{name}: frame failed: {e}")
    if not frames:
        raise RuntimeError("all frames failed")
    log(f"{name}: {len(frames)} frames ok, {errors} failed")
    return assemble(name, run, mappers, frames, sorted(frames))

def precip_totals(frames, fhs, site):
    """Total precip since run start at each frame, from whatever accumulation buckets the model provides."""
    totals = {0: 0.0}
    out = []
    for fh in fhs:
        if fh == 0:
            out.append(np.zeros(1)); continue
        best = None
        recs = frames[fh].get("tp", [])
        for a, b, per in recs:              # prefer a run-total record
            if b == fh and a == 0:
                best = per[site]; break
        if best is None:
            for a, b, per in recs:
                if b == fh and a in totals and totals[a] is not None:
                    best = totals[a] + per[site]; break
        totals[fh] = best
        out.append(best)
    return out

def assemble(name, run, mappers, frames, fhs):
    """-> {site: {meta, data:{var: ndarray(frames, ny, nx)}}}"""
    result = {}
    for key, mp in mappers.items():
        shape = (mp.ny, mp.nx)
        data = {}
        for var in ("t", "d", "g", "c", "r"):
            if not any(var in frames[fh] for fh in fhs):
                continue
            data[var] = np.stack([frames[fh][var][key] if var in frames[fh] else np.full(shape, np.nan) for fh in fhs])
        tps = precip_totals(frames, fhs, key)
        if any(x is not None and x.size > 1 for x in tps):
            data["tp"] = np.stack([np.zeros(shape) if (x is not None and x.size == 1) else (x if x is not None else np.full(shape, np.nan)) for x in tps])
        times = [(run + dt.timedelta(hours=fh)).strftime("%Y-%m-%dT%H:00Z") for fh in fhs]
        steps = np.diff(fhs)
        result[key] = {"run": run.strftime("%Y-%m-%dT%H:00Z"), "bounds": mp.bounds, "nx": mp.nx, "ny": mp.ny,
                       "times": times, "step": int(steps.min()) if len(steps) else 1, "data": data}
    return result

# --------------------------------------------------------------------------------------------
# ECMWF open data
# --------------------------------------------------------------------------------------------
def build_ecmwf(now):
    from ecmwf.opendata import Client
    last_err = None
    for source in ("aws", "ecmwf", "google"):
        try:
            client = Client(source=source, maximum_retries=3, retry_after=15)   # library default retries for hours
            run = client.latest(type="fc", step=ECMWF_STEPS[-1], param="2t")
            run = run.replace(tzinfo=None)
            log(f"ECMWF: newest run {run:%Y-%m-%d %HZ} via {source}")
            return run, (lambda c=client, r=run: process_ecmwf(c, r))
        except Exception as e:
            last_err = e
            log(f"ECMWF: source {source} failed: {e}")
    raise RuntimeError(f"ECMWF unavailable: {last_err}")

def process_ecmwf(client, run):
    import pygrib
    tmp = pathlib.Path("ecmwf_tmp"); tmp.mkdir(exist_ok=True)
    per_param = {}
    for param, var in ECMWF_PARAMS.items():
        target = tmp / f"{param}.grib2"
        try:
            client.retrieve(date=run.strftime("%Y%m%d"), time=run.hour, type="fc", step=ECMWF_STEPS, param=param, target=str(target))
            per_param[param] = target
        except Exception as e:
            log(f"ECMWF: {param} unavailable ({e})")
    if "2t" not in per_param:
        raise RuntimeError("ECMWF 2t download failed")
    grbs = pygrib.open(str(per_param["2t"]))
    m0 = grbs.message(1)
    lats, lons = m0.latlons(); lons = ((lons + 180.0) % 360.0) - 180.0
    grbs.close()
    mappers = make_mappers(lats, lons, hires=False)
    frames = {fh: {"tp": []} for fh in ECMWF_STEPS}
    for param, path in per_param.items():
        var = ECMWF_PARAMS[param]
        for msg in pygrib.open(str(path)):
            fh = int(msg.endStep)
            if fh not in frames:
                continue
            vals = msg.values
            vals = np.ma.filled(vals.astype(np.float64), np.nan) if np.ma.isMaskedArray(vals) else np.asarray(vals, dtype=np.float64)
            if var == "t" or var == "d":
                per = {k: to_units(var, mp(vals)) for k, mp in mappers.items()}
            elif var == "tp":
                per = {k: mp(vals) * 39.3701 for k, mp in mappers.items()}       # metres -> inches
            elif var == "c":
                per = {k: (mp(vals) * (100.0 if np.nanmax(vals) <= 1.01 else 1.0)) for k, mp in mappers.items()}
            else:  # gust m/s
                per = {k: to_units("g", mp(vals)) for k, mp in mappers.items()}
            if var == "tp":
                frames[fh]["tp"].append((0, fh, per))
            else:
                frames[fh][var] = per
    shutil.rmtree(tmp, ignore_errors=True)
    fhs = [fh for fh in ECMWF_STEPS if "t" in frames[fh]]
    return assemble("ECMWF", run, mappers, frames, fhs)

# --------------------------------------------------------------------------------------------
# output + cache
# --------------------------------------------------------------------------------------------
def write_model(result, model, outdir):
    """Write bins; return {site: manifest entry}."""
    entries = {}
    for site, r in result.items():
        (outdir / site).mkdir(parents=True, exist_ok=True)
        vars_meta = {}
        for var, arr in r["data"].items():
            fn = f"{site}/{model}_{var}.bin"
            (outdir / fn).write_bytes(encode(var, arr).tobytes())
            off, sc = ENC[var]
            vars_meta[var] = {"file": fn, "offset": off, "scale": sc}
        entries[site] = {k: r[k] for k in ("run", "bounds", "nx", "ny", "times", "step")}
        entries[site]["vars"] = vars_meta
    return entries

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_site/data")
    ap.add_argument("--cache", default=".mapcache")
    ap.add_argument("--models", default="HRRR,NAM,GFS,ECMWF")
    args = ap.parse_args(argv)
    out = pathlib.Path(args.out); out.mkdir(parents=True, exist_ok=True)
    cache = pathlib.Path(args.cache); cache.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    manifest = {k: {"site": k, **v, "generated": now.strftime("%Y-%m-%dT%H:%MZ"), "models": {}} for k, v in SITES.items()}
    status = {"generated": now.strftime("%Y-%m-%dT%H:%MZ"), "models": {}}

    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        mcache = cache / model
        meta_path = mcache / "meta.json"
        cached = json.loads(meta_path.read_text()) if meta_path.exists() else None
        t0 = time.time()
        try:
            run, job = build_ecmwf(now) if model == "ECMWF" else build_noaa(model, NOAA_MODELS[model], now)
            run_s = run.strftime("%Y-%m-%dT%H:00Z")
            if cached and cached.get("run") == run_s:
                log(f"{model}: run {run_s} unchanged, reusing cache")
            else:
                result = job()
                tmp = cache / f"{model}.new"
                shutil.rmtree(tmp, ignore_errors=True)
                entries = write_model(result, model, tmp / "files")
                (tmp / "meta.json").write_text(json.dumps({"run": run_s, "entries": entries}))
                shutil.rmtree(mcache, ignore_errors=True)
                tmp.rename(mcache)
                cached = json.loads(meta_path.read_text())
                log(f"{model}: built in {time.time() - t0:.0f}s")
            status["models"][model] = {"run": run_s, "ok": True}
        except Exception as e:
            traceback.print_exc()
            status["models"][model] = {"ok": False, "error": str(e)[:300],
                                       "run": cached.get("run") if cached else None, "stale": bool(cached)}
            log(f"{model}: FAILED ({e}){' - keeping previous run ' + cached['run'] if cached else ''}")
        if cached:
            src = mcache / "files"
            if src.exists():
                shutil.copytree(src, out, dirs_exist_ok=True)
            for site, entry in cached["entries"].items():
                if site in manifest:
                    manifest[site]["models"][model] = entry

    for site, m in manifest.items():
        (out / f"{site}.json").write_text(json.dumps(m, separators=(",", ":")))
    (out / "status.json").write_text(json.dumps(status, indent=1))
    log("status:", json.dumps(status))
    return 0

if __name__ == "__main__":
    sys.exit(main())
