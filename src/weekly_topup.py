"""Weekly Jackson P1 top-up ("prep for Monday").

Why this exists
  Every Monday Rahaf's 5-day blitz starts on a fresh batch of Priority 1 records. Getting that
  batch ready was a hand-run routine (see memory note jackson-weekly-topup-routine): pick the next
  rung's list, pull unassigned/no-status records, assign them to Rahaf, tag `Batch YYYY-MM-DD` plus
  one `SMS Line A-E`, set the owner's primary phone to the best Dial First/Second mobile, and point
  presets 01/02 at the new list and Batch tag. am-start (pm_text_sender.py) then sets them
  Prospecting at 9:50 Central, which starts the drip.

Modes
  python src/weekly_topup.py plan                    # dry run: what Monday's batch would be
  python src/weekly_topup.py plan --batch-date 2026-11-02 --target 72 --slack
  python src/weekly_topup.py apply                   # same plan, still writes nothing without --live
  python src/weekly_topup.py apply --live --only-between 18:00-20:00 --slack

Env: REISIFT_API_KEY; SLACK_WEBHOOK_URL / GOOGLE_CHAT_WEBHOOK_URL for --slack.

Safety rules
  - `plan`, and `apply` without --live, write nothing to DataSift.
  - `apply --live` refuses if that Monday's Batch tag already exists (a rerun would pick a second
    batch); pass --force only after checking what the first run did.
  - Every write is logged to output/account_backups/topup_write_{date}.json, even on a crash.
  - Status is never set here; am-start owns the status flip (it starts the drip).
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pm_text_sender import (  # noqa: E402
    CALLER_USER_UUID, PRESET_01_TITLE, STATE_DIR, Sift, _best_primary, _env, _google_chat, _in_window, _slack,
    ct_now,
)

PRESET_01_UUID = "4881671f-526c-440f-add2-97564c89d069"
P1_TAG = "Priority 1"
LIST_PREFIX = "Jackson - Priority 1 - "
# Rungs in the order they get added to presets 01/02 (lowest doors-per-deal first among what's left).
RUNGS = [
    "Out-of-State + Vacant - 12.3 doors per deal",
    "Free & Clear + Out-of-State + Vacant - 10.7 doors per deal",
    "Free & Clear + Vacant - 18.9 doors per deal",
    "High Equity + Vacant - 19.9 doors per deal",
    "Absentee + High Equity + Vacant - 19.4 doors per deal",
    "Senior + Vacant - 23.0 doors per deal",
    "Absentee + Vacant - 23.8 doors per deal",
]
DEFAULT_TARGET = 72
LINES = ("A", "B", "C", "D", "E")   # even round-robin (D/E ran a light 8-each first week on 10/26)
LINE_CAP = 20                        # new records per SMS line per day
MAIL_ONLY_TAG = "Mail Only"
TAG_CHUNK = 50
BACKUP_DIR = STATE_DIR.parent / "account_backups"


def _rows(resp):
    if isinstance(resp, dict):
        return resp.get("results") or resp.get("data") or []
    return resp or []


def next_monday(today: dt.date) -> dt.date:
    return today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7) if today.weekday() else today


def line_plan(n: int) -> list[str]:
    return [LINES[i % len(LINES)] for i in range(n)]


def skip_reason(d: dict) -> str | None:
    """Why a property detail can't go into the batch, or None if it can (phone checked separately)."""
    tags = [t if isinstance(t, str) else (t.get("title") or "") for t in (d.get("tags") or [])]
    o = d.get("owner") or {}
    if d.get("assigned_to"):
        return "assigned"
    if d.get("status"):
        return "has status"
    if "Mail Only" in tags:
        return "already Mail Only"
    if any(t.startswith("Batch ") or t.startswith("SMS Line") for t in tags):
        return "batch/line tag"
    if o.get("type") != "person":
        return "not person"
    if not (o.get("first_name") or "").strip():
        return "no first name"
    if o.get("dnc") or o.get("opt_out"):
        return "dnc/opt_out"
    return None


def load_presets(api: Sift) -> tuple[dict, dict]:
    presets = _rows(api.call("/api/internal/filter-preset/?limit=500"))
    p01 = [p for p in presets if p.get("uuid") == PRESET_01_UUID]
    if not p01 or not (p01[0].get("title") or "").startswith(PRESET_01_TITLE):
        sys.exit(f"Preset 01 ({PRESET_01_UUID}) not found or renamed")
    p01 = p01[0]
    p02 = [p for p in presets if (p.get("title") or "").startswith("02.") and p.get("folder") == p01.get("folder")]
    if len(p02) != 1:
        sys.exit(f"Expected one '02.' preset in preset 01's folder, found {len(p02)}")
    return p01, p02[0]


