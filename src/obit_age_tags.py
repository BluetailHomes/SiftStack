"""Stamp the Obit age tags (Obit under 3mo / Obit 3-6mo / Obit 6mo+) from each record's
last_obituary_date, so the 01. JACKSON OBITUARY presets 01-03 (age tag + list
"Jackson - Obituary - P1 Neighborhoods") show the records SiftMap pulls in.

Why this exists
  SiftMap preset 16030 adds Jackson obituary records to the list + "Obituary P1" tag but no age
  tag, so presets 01-03 stay empty. The tags already in the account were stamped once (around the
  2026-10-14 blueprint) and never moved: on 2026-11-12 "Obit 3-6mo" held records up to 209 days old.

Which records
  On the P1-neighborhood obituary list, tagged "Obituary P1", or already carrying an age tag
  (so old stamps keep moving as records age). A record with no last_obituary_date is left as it is.

Buckets (days since last_obituary_date, Central date)
  < 90 -> Obit under 3mo (hold, Ty's 3-month wait) | 90-182 -> Obit 3-6mo | 183+ -> Obit 6mo+

Modes
  python src/obit_age_tags.py            # plan: what would change; read-only
  python src/obit_age_tags.py --live     # add the right tag, remove the wrong ones, read back

Safety: uuids always sit inside query.must (else a bulk tag call hits the whole account); before
state goes to output/account_backups/obit_age_tags_{date}.json before any write; only the three age
tags are ever added or removed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter

from pm_text_sender import STATE_DIR, Sift, _env, _slack, ct_now  # noqa: E402
from weekly_topup import BACKUP_DIR, TAG_CHUNK, add_tags, tag_uuids  # noqa: E402

UNDER_3, MID, OVER_6 = "Obit under 3mo", "Obit 3-6mo", "Obit 6mo+"
AGE_TAGS = (UNDER_3, MID, OVER_6)
PULL_TAG = "Obituary P1"
PULL_LIST = "83c72451-32c7-4be6-a596-7ed23cf9adb1"   # Jackson - Obituary - P1 Neighborhoods
HOLD_DAYS = 90
SIX_MONTHS_DAYS = 183


def _title(t) -> str:
    return t if isinstance(t, str) else (t.get("title") or "")


def _date(v) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def bucket(obit: dt.date | None, today: dt.date) -> str | None:
    if not obit:
        return None
    age = (today - obit).days
    return UNDER_3 if age < HOLD_DAYS else MID if age < SIX_MONTHS_DAYS else OVER_6


def change(d: dict, today: dt.date) -> tuple[str | None, list[str]]:
    """(tag to add or None, age tags to remove) for one property detail record."""
    want = bucket(_date(d.get("last_obituary_date")), today)
    if not want:
        return None, []
    have = {_title(t) for t in d.get("tags") or []} & set(AGE_TAGS)
    return (None if want in have else want), sorted(have - {want})


def remove_tags(api: Sift, uuids: list[str], titles: list[str]) -> int:
    n = 0
    for i in range(0, len(uuids), TAG_CHUNK):
        chunk = uuids[i:i + TAG_CHUNK]
        body = {"query": {"must": {"properties": chunk}, "ordering": ["-list_count"]}, "tags": titles}
        assert body["query"]["must"]["properties"] == chunk
        r = api.call("/api/internal/property/remove-tags/", "POST", body)
        n += (r or {}).get("count", 0) if isinstance(r, dict) else 0
    return n


def targets(api: Sift) -> list[str]:
    tags = tag_uuids(api)
    tag_ids = [tags[t] for t in AGE_TAGS + (PULL_TAG,) if t in tags]
    seen: dict[str, None] = {}
    for must in ({"any_lists": [PULL_LIST]},) + (({"any_tags": tag_ids},) if tag_ids else ()):
        for row in api.search(must):
            seen.setdefault(row["uuid"])
    return list(seen)


def plan(api: Sift, today: dt.date) -> dict:
    rows, counts = [], Counter()
    for uuid in targets(api):
        d = api.call(f"/api/internal/property/{uuid}/")
        add, drop = change(d, today)
        want = bucket(_date(d.get("last_obituary_date")), today)
        counts[want or "no obituary date"] += 1
        if add or drop:
            rows.append({"uuid": uuid, "street": (d.get("address") or {}).get("street"),
                         "state": (d.get("address") or {}).get("state"), "obit": d.get("last_obituary_date"),
                         "before": sorted({_title(t) for t in d.get("tags") or []} & set(AGE_TAGS)),
                         "add": add, "remove": drop})
    return {"rows": rows, "counts": dict(counts), "checked": sum(counts.values())}


def apply(api: Sift, p: dict, today: dt.date) -> dict:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    (BACKUP_DIR / f"obit_age_tags_{today}.json").write_text(json.dumps(p, indent=1), encoding="utf-8")
    for tag in AGE_TAGS:
        adds = [r["uuid"] for r in p["rows"] if r["add"] == tag]
        if adds:
            add_tags(api, adds, [tag])
        drops = [r["uuid"] for r in p["rows"] if tag in r["remove"]]
        if drops:
            remove_tags(api, drops, [tag])
    bad = []
    for r in p["rows"]:
        d = api.call(f"/api/internal/property/{r['uuid']}/")
        add, drop = change(d, today)
        if add or drop:
            bad.append(r["street"] or r["uuid"])
    return {"written": len(p["rows"]) - len(bad), "failed": bad}


def report(p: dict, res: dict | None, live: bool) -> str:
    moves = Counter(f"{'/'.join(r['before']) or 'none'} -> {r['add'] or '(remove only)'}" for r in p["rows"])
    c = p["counts"]
    lines = [f"Obit age tags ({'LIVE' if live else 'plan'}): {p['checked']} records, {len(p['rows'])} to change",
             f"  now: {c.get(UNDER_3, 0)} under 3mo, {c.get(MID, 0)} 3-6mo, {c.get(OVER_6, 0)} 6mo+, "
             f"{c.get('no obituary date', 0)} no obituary date (left alone)"]
    lines += [f"  {n:>3}  {m}" for m, n in moves.most_common()]
    if res:
        lines.append(f"  written {res['written']}" + (f", NOT confirmed on read-back: {', '.join(res['failed'][:10])}" if res["failed"] else ""))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--live", action="store_true", help="write the tag changes")
    ap.add_argument("--slack", action="store_true")
    args = ap.parse_args()
    api = Sift(_env("REISIFT_API_KEY"))
    today = ct_now().date()
    p = plan(api, today)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"obit_age_tags_plan_{today}.json").write_text(json.dumps(p, indent=1), encoding="utf-8")
    res = apply(api, p, today) if args.live and p["rows"] else None
    text = report(p, res, args.live)
    print(text)
    if args.slack:
        _slack(text)
    return 1 if res and res["failed"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        if "--slack" in sys.argv:
            _slack(f"Obit age tags FAILED ({type(e).__name__}: {e})")
        raise
