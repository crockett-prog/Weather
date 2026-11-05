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
    """-> (counter_code, counter_kind, rate_code, today_code). kind: total | daily | hourly"""
    codes = [c for c in specs if "rain" in c.lower() or "precip" in c.lower()]
    def find(*words):
        for c in codes:
            lc = c.lower()
            if any(w in lc for w in words):
                return c
        return None
    total = find("total", "accum", "cumul", "year")
    daily = find("day", "daily", "today", "24h")
    hourly = find("1h", "hour")
    rate = find("rate", "intensity")
    if os.environ.get("TUYA_RAIN_CODE"):
        forced = os.environ["TUYA_RAIN_CODE"].strip()
        kind = os.environ.get("TUYA_RAIN_KIND", "total").strip()
        return forced, kind, rate, daily
    if total: return total, "total", rate, daily
    if daily: return daily, "daily", rate, daily
    if hourly: return hourly, "hourly", rate, daily
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
        elif v < prev - 1e-6:            # counter reset (midnight for daily counters)
            inc = v
        else:
            inc = v - prev
        prev = v
        if 0 <= slot < 48:
            slots[slot] += max(0.0, inc)
    return [round(x, 3) for x in slots]

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
        for c in status:
            specs.setdefault(c, {"code": c, "values": "{}"})

        log(f"Station: {info.get('name')!r}, online={info.get('online')}")
        log("Data points reported by the station (code = value [unit, scale]):")
        for c, v in status.items():
            sc, un = scale_of(specs.get(c, {}))
            log(f"   {c} = {v}  [{un or '-'}, scale {sc}]")

        code, kind, rate_code, today_code = pick_rain_codes(specs)
        if not code:
            log("No rain data point found. If one of the codes above is rain, set repo secret TUYA_RAIN_CODE to it "
                "(and TUYA_RAIN_KIND to total, daily or hourly).")
            return 0
        sc, un = scale_of(specs.get(code, {}))
        log(f"Using rain code '{code}' as a {kind} counter ({un or 'unit unknown'}, scale {sc})")

        now = int(time.time() * 1000)
        start = now - 50 * HOUR
        logs = report_logs(api, dev, code, start - 24 * HOUR, now)
        before = [e for e in logs if int(e["event_time"]) < now - 48 * HOUR]
        inside = [e for e in logs if int(e["event_time"]) >= now - 48 * HOUR]
        baseline = to_inches(before[-1]["value"], sc, un) if before else None
        if baseline is None and inside:
            baseline = to_inches(inside[0]["value"], sc, un); inside = inside[1:]
        if baseline is None:
            baseline = to_inches(status.get(code), sc, un)     # nothing reported in 3 days -> no change
        hours = hours_from_counter(inside, baseline, kind, now, sc, un)
        log(f"{len(inside)} rain reports in the last 48 h")

        def val(c):
            if not c or c not in status:
                return None
            s2, u2 = scale_of(specs.get(c, {}))
            x = to_inches(status[c], s2, u2)
            return None if x is None else round(x, 3)

        readings = {}
        for c, v in status.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                s2, u2 = scale_of(specs.get(c, {}))
                readings[c] = {"value": v / (10 ** s2), "unit": u2}
        data = {
            "station": "smartlife", "name": info.get("name") or "Smart Life station",
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now / 1000)),
            "online": info.get("online"), "hours": hours,
            "total24": round(sum(hours[:24]), 2), "total48": round(sum(hours), 2),
            "today": val(today_code), "rate": val(rate_code), "rain_code": code, "rain_kind": kind,
            "reported_hours": 48, "readings": readings,
        }
        out.mkdir(parents=True, exist_ok=True)
        (out / "smartlife.json").write_text(json.dumps(data, separators=(",", ":")))
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
