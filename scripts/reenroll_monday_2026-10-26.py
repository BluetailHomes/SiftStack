"""One-shot: re-enroll 3 Jackson P1 records in their line drip on Mon 2026-10-26 ~9:50 AM Central.

Their drips were removed Sat 2026-10-24 so they get no weekend texts. Flipping status
not_interested -> prospecting re-fires the line sequence (prospecting + one SMS Line tag).
Writes only inside 09:45-10:30 Central on 2026-10-26; any other time it just prints the plan.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
try:  # local runs read .env; the GitHub runner gets secrets as env vars
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass
import pm_text_sender as p

RECORDS = {
    "4109 Monroe Ave": "7ac2937d-d421-42bb-9bf8-5df2de66194e",
    "2015 E Gregory Blvd": "88083868-021c-4a06-818a-dcca8c73fbed",
    "2008 E 30th St": "be43db56-3b12-47cb-bf74-19c1a177dcb9",
}
DAY, START, END = "2026-10-26", "09:45", "10:30"


def history(api, u):
    h = api.call(f"/api/internal/drip-campaign-history/by-property/?property_uuid={u}")
    return h if isinstance(h, list) else (h.get("results") or [])


def running(api, u):
    out = []
    for h in history(api, u):
        if h.get("status") == "running":
            d = h.get("drip_campaign")
            out.append(d.get("title") if isinstance(d, dict) else (h.get("name") or str(d)))
    return out


def lines(api, u):
    d = api.call(f"/api/internal/property/{u}/")
    tags = [t.get("title") if isinstance(t, dict) else t for t in (d.get("tags") or [])]
    return [t for t in tags if t and t.startswith("SMS Line")], d.get("status")


def main():
    now = p.ct_now()
    live = now.strftime("%Y-%m-%d") == DAY and START <= now.strftime("%H:%M") <= END
    print(f"Central now {now:%Y-%m-%d %H:%M} -> {'LIVE' if live else 'outside window, no writes'}")
    api = p.Sift(os.environ["REISIFT_API_KEY"])
    flipped, report = [], []
    for name, u in RECORDS.items():
        tags, status = lines(api, u)
        run = running(api, u)
        if run:
            report.append(f"{name}: skipped, already running {run}")
        elif len(tags) != 1:
            report.append(f"{name}: skipped, line tags {tags}")
        elif not live:
            report.append(f"{name}: would re-enroll ({tags[0]}, status {status})")
        else:
            api.call(f"/api/internal/property/{u}/status/", "POST", {"status": "not_interested"})
            time.sleep(3)
            api.call(f"/api/internal/property/{u}/status/", "POST", {"status": "prospecting"})
            flipped.append((name, u, tags[0]))
    if flipped:
        time.sleep(90)
        for name, u, tag in flipped:
            tags, status = lines(api, u)
            run = running(api, u)
            ok = status == "prospecting" and tags == [tag] and len(run) == 1
            report.append(f"{name}: {'OK' if ok else 'CHECK'} status {status}, {tag}, running {run}")
    text = "Monday re-enroll (3 Jackson P1 records)\n" + "\n".join(report)
    print(text)
    if live:
        p._slack(text)


if __name__ == "__main__":
    main()
