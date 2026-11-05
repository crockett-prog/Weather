#!/usr/bin/env python3
"""
Pull rain from a Smart Life (Tuya) weather station for the page's 48-hour rain gauge.

Runs in the GitHub Action. Credentials come from repo secrets and are never printed:
  TUYA_ACCESS_ID, TUYA_ACCESS_SECRET   (Tuya developer cloud project -> Overview)
  TUYA_DEVICE_ID                       (the weather station's Device ID)
  TUYA_REGION                          us (default) | us-east | eu | eu-west | cn | in

Writes <out>/smartlife.json:
  {station, name, updated, hours:[48 inches, index 0 = latest hour], total24, total48,
   today, rate, rain_code, reported_hours, readings:{code: value}}

Every run also prints the station's data points (codes, values, units) to the Action log,
so the rain code can be checked or pinned with TUYA_RAIN_CODE if the automatic pick is wrong.
"""
import hashlib, hmac, json, os, pathlib, sys, time, urllib.error, urllib.parse, urllib.request, uuid

HOSTS = {
    "us": "https://openapi.tuyaus.com", "us-east": "https://openapi-ueaz.tuyaus.com",
    "eu": "https://openapi.tuyaeu.com", "eu-west": "https://openapi-weaz.tuyaeu.com",
    "cn": "https://openapi.tuyacn.com", "in": "https://openapi.tuyain.com",
}
HOUR = 3600 * 1000

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

class Tuya:
    def __init__(self, cid, secret, host):
        self.cid, self.secret, self.host, self.token = cid, secret, host, ""

    def _sign(self, method, path_query, t, nonce, body=b"", with_token=True):
        content_sha = hashlib.sha256(body).hexdigest()
        string_to_sign = "\n".join([method, content_sha, "", path_query])
        msg = self.cid + (self.token if with_token else "") + t + nonce + string_to_sign
        return hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest().upper()

    def get(self, path, params=None, with_token=True):
        q = ""
        if params:
            q = "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))   # Tuya signs the sorted query
        t, nonce = str(int(time.time() * 1000)), uuid.uuid4().hex
        headers = {"client_id": self.cid, "t": t, "nonce": nonce, "sign_method": "HMAC-SHA256",
                   "sign": self._sign("GET", path + q, t, nonce, with_token=with_token)}
        if with_token:
            headers["access_token"] = self.token
        req = urllib.request.Request(self.host + path + q, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode("utf-8"))
        if not j.get("success"):
            raise RuntimeError(f"Tuya error {j.get('code')}: {j.get('msg')}")
        return j.get("result")

    def login(self):
        self.token = self.get("/v1.0/token", {"grant_type": "1"}, with_token=False)["access_token"]

def scale_of(spec):
    try:
        v = json.loads(spec.get("values") or "{}")
        return int(v.get("scale", 0)), (v.get("unit") or "").strip()
    except Exception:
        return 0, ""

def to_inches(raw, scale, unit):
    try:
        v = float(raw) / (10 ** scale)
    except (TypeError, ValueError):
        return None
    u = unit.lower()
    if u in ("mm", "mm/h", "mm/hr") or u.startswith("mm"):
        return v / 25.4
    if u.startswith("cm"):
        return v / 2.54
    return v   # "in", "inch", or unknown -> assume inches

def pick_rain_codes(specs):
    """-> (counter_code, counter_kind, rate_code, today_code). kind: total | daily | rolling | hourly"""
    def label(c):
        return (c + " " + (specs[c].get("name") or "")).lower()
    codes = [c for c in specs if ("rain" in label(c) or "precip" in label(c)) and "convert" not in c.lower() and "unit" not in c.lower()]
    def find(*words):
        for c in codes:
            lc = label(c)
            if any(w in lc for w in words):
                return c
        return None
    total = find("total", "accum", "cumul", "year")
    daily = find("daily", "today", "_day", "day_")
    rolling = find("24h", "24 h", "24hr", "last24")
    hourly = find("1h", "hour")
    rate = find("rate", "intensity")
    if os.environ.get("TUYA_RAIN_CODE"):
        forced = os.environ["TUYA_RAIN_CODE"].strip()
        kind = os.environ.get("TUYA_RAIN_KIND", "total").strip()
        return forced, kind, rate, daily
    if total: return total, "total", rate, daily
    if daily: return daily, "daily", rate, daily
    if rolling: return rolling, "rolling", rate, daily
    if hourly: return hourly, "hourly", rate, daily
    other = [c for c in codes if c != rate]
    if other:   # a rain data point with no total/daily/hourly hint: treat as a running counter (resets are handled)
        log(f"Rain data point '{other[0]}' has no total/daily/hourly hint in its name; treating it as a counter")
        return other[0], "total", rate, daily
    return None, None, rate, daily