def plan(api: Sift, target: int) -> dict:
    p01, p02 = load_presets(api)
    lists = {(l.get("title") or l.get("name") or ""): l for l in _rows(api.call("/api/internal/list/?limit=999"))}
    used_uuids = set(p01["filters"]["must"].get("any_lists") or [])

    rungs, missing = [], []
    for r in RUNGS:
        lst = lists.get(LIST_PREFIX + r)
        if not lst:
            missing.append(r)
            continue
        rungs.append({"name": r, "uuid": lst["uuid"], "count": lst.get("properties_count") or lst.get("count"),
                      "used": lst["uuid"] in used_uuids})
    used = [r for r in rungs if r["used"]]
    unused = [r for r in rungs if not r["used"]]

    tags = _rows(api.call("/api/internal/tag/?offset=0&limit=10000&ordering=title"))
    p1_uuid = next((t["uuid"] for t in tags if t.get("title") == P1_TAG), None)
    if not p1_uuid:
        sys.exit(f"Tag '{P1_TAG}' not found")

    base = dict(p01["filters"]["must"])
    for k in ("any_tags", "assigned_to", "predictivecall_attempts"):
        base.pop(k, None)
    base["phone"] = 1

    # Work the used rungs first (leftovers), then add new rungs one at a time until the batch fills.
    eligible, mail_only, skipped, seen, pool_by_rung, added = [], [], Counter(), set(), {}, []
    for r in used + unused:
        if len(eligible) >= target:
            break
        must = dict(base, any_lists=[r["uuid"]], any_tags=[p1_uuid])
        # Search rows don't carry assigned_to/status reliably; skip_reason() checks the detail.
        rows = api.search(must)
        pool_by_rung[r["name"]] = len(rows)
        took = 0
        for row in rows:
            if len(eligible) >= target:
                break
            u = row["uuid"]
            if u in seen:
                continue
            seen.add(u)
            d = api.call(f"/api/internal/property/{u}/")
            why = skip_reason(d)
            if why:
                skipped[why] += 1
                continue
            o = d.get("owner") or {}
            addr = d.get("address")
            rec = {"uuid": u, "rung": r["name"], "owner_uuid": o.get("uuid"),
                   "name": f"{o.get('first_name') or ''} {o.get('last_name') or ''}".strip(),
                   "address": addr.get("street") if isinstance(addr, dict) else addr,
                   "best_phone": _best_primary({p["number"]: p for p in (o.get("phones") or [])}),
                   "current_primary": o.get("primary_phone")}
            if rec["best_phone"]:
                eligible.append(rec)
                took += 1
            else:
                mail_only.append(rec)
        if not r["used"] and took:
            added.append(r)

    for rec, ln in zip(eligible, line_plan(len(eligible))):
        rec["line"] = ln
    for rec in eligible:
        cur = rec["current_primary"]
        cur = cur.get("number") if isinstance(cur, dict) else cur
        rec["primary_change"] = str(cur or "")[-10:] != str(rec["best_phone"])[-10:]

    return {"preset_01": p01["uuid"], "preset_02": p02["uuid"], "p1_tag": p1_uuid,
            "rungs_used": [r["name"] for r in used], "rungs_added": [r["name"] for r in added],
            "rungs_added_uuids": [r["uuid"] for r in added],
            "rungs_missing": missing, "pool_by_rung": pool_by_rung, "eligible": eligible,
            "mail_only": mail_only, "skipped": dict(skipped)}


