"""Pull one caller's answered outbound calls for a Central-time day from the
DataSift logs API (no smrtPhone browser session needed), then download the
recordings. Writes the same call_log.json / calls_to_review.json shape as
pull_calls.py, so transcribe.py runs unchanged.

    python pull_calls_datasift.py                 # today (Central)
    python pull_calls_datasift.py --day 2026-10-26

Needs REISIFT_API_KEY (env var or .env in the working directory).
"""
import argparse
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path.cwd()
OUT = ROOT / "output" / "call_coaching"
API = "https://apiv2.reisift.io"


def _central_offset(when_utc):
    y = when_utc.year
    march = dt.datetime(y, 3, 1)
    start = march + dt.timedelta(days=(6 - march.weekday()) % 7 + 7, hours=8)
    nov = dt.datetime(y, 11, 1)
    end = nov + dt.timedelta(days=(6 - nov.weekday()) % 7, hours=7)
    return dt.timedelta(hours=-5) if start <= when_utc.replace(tzinfo=None) < end else dt.timedelta(hours=-6)


def to_ct(ts):
    u = dt.datetime.strptime(ts[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    off = _central_offset(u)
    return (u + off).replace(tzinfo=dt.timezone(off))


def ct_now():
    u = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    off = _central_offset(u)
    return (u + off).replace(tzinfo=dt.timezone(off))


def _env(name):
    if os.environ.get(name):
        return os.environ[name]
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


class Sift:
    def __init__(self, key):
        self.key = key
        self.last = 0.0

    def call(self, path):
        url = path if path.startswith("http") else API + path
        for attempt in range(12):
            wait = 0.3 - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            req = urllib.request.Request(url, headers={
                "authorization": f"Api-Key {self.key}",
                "accept": "application/json",
                "user-agent": "siftstack-cold-call-coach",
            })
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code == 429:
                    m = re.search(r"available in (\d+) second", body)
                    hint = int(m.group(1)) + 1 if m else 0
                    time.sleep(min(max(hint, 3 * (attempt + 1)), 300))
                    continue
                if e.code >= 500 and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemExit(f"DataSift API {e.code} on {path}: {body[:300]}")
        raise SystemExit(f"DataSift API kept rate-limiting {path}")


def load_calls(sift, day, email):
    calls, path = {}, "/api/internal/property/logs/?limit=100"
    while path:
        r = sift.call(path)
        rows = r.get("results") or []
        older = False
        for e in rows:
            ts = e.get("timestamp") or e.get("created_at") or ""
            if not ts:
                continue
            ct = to_ct(ts)
            if ct.date() < day:
                older = True
                continue
            et = e.get("event_type") or ""
            if ct.date() != day or not et.startswith("owner.call."):
                continue
            p = e.get("payload") or {}
            c = p.get("call") or {}
            if not c.get("uuid"):
                continue
            owner = (p.get("extra_info") or {}).get("owner") or {}
            oid = c.get("owner") or ""
            if isinstance(oid, dict):
                oid = oid.get("uuid") or ""
            prop = e.get("resource") or ""
            if isinstance(prop, dict):
                prop = prop.get("uuid") or ""
            cc = calls.setdefault(c["uuid"], {
                "ts": ts[:19].replace("T", " "), "from": c.get("origin_number") or "",
                "to": c.get("destination_number") or "", "dir": c.get("direction"),
                "by": ((c.get("external_user") or {}).get("email") or "").lower(),
                "dur": None, "result": None, "rec": None, "ext": c.get("external_id"),
                "name": " ".join(x for x in (owner.get("first_name"), owner.get("last_name")) if x),
                "owner": str(oid), "prop": str(prop),
            })
            if ts[:19].replace("T", " ") < cc["ts"]:
                cc["ts"] = ts[:19].replace("T", " ")
            if et == "owner.call.answered":
                cc.update(result="answered", dur=c.get("duration"), rec=c.get("recording"))
        nxt = r.get("next_cursor")
        path = None if older or not rows or not nxt else \
            f"/api/internal/property/logs/?limit=100&cursor={quote(str(nxt))}"
    return [dict(v, uuid=k) for k, v in calls.items() if v["by"] == email]


def normalize(c, caller):
    cid = str(c["ext"] or c["uuid"])
    rec_url = f"https://app.reisift.io/records/owners/{c['owner']}/properties" if c["owner"] else ""
    return {
        "call_id": cid, "created_at_utc": c["ts"], "caller": caller,
        "direction": c["dir"], "status": c["result"] or "not answered", "disposition": "",
        "contact_name": c["name"], "from_num": c["from"], "to_num": c["to"],
        "duration_seconds": int(c["dur"] or 0), "reisift_record_url": rec_url,
        "recording_url": c["rec"] or "", "call_detail_url": "",
    }


def download(url, dest):
    if dest.exists() and dest.stat().st_size > 4096:
        return True
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"user-agent": "Mozilla/5.0"}), timeout=120) as r:
            data = r.read()
    except Exception as e:
        print(f"  recording failed {dest.name}: {e}")
        return False
    if len(data) <= 4096:
        print(f"  recording too small {dest.name}")
        return False
    dest.write_bytes(data)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", help="YYYY-MM-DD, Central time (default today)")
    ap.add_argument("--min-seconds", type=int, default=25)
    ap.add_argument("--caller-email", default="rahaf@bluetailhomes.com")
    ap.add_argument("--caller-name", default="rahaf gamea")
    a = ap.parse_args()
    key = _env("REISIFT_API_KEY")
    if not key:
        raise SystemExit("REISIFT_API_KEY is not set")
    day = dt.date.fromisoformat(a.day) if a.day else ct_now().date()

    raw = load_calls(Sift(key), day, a.caller_email.lower())
    log = sorted((normalize(c, a.caller_name) for c in raw), key=lambda x: x["created_at_utc"])
    review = [c for c in log if c["direction"] == "outbound" and c["status"] == "answered"
              and c["recording_url"] and c["duration_seconds"] >= a.min_seconds]

    (OUT / "recordings").mkdir(parents=True, exist_ok=True)
    ok = [c for c in review if download(c["recording_url"], OUT / "recordings" / f"{c['call_id']}.mp3")]
    (OUT / "call_log.json").write_text(json.dumps(log, indent=1), encoding="utf-8")
    (OUT / "calls_to_review.json").write_text(json.dumps(ok, indent=1), encoding="utf-8")
    print(f"{day}: {len(log)} calls by {a.caller_email}, {len(review)} answered >= {a.min_seconds}s, {len(ok)} recordings saved")


if __name__ == "__main__":
    main()