def report_logs(api, dev, code, start, end):
    out, key = [], ""
    for _ in range(50):
        params = {"codes": code, "start_time": str(start), "end_time": str(end), "size": "100"}
        if key:
            params["last_row_key"] = key
        res = api.get(f"/v2.0/cloud/thing/{dev}/report-logs", params) or {}
        out += res.get("logs") or []
        key = res.get("last_row_key") or res.get("lastRowKey") or ""
        if not res.get("has_more") or not key:
            break
    return sorted(out, key=lambda x: int(x.get("event_time") or 0))

def hours_from_counter(logs, baseline, kind, now, scale, unit):
    """Counter devices usually report only when the value changes, so an hour with no report = no rain."""
    slots = [0.0] * 48
    prev = baseline
    for e in logs:
        v = to_inches(e.get("value"), scale, unit)
        ts = int(e.get("event_time") or 0)
        if v is None:
            continue
        slot = int((now - ts) // HOUR)
        if kind == "hourly":
            if 0 <= slot < 48:
                slots[slot] = max(slots[slot], v)
            continue
        if prev is None:
            inc = 0.0
        elif v < prev - 1e-6:
            # daily counter: dropped at midnight -> the new value is new rain
            # rolling 24 h total: old rain aged out -> no new rain
            inc = 0.0 if kind == "rolling" else v
        else:
            inc = v - prev
        prev = v
        if 0 <= slot < 48:
            slots[slot] += max(0.0, inc)
    return [round(x, 3) for x in slots]

# ---------- turn the station's data points into page-ready current conditions (imperial) ----------
SKIP = ("unit", "convert", "alarm", "max", "min", "set", "calib", "switch", "mode", "enum", "battery", "signal")
def _find(readings, must, prefer=(), avoid=()):
    best = None
    for c, r in readings.items():
        lab = (c + " " + (r.get("name") or "")).lower()
        if not all(any(w in lab for w in grp) for grp in must):
            continue
        if any(w in lab for w in SKIP + tuple(avoid)):
            continue
        score = sum(w in lab for w in prefer)
        if best is None or score > best[0]:
            best = (score, c, r)
    return best[2] if best else None

def _num(r):
    try:
        return float(r["value"])
    except (TypeError, ValueError, KeyError):
        return None

DIRS = {"n":0,"nne":22.5,"ne":45,"ene":67.5,"e":90,"ese":112.5,"se":135,"sse":157.5,"s":180,"ssw":202.5,"sw":225,
        "wsw":247.5,"w":270,"wnw":292.5,"nw":315,"nnw":337.5,"north":0,"east":90,"south":180,"west":270,
        "north_east":45,"south_east":135,"south_west":225,"north_west":315}

def normalize_current(readings):
    cur = {}
    outdoor = ("out", "external", "ext", "outdoor")
    t = _find(readings, [("temp",)], prefer=outdoor, avoid=("feel", "dew", "heat", "chill", "indoor", "inner", "_in_"))
    if t and _num(t) is not None:
        v, u = _num(t), (t.get("unit") or "").lower()
        cur["temp_f"] = round(v if ("f" in u or "℉" in u) else v * 9 / 5 + 32, 1)
    h = _find(readings, [("humid",)], prefer=outdoor, avoid=("indoor", "inner", "_in_"))
    if h and _num(h) is not None:
        cur["humidity"] = round(_num(h))
    w = _find(readings, [("wind",), ("speed", "spd")], avoid=("gust", "dir", "max"))
    def mph(r):
        v, u = _num(r), (r.get("unit") or "").lower().replace(" ", "")
        if v is None:
            return None
        if "km" in u: return v * 0.621371
        if "mph" in u or "mi" in u: return v
        if "kn" in u: return v * 1.15078
        return v * 2.23694                    # m/s (Tuya's usual unit)
    if w and mph(w) is not None:
        cur["wind_mph"] = round(mph(w), 1)
    g = _find(readings, [("gust",)])
    if g and mph(g) is not None:
        cur["gust_mph"] = round(mph(g), 1)
    d = _find(readings, [("wind",), ("dir", "direct")])
    if d:
        v = d.get("value")
        if isinstance(v, (int, float)):
            cur["wind_dir_deg"] = float(v)
        elif isinstance(v, str) and v.lower().replace(" ", "_") in DIRS:
            cur["wind_dir_deg"] = DIRS[v.lower().replace(" ", "_")]
    p = _find(readings, [("pressure", "baro", "atmos")], prefer=("rel", "sea"))
    if p and _num(p) is not None:
        v, u = _num(p), (p.get("unit") or "").lower()
        if "inhg" in u: cur["pressure_inhg"] = round(v, 2)
        elif "kpa" in u: cur["pressure_inhg"] = round(v * 0.2953, 2)
        elif "mmhg" in u: cur["pressure_inhg"] = round(v * 0.03937, 2)
        elif u == "pa": cur["pressure_inhg"] = round(v * 0.0002953, 2)
        else: cur["pressure_inhg"] = round(v * 0.02953, 2)        # hPa / mbar
    uv = _find(readings, [("uv",)])
    if uv and _num(uv) is not None:
        cur["uv"] = _num(uv)
    if "temp_f" in cur and "humidity" in cur and cur["humidity"] > 0:
        import math
        tc = (cur["temp_f"] - 32) * 5 / 9
        a, b = 17.62, 243.12
        gm = math.log(cur["humidity"] / 100) + a * tc / (b + tc)
        cur["dewpoint_f"] = round((b * gm / (a - gm)) * 9 / 5 + 32, 1)
    return cur

def main():
    out = pathlib.Path(sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else "_site/data/pws")
    cid, secret, dev = (os.environ.get(k, "").strip() for k in ("TUYA_ACCESS_ID", "TUYA_ACCESS_SECRET", "TUYA_DEVICE_ID"))
    if not (cid and secret and dev):
        log("Smart Life secrets not set (TUYA_ACCESS_ID / TUYA_ACCESS_SECRET / TUYA_DEVICE_ID); skipping")
        return 0
    host = HOSTS.get(os.environ.get("TUYA_REGION", "us").strip().lower() or "us", HOSTS["us"])
    api = Tuya(cid, secret, host)
    try:
        api.login()
        info = api.get(f"/v1.0/devices/{dev}") or {}
        status = {s["code"]: s.get("value") for s in (api.get(f"/v1.0/devices/{dev}/status") or [])}
        specs = {}
        try:
            for s in (api.get(f"/v1.0/devices/{dev}/specifications") or {}).get("status") or []:
                specs[s["code"]] = s
        except Exception as e:
            log(f"specifications unavailable ({e})")
        # The basic status call only returns "standard instruction" data points. Weather stations often keep
        # their sensors (rain, temperature, wind) as custom data points, so also read the device's thing model
        # and shadow properties, which list every data point.
        names = {}
        obs_time = 0
        try:
            model = api.get(f"/v2.0/cloud/thing/{dev}/model") or {}
            m = json.loads(model.get("model") or "{}")
            for svc in m.get("services") or []:
                for prop in svc.get("properties") or []:
                    c = prop.get("code"); ts = prop.get("typeSpec") or {}
                    if not c:
                        continue
                    names[c] = prop.get("name") or ""
                    if c not in specs or specs[c].get("values") in (None, "{}"):
                        specs[c] = {"code": c, "values": json.dumps({"unit": ts.get("unit", ""), "scale": ts.get("scale", 0)})}
        except Exception as e:
            log(f"thing model unavailable ({str(e)[:120]})")
        try:
            sh = api.get(f"/v2.0/cloud/thing/{dev}/shadow/properties") or {}
            for prop in sh.get("properties") or []:
                c = prop.get("code")
                try:
                    obs_time = max(obs_time, int(prop.get("time") or 0))
                except (TypeError, ValueError):
                    pass
                if c and c not in status:
                    status[c] = prop.get("value")
                if c and prop.get("custom_name"):
                    names.setdefault(c, prop.get("custom_name"))
                if c and prop.get("dp_id") is not None:
                    names[c] = (names.get(c) or "") + f" (dp {prop.get('dp_id')})"
        except Exception as e:
            log(f"shadow properties unavailable ({str(e)[:120]})")
        for c in status:
            specs.setdefault(c, {"code": c, "values": "{}"})
        for c, n in names.items():
            if c in specs:
                specs[c]["name"] = n

        log(f"Station: {info.get('name')!r}, online={info.get('online')}")
        log("Data points reported by the station (code = value [unit, scale]):")
        for c, v in status.items():
            sc, un = scale_of(specs.get(c, {}))
            nm = specs.get(c, {}).get("name") or ""
            log(f"   {c} = {v}  [{un or '-'}, scale {sc}]{'  ' + nm if nm else ''}")

        now = int(time.time() * 1000)
        def val(c):
            if not c or c not in status:
                return None
            s2, u2 = scale_of(specs.get(c, {}))
            x = to_inches(status[c], s2, u2)
            return None if x is None else round(x, 3)

        # ---- rain (48 h history) ----
        code, kind, rate_code, today_code = pick_rain_codes(specs)
        hours = None
        if not code:
            log("No rain data point found. If one of the codes above is rain, set repo secret TUYA_RAIN_CODE to it "
                "(and TUYA_RAIN_KIND to total, daily or hourly). Publishing the other readings anyway.")
        else:
            sc, un = scale_of(specs.get(code, {}))
            log(f"Using rain code '{code}' as a {kind} counter ({un or 'unit unknown'}, scale {sc})")
            logs = report_logs(api, dev, code, now - 74 * HOUR, now)
            before = [e for e in logs if int(e["event_time"]) < now - 48 * HOUR]
            inside = [e for e in logs if int(e["event_time"]) >= now - 48 * HOUR]
            baseline = to_inches(before[-1]["value"], sc, un) if before else None
            if baseline is None and inside:
                baseline = to_inches(inside[0]["value"], sc, un); inside = inside[1:]
            if baseline is None:
                baseline = to_inches(status.get(code), sc, un)     # nothing reported in 3 days -> no change
            hours = hours_from_counter(inside, baseline, kind, now, sc, un)
            log(f"{len(inside)} rain reports in the last 48 h")

        # ---- every reading, scaled, plus normalized current conditions ----
        readings = {}
        for c, v in status.items():
            s2, u2 = scale_of(specs.get(c, {}))
            nm = specs.get(c, {}).get("name") or ""
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                readings[c] = {"value": round(v / (10 ** s2), 3), "unit": u2, "name": nm}
            elif isinstance(v, str) and v:
                readings[c] = {"value": v, "unit": u2, "name": nm}
        current = normalize_current(readings)
        if obs_time:
            current["time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(obs_time / 1000))
        log("Current conditions for the page: " + (", ".join(f"{k}={v}" for k, v in current.items()) or "none recognized"))

        data = {
            "station": "smartlife", "name": info.get("name") or "Smart Life station",
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now / 1000)),
            "online": info.get("online"), "hours": hours,
            "total24": round(sum(hours[:24]), 2) if hours else None, "total48": round(sum(hours), 2) if hours else None,
            "today": val(today_code), "rate": val(rate_code), "rain_code": code, "rain_kind": kind,
            "reported_hours": 48 if hours else 0, "current": current, "readings": readings,
        }
        out.mkdir(parents=True, exist_ok=True)
        (out / "smartlife.json").write_text(json.dumps(data, separators=(",", ":")))
        if hours:
            log(f"Rain: 48 h {data['total48']} in, 24 h {data['total24']} in, today {data['today']}, rate {data['rate']}")
    except urllib.error.HTTPError as e:
        log(f"Smart Life: HTTP {e.code} from Tuya")
    except Exception as e:
        msg = str(e).replace(dev, "<device>") if dev else str(e)
        log(f"Smart Life: failed ({msg})")
        if "permission" in msg.lower() or "1106" in msg or "28841" in msg:
            log("Hint: check that the cloud project's API services are authorized and its trial hasn't expired.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