def report(p: dict, batch_date: str, target: int, live: bool = False) -> str:
    el = p["eligible"]
    mode = "LIVE" if live else "DRY RUN, nothing written"
    lines = [f"Weekly top-up plan for {batch_date} ({mode}): "
             f"{len(el)} of {target} ready, {len(p['mail_only'])} Mail Only"]
    if len(el) < target:
        lines.append(f"  SHORT by {target - len(el)}: ran out of rungs")
    lines.append("  Rungs already in presets 01/02: " + (", ".join(p["rungs_used"]) or "none"))
    lines.append("  Rungs to add: " + (", ".join(p["rungs_added"]) or "none"))
    if p["rungs_missing"]:
        lines.append("  Lists not found: " + ", ".join(p["rungs_missing"]))
    by_rung = Counter(r["rung"] for r in el)
    for name, n in p["pool_by_rung"].items():
        lines.append(f"    {name}: {n} in list with a phone, taking {by_rung.get(name, 0)}")
    lines.append("  Lines: " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(r["line"] for r in el).items())))
    lines.append(f"  Primary phone changes: {sum(r['primary_change'] for r in el)}")
    if p["skipped"]:
        lines.append("  Skipped: " + ", ".join(f"{k} {v}" for k, v in sorted(p["skipped"].items())))
    verb = "Tagging" if live else "Would tag"
    lines.append(f"  {verb} 'Batch {batch_date}', assign to Rahaf, and point presets 01/02 at it.")
    return "\n".join(lines)


def digits(v) -> str:
    if isinstance(v, dict):
        v = v.get("number")
    return "".join(c for c in str(v or "") if c.isdigit())[-10:]


def tag_uuids(api: Sift) -> dict:
    return {t.get("title"): t["uuid"] for t in _rows(api.call("/api/internal/tag/?offset=0&limit=10000&ordering=title"))}


def ensure_tag(api: Sift, title: str) -> str:
    uuid = tag_uuids(api).get(title)
    if uuid:
        return uuid
    api.call("/api/internal/tag/", "POST", {"title": title})
    uuid = tag_uuids(api).get(title)
    if not uuid:
        raise RuntimeError(f"Tag '{title}' was not created")
    return uuid


def add_tags(api: Sift, uuids: list[str], titles: list[str]) -> int:
    """Tag exactly these properties. The uuids must sit inside query.must: a top-level
    `properties` key is ignored and the tag then lands on every record in the account."""
    n = 0
    for i in range(0, len(uuids), TAG_CHUNK):
        chunk = uuids[i:i + TAG_CHUNK]
        body = {"query": {"must": {"properties": chunk}, "ordering": ["-list_count"]}, "tags": titles}
        assert body["query"]["must"]["properties"] == chunk
        r = api.call("/api/internal/property/add-tags/", "POST", body)
        n += (r or {}).get("count", 0) if isinstance(r, dict) else 0
    return n


def check(api: Sift, rec: dict, batch_title: str) -> list[str]:
    d = api.call(f"/api/internal/property/{rec['uuid']}/")
    tags = [t if isinstance(t, str) else t.get("title") for t in (d.get("tags") or [])]
    lines = [t for t in tags if t and t.startswith("SMS Line")]
    a = d.get("assigned_to")
    a = a.get("uuid") if isinstance(a, dict) else a
    problems = []
    if a != CALLER_USER_UUID:
        problems.append(f"assigned_to={a}")
    if batch_title not in tags:
        problems.append("no batch tag")
    if lines != [f"SMS Line {rec['line']}"]:
        problems.append(f"line tags={lines}")
    if digits((d.get("owner") or {}).get("primary_phone")) != digits(rec["best_phone"]):
        problems.append(f"primary={(d.get('owner') or {}).get('primary_phone')}")
    if d.get("status"):
        problems.append(f"status={d.get('status')}")
    return problems


def apply(api: Sift, p: dict, batch_date: str, force: bool = False) -> str:
    """Write the plan to DataSift. Returns a summary line block for Slack."""
    batch_title = f"Batch {batch_date}"
    el, mail = p["eligible"], p["mail_only"]
    per_line = Counter(r["line"] for r in el)
    if max(per_line.values(), default=0) > LINE_CAP:
        raise RuntimeError(f"Line load {dict(per_line)} exceeds {LINE_CAP}/line; add a line or lower --target")
    if batch_title in tag_uuids(api) and not force:
        raise RuntimeError(f"Tag '{batch_title}' already exists: this week's batch may already be written "
                           "(rerun with --force only after checking)")

    log = {"batch_date": batch_date, "started": ct_now().isoformat(), "assigned": [], "primary": [],
           "tagged": {}, "mail_only": [], "presets": {}, "errors": []}
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    log_path = BACKUP_DIR / f"topup_write_{batch_date}.json"
    try:
        batch_uuid = ensure_tag(api, batch_title)
        log["batch_tag_uuid"] = batch_uuid
        for rec in el:
            try:
                api.call(f"/api/internal/property/{rec['uuid']}/", "PATCH", {"assigned_to": CALLER_USER_UUID})
                log["assigned"].append(rec["uuid"])
                if rec["primary_change"]:
                    api.call(f"/api/internal/owner/{rec['owner_uuid']}/", "PATCH", {"primary_phone": rec["best_phone"]})
                    log["primary"].append({"owner": rec["owner_uuid"], "old": rec["current_primary"],
                                           "new": rec["best_phone"]})
            except Exception as e:  # keep going; the read-back reports the record
                log["errors"].append({"uuid": rec["uuid"], "step": "assign/primary", "error": str(e)})
        for ln in LINES:
            uu = [r["uuid"] for r in el if r["line"] == ln]
            if uu:
                log["tagged"][ln] = add_tags(api, uu, [batch_title, f"SMS Line {ln}"])
        if mail:
            ensure_tag(api, MAIL_ONLY_TAG)
            add_tags(api, [r["uuid"] for r in mail], [MAIL_ONLY_TAG])
            log["mail_only"] = [r["uuid"] for r in mail]

        p01, p02 = load_presets(api)
        for pr in (p01, p02):
            old = copy.deepcopy(pr["filters"])
            filt = copy.deepcopy(pr["filters"])
            filt["must"]["any_tags"] = [batch_uuid]
            lists = filt["must"].setdefault("any_lists", [])
            lists += [u for u in p["rungs_added_uuids"] if u not in lists]
            api.call(f"/api/internal/filter-preset/{pr['uuid']}/", "PATCH", {"filters": filt})
            log["presets"][pr["uuid"]] = {"title": pr.get("title"), "old": old, "new": filt}
    finally:
        log["finished"] = ct_now().isoformat()
        log_path.write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
        print(f"Write log saved to {log_path}")

    bad = {}
    for rec in el:
        problems = check(api, rec, batch_title)
        if problems:
            bad[rec["uuid"]] = problems
    out = [f"  Wrote: {len(log['assigned'])} assigned, {len(log['primary'])} primary phones, "
           f"{len(log['mail_only'])} Mail Only, presets 01/02 -> {batch_title}"]
    if log["errors"]:
        out.append(f"  WRITE ERRORS on {len(log['errors'])} records, see {log_path.name}")
    out.append(f"  Read-back: {len(el) - len(bad)} of {len(el)} OK" if not bad else
               f"  Read-back PROBLEMS on {len(bad)} of {len(el)}: " +
               "; ".join(f"{u[:8]} {', '.join(v)}" for u, v in list(bad.items())[:8]))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("plan", help="dry run: build Monday's batch and report it")
    pa = sub.add_parser("apply", help="build the batch and write it (only with --live)")
    for s in (pp, pa):
        s.add_argument("--batch-date", help="Monday the batch starts (default: next Monday, Central)")
        s.add_argument("--target", type=int, default=DEFAULT_TARGET)
        s.add_argument("--slack", action="store_true", help="post the report to Slack and Google Chat")
    pa.add_argument("--live", action="store_true", help="actually write to DataSift")
    pa.add_argument("--force", action="store_true", help="write even if this Monday's Batch tag exists")
    pa.add_argument("--only-between", help="HH:MM-HH:MM Central; do nothing outside it")
    args = ap.parse_args()

    if getattr(args, "only_between", None) and not _in_window(args.only_between):
        print(f"Outside window {args.only_between} Central (now {ct_now():%H:%M}); skipping.")
        return 0
    live = args.cmd == "apply" and args.live
    api = Sift(_env("REISIFT_API_KEY") or sys.exit("REISIFT_API_KEY missing"))
    batch_date = args.batch_date or next_monday(ct_now().date()).isoformat()
    p = plan(api, args.target)
    text = report(p, batch_date, args.target, live=live)
    print(text)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    out = STATE_DIR / f"topup_plan_{batch_date}.json"
    out.write_text(json.dumps(dict(p, batch_date=batch_date, target=args.target, assignee=CALLER_USER_UUID,
                                   generated=ct_now().isoformat()), indent=1, default=str), encoding="utf-8")
    print(f"Plan saved to {out}")
    rc = 0
    if live:
        try:
            result = apply(api, p, batch_date, force=args.force)
        except Exception as e:
            result, rc = f"  TOP-UP FAILED: {e}", 1
        print(result)
        text += "\n" + result
    if args.slack:
        _slack(text)
        _google_chat(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
