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
  python src/weekly_topup.py validate --slack        # Friday: re-read the batch, write the readiness marker
  python src/weekly_topup.py apply --uuids-file batch.csv --batch-date 2026-11-02   # exactly a reviewed batch

Env: REISIFT_API_KEY; SLACK_WEBHOOK_URL / GOOGLE_CHAT_WEBHOOK_URL for --slack.

Safety rules
  - `plan`, and `apply` without --live, write nothing to DataSift.
  - `apply --live` refuses if that Monday's Batch tag already exists (a rerun would pick a second
    batch); pass --force only after checking what the first run did.
  - The report opens with a warning when that Batch tag already exists, dry runs included, so a
    plan can't look like a batch still waiting to be written.
  - Every write is logged to output/account_backups/topup_write_{date}.json, even on a crash.
  - Status is never set here; am-start owns the status flip (it starts the drip).
  - Suppression is owner-level: a decline, an active lead, a do-not-contact tag or a DNC/opt-out
    on ANY of the owner's properties (matched by owner, name + mailing address, or any phone) keeps
    every one of their properties out. So do Entity Owner / Entity Classification Hold, known equity
    under 30% (unknown equity is kept), and a sale in the last 365 days.
  - `--uuids-file` (a CSV with property_uuid and line columns, in order) pins the batch to exactly
    those records. Each is re-checked; one that fails drops out and nothing is added in its place.
  - `validate` re-reads every record carrying that Monday's Batch tag (not the runner's plan file, so
    a Sunday dry run can't swap in a different batch) and writes output/pm_texts/topup_ready_{date}.json.
    Monday's am-start launches only the records listed there, and nothing when the file is
    missing or not ok, so any failed Friday step means no Monday launch.
"""
from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pm_text_sender import (  # noqa: E402
    CALLER_EMAIL, CALLER_USER_UUID, POST_SEQ_HOLD_TAG, PRESET_01_TITLE, STATE_DIR, STOP_REVIEW_TAG, Sift, _all_phones,
    _best_primary, _env, _google_chat, _in_window, _slack, _tag_key, ct_now, drip_rows, enroll_block, load_day,
    record_stop, review_tags_problem,
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
DEFAULT_TARGET = 45                  # temporary reduced cohort (approved 2026-10-27)
LINES = ("A", "B", "C", "D", "E")   # even round-robin (D/E ran a light 8-each first week on 10/26)
LINE_CAP = 9                         # new records per SMS line per Monday; am-start may start fewer
MAIL_ONLY_TAG = "Mail Only"
TAG_CHUNK = 50
BACKUP_DIR = STATE_DIR.parent / "account_backups"
MIN_EQUITY = 30.0                    # known equity below this is suppressed; unknown equity is kept
RECENT_SOLD_DAYS = 365               # a sale this recent means the owner on file may be stale
SUPPRESS_TAGS = ("Do Not Market", "ZZ STOP GO", "Entity Owner", "Entity Classification Hold", "Sold", "Negative Equity")
# Statuses on ANY of the owner's properties that keep the owner out of a new batch.
DECLINE_STATUSES = ("not_interested", "Dead Lead", "lost_deal")
ACTIVE_STATUSES = ("new_lead", "No Contact New Lead", "Cold Lead", "Warm Lead", "Hot Lead", "Ghosting Lead",
                   "prospecting", "under_contract", "closed", "Deep Prospecting")
OWNER_BLOCK_TAGS = ("Do Not Market", "ZZ STOP GO", STOP_REVIEW_TAG[1], POST_SEQ_HOLD_TAG[1])


def _rows(resp):
    if isinstance(resp, dict):
        return resp.get("results") or resp.get("data") or []
    return resp or []


def next_monday(today: dt.date) -> dt.date:
    return today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7) if today.weekday() else today


def line_plan(n: int) -> list[str]:
    return [LINES[i % len(LINES)] for i in range(n)]


def _d10(n) -> str:
    return re.sub(r"\D", "", str(n or ""))[-10:]


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", str(s or "").lower())).strip()


def owner_key(o: dict) -> str | None:
    """Name + mailing street + ZIP, to match one owner filed under different owner uuids."""
    a = (o or {}).get("address") or {}
    nm = _norm(f"{o.get('first_name') or ''} {o.get('last_name') or ''} {o.get('company') or ''}")
    st = _norm(a.get("street"))
    return f"{nm}|{st}|{str(a.get('zip5') or a.get('postal_code') or '')[:5]}" if nm and st else None


def suppress_reason(d: dict, today: dt.date | None = None) -> str | None:
    """Why this record must never be enrolled (whatever its assignment), or None.
    Unknown equity and an unknown sale date are kept, never dropped."""
    o = d.get("owner") or {}
    keys = {_tag_key(t) for t in d.get("tags") or []}
    for t in SUPPRESS_TAGS:
        if _tag_key(t) in keys:
            return f"tagged {t!r}"
    if o.get("deceased"):
        return "owner flagged deceased"
    if any(so.get("dnc") or so.get("opt_out") for so in d.get("secondary_owners") or []):
        return "secondary owner dnc/opt_out"
    for n, ph in _all_phones(d).items():
        if any("litig" in str(t).lower() for t in ph.get("tags") or []):
            return f"litigator tag on ...{_d10(n)[-4:]}"
    try:
        eq = float(d["equity_percent"]) if d.get("equity_percent") not in (None, "") else None
    except (TypeError, ValueError):
        eq = None
    if eq is not None and eq < MIN_EQUITY:
        return f"equity {eq:.1f}% < {MIN_EQUITY:.0f}%"
    try:
        sold = dt.date.fromisoformat(str(d.get("last_sold"))[:10]) if d.get("last_sold") else None
    except ValueError:
        sold = None
    if sold and ((today or ct_now().date()) - sold).days <= RECENT_SOLD_DAYS:
        return f"sold {sold} (within {RECENT_SOLD_DAYS} days)"
    return None


class OwnerIndex:
    """Who must not get a new batch because of ANOTHER property: built from every record in the
    account with a decline or active status, an owner block tag, or an owner DNC/opt-out flag, keyed
    by owner uuid (secondary owners too), name + mailing address, and every phone on the record."""

    def __init__(self, api: Sift):
        self.by_owner, self.by_key, self.by_phone = defaultdict(set), defaultdict(set), defaultdict(set)
        self.unreadable = 0
        tag_by_key = {_tag_key(t): u for t, u in tag_uuids(api).items() if t}
        labels: dict[str, set] = defaultdict(set)
        searches = [(f"declined ({s})", {"any_property_status": [s]}) for s in DECLINE_STATUSES]
        searches += [(f"active ({s})", {"any_property_status": [s]}) for s in ACTIVE_STATUSES]
        searches += [(f"tagged {t!r}", {"any_tags": [tag_by_key[_tag_key(t)]]}) for t in OWNER_BLOCK_TAGS
                     if _tag_key(t) in tag_by_key]
        searches += [(f"owner {k}", {k: 1}) for k in ("dnc", "opt_out")]
        for label, must in searches:
            for r in api.search(must):
                labels[r["uuid"]].add(label)
        # Search rows carry one phone; the detail has all of them and the secondary owners.
        for u, labs in labels.items():
            try:
                d = api.call(f"/api/internal/property/{u}/")
            except RuntimeError:
                self.unreadable += 1
                continue
            o = d.get("owner") or {}
            for ow in [o] + list(d.get("secondary_owners") or []):
                if ow.get("uuid"):
                    self.by_owner[ow["uuid"]] |= {(l, u) for l in labs}
            if k := owner_key(o):
                self.by_key[k] |= {(l, u) for l in labs}
            for n in _all_phones(d):
                self.by_phone[_d10(n)] |= {(l, u) for l in labs}

    def block(self, uuid: str, d: dict) -> str | None:
        """Why this owner is held because of another property, or None."""
        o = d.get("owner") or {}
        hits = set(self.by_owner.get(o.get("uuid"), ())) if o.get("uuid") else set()
        if k := owner_key(o):
            hits |= self.by_key.get(k, set())
        for n in _all_phones(d):
            hits |= self.by_phone.get(_d10(n), set())
        hits = sorted({l for l, pu in hits if pu != uuid})
        return f"owner-level: {', '.join(hits)} on another property" if hits else None


def skip_reason(d: dict) -> str | None:
    """Why a property detail can't go into the batch, or None if it can (phone checked separately)."""
    tags = [t if isinstance(t, str) else (t.get("title") or "") for t in (d.get("tags") or [])]
    o = d.get("owner") or {}
    if enroll_block(d):
        return "excluded (STOP review / post-sequence hold / no direct outreach)"
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
    if (d.get("sms_attempts") or 0) or (d.get("predictivecall_attempts") or 0):
        return "prior text/dialer attempts"
    return suppress_reason(d)


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


def load_pinned(path: str) -> dict:
    """{property_uuid: line} in file order, from a reviewed batch CSV (property_uuid, line columns)."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    pinned = {r["property_uuid"].strip(): r["line"].strip().upper() for r in rows if (r.get("property_uuid") or "").strip()}
    if len(pinned) != len(rows) or any(ln not in LINES for ln in pinned.values()):
        sys.exit(f"{path}: every row needs a unique property_uuid and a line in {LINES}")
    return pinned


def plan(api: Sift, target: int, pinned: dict | None = None, idx: OwnerIndex | None = None) -> dict:
    """With `pinned` ({uuid: line}), take exactly those records on those lines, in that order: each
    still goes through every check, and one that fails or isn't in a P1 rung just drops out."""
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

    base = copy.deepcopy(p01["filters"]["must"])
    for k in ("any_tags", "assigned_to", "predictivecall_attempts"):
        base.pop(k, None)
    base["phone"] = 1
    # P1's review tags stay out of the search even before preset 01 carries them; skip_reason()
    # checks each detail again.
    never = base.setdefault("must_not", {}).setdefault("any_tags", [])
    never += [u for u, _ in (STOP_REVIEW_TAG, POST_SEQ_HOLD_TAG) if u not in never]

    # Work the used rungs first (leftovers), then add new rungs one at a time until the batch fills.
    idx = idx or OwnerIndex(api)
    if pinned is not None:
        target = len(pinned)
    eligible, mail_only, skipped, seen, pool_by_rung, added = [], [], Counter(), set(), {}, []
    for r in used + unused:
        if len(eligible) >= target or (pinned is not None and seen >= set(pinned)):
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
            if u in seen or (pinned is not None and u not in pinned):
                continue
            seen.add(u)
            d = api.call(f"/api/internal/property/{u}/")
            why = skip_reason(d) or idx.block(u, d)
            if not why:
                stop = record_stop(api, u, d)
                if stop["hold"]:
                    why = "STOP policy hold"
            if not why:
                try:
                    why = "has drip history" if drip_rows(api, u) else None
                except RuntimeError:
                    why = "drip history unreadable"
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
            if rec["best_phone"] and any(e["best_phone"] == rec["best_phone"] or (rec["owner_uuid"] and e["owner_uuid"] == rec["owner_uuid"]) for e in eligible):
                skipped["owner or number already in batch"] += 1
            elif rec["best_phone"]:
                eligible.append(rec)
                took += 1
            else:
                mail_only.append(rec)
        if not r["used"] and took:
            added.append(r)

    if pinned is None:
        for rec, ln in zip(eligible, line_plan(len(eligible))):
            rec["line"] = ln
    else:
        order = list(pinned)
        eligible.sort(key=lambda rec: order.index(rec["uuid"]))
        for rec in eligible:
            rec["line"] = pinned[rec["uuid"]]
        if missing_pinned := [u for u in pinned if u not in seen]:
            skipped["pinned record not in a P1 rung search"] = len(missing_pinned)
        if mail_only:  # a pinned record that lost its phone is held, not moved to Mail Only
            skipped["pinned record has no usable phone"] = len(mail_only)
            mail_only = []
    for rec in eligible:
        cur = rec["current_primary"]
        cur = cur.get("number") if isinstance(cur, dict) else cur
        rec["primary_change"] = str(cur or "")[-10:] != str(rec["best_phone"])[-10:]

    return {"preset_01": p01["uuid"], "preset_02": p02["uuid"], "p1_tag": p1_uuid,
            "rungs_used": [r["name"] for r in used], "rungs_added": [r["name"] for r in added],
            "rungs_added_uuids": [r["uuid"] for r in added],
            "rungs_missing": missing, "pool_by_rung": pool_by_rung, "eligible": eligible,
            "mail_only": mail_only, "skipped": dict(skipped), "pinned": pinned is not None,
            "index_unreadable": idx.unreadable}


def report(p: dict, batch_date: str, target: int, live: bool = False, batch_exists: bool = False,
           force: bool = False) -> str:
    el = p["eligible"]
    mode = "LIVE" if live else "DRY RUN, nothing written"
    lines = []
    if batch_exists:  # warn in dry runs too, so a plan can't look like a batch still waiting to be written
        lines.append(f"Batch {batch_date} already exists, " +
                     ("--force is overriding that and writing anyway" if live and force else
                      "a live run would refuse (this week's batch may already be written)"))
    lines += [f"Weekly top-up plan for {batch_date} ({mode}): "
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


def check(api: Sift, rec: dict, batch_title: str, d: dict | None = None) -> list[str]:
    d = d or api.call(f"/api/internal/property/{rec['uuid']}/")
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
    if p.get("index_unreadable"):
        raise RuntimeError(f"{p['index_unreadable']} records behind the owner-level check were unreadable")
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


def validate(api: Sift, batch_date: str, idx: OwnerIndex | None = None) -> tuple[dict, str]:
    """Re-read every record carrying that Monday's Batch tag and write the readiness marker am-start
    needs. The tag, not a plan file, defines the batch, so a later dry run can't change it.
    A record is ready only if the write landed (assigned, Batch + one line tag, primary = its best
    mobile, no status), the STOP policy allows it, its primary is not DNC, it has no drip history, it
    passes suppress_reason() and the owner-level check, and no other batch record shares its primary
    number or owner. ok=False (Monday launches nothing) when the Batch tag is missing, the exclusion
    tags can't be confirmed, any record can't be read, or no record is ready. An excluded record
    just drops out."""
    batch_title = f"Batch {batch_date}"
    marker = {"ok": False, "batch_date": batch_date, "checked": ct_now().isoformat(), "uuids": [], "lines": {},
              "excluded": {}, "problem": None}
    el, details, unread = [], {}, []
    try:
        batch_uuid = tag_uuids(api).get(batch_title)
        if not batch_uuid:
            marker["problem"] = f"tag {batch_title!r} not found (no batch written)"
        for row in api.search({"any_tags": [batch_uuid]}) if batch_uuid else []:
            u = row["uuid"]
            try:
                d = api.call(f"/api/internal/property/{u}/")
            except RuntimeError as e:
                unread.append(u)
                marker["excluded"][u] = [f"unreadable: {str(e)[:80]}"]
                continue
            tags = [t if isinstance(t, str) else t.get("title") for t in (d.get("tags") or [])]
            lines = [t for t in tags if t and t.startswith("SMS Line ")]
            o = d.get("owner") or {}
            el.append({"uuid": u, "line": lines[0][len("SMS Line "):] if len(lines) == 1 else "?",
                       "owner_uuid": o.get("uuid"),
                       "best_phone": _best_primary({p["number"]: p for p in (o.get("phones") or [])})})
            details[u] = d
        # Fail closed when the exclusion tags can't be confirmed: a renamed tag would match nothing.
        if not marker["problem"]:
            marker["problem"] = review_tags_problem(api)
        idx = idx or OwnerIndex(api)
    except RuntimeError as e:
        marker["problem"] = marker["problem"] or f"batch or owner-level search failed: {str(e)[:80]}"
        idx = None
    numbers, owners = Counter(digits(r.get("best_phone")) for r in el), Counter(r.get("owner_uuid") for r in el)
    for rec in el:
        d = details[rec["uuid"]]
        try:
            problems = check(api, rec, batch_title, d)
            if not rec["best_phone"]:
                problems.append("no usable Dial First/Second mobile")
            if why := enroll_block(d):
                problems.append(why)
            if (d.get("owner") or {}).get("dnc") or (d.get("owner") or {}).get("opt_out"):
                problems.append("owner dnc/opt_out")
            if why := suppress_reason(d):
                problems.append(why)
            if idx and (why := idx.block(rec["uuid"], d)):
                problems.append(why)
            if rec["best_phone"] and numbers[digits(rec["best_phone"])] > 1:
                problems.append(f"primary ...{digits(rec['best_phone'])[-4:]} is on {numbers[digits(rec['best_phone'])]} batch records")
            if rec.get("owner_uuid") and owners[rec["owner_uuid"]] > 1:
                problems.append(f"owner has {owners[rec['owner_uuid']]} batch records")
            stop = record_stop(api, rec["uuid"], d)
            if stop["hold"]:
                problems.append(f"STOP hold ({stop['why']})")
            if digits((d.get("owner") or {}).get("primary_phone")) in stop["blocked"]:
                problems.append("primary DNC")
            if drip_rows(api, rec["uuid"]):
                problems.append("has drip history")
        except RuntimeError as e:
            unread.append(rec["uuid"])
            problems = [f"unreadable: {str(e)[:80]}"]
        if problems:
            marker["excluded"][rec["uuid"]] = problems
        else:
            marker["uuids"].append(rec["uuid"])
            marker["lines"][rec["uuid"]] = f"SMS Line {rec['line']}"
    per_line = Counter(marker["lines"].values())
    if not marker["problem"]:
        if unread:
            marker["problem"] = f"{len(unread)} records unreadable"
        elif idx and idx.unreadable:
            marker["problem"] = f"{idx.unreadable} records behind the owner-level check unreadable"
        elif max(per_line.values(), default=0) > LINE_CAP:
            marker["problem"] = f"line load {dict(per_line)} exceeds {LINE_CAP}"
        elif not marker["uuids"]:
            marker["problem"] = "no record passed validation"
    marker["ok"] = not marker["problem"]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"topup_ready_{batch_date}.json").write_text(json.dumps(marker, indent=1), encoding="utf-8")
    head = (f":white_check_mark: Monday {batch_date} preview: {len(marker['uuids'])} records ready to start at 9:50 AM CT"
            if marker["ok"] else f":x: Monday {batch_date} will NOT launch: {marker['problem']}")
    out = [head, "  By line (ceiling; Monday starts fewer if a line has less room): " +
           (", ".join(f"{k[4:]} {v}" for k, v in sorted(per_line.items())) or "none")]
    for u, why in list(marker["excluded"].items())[:15]:
        out.append(f"  EXCLUDED {u[:8]}: {', '.join(why)}")
    return marker, "\n".join(out)


def kpi_week(api: Sift, batch_date: str) -> str:
    """Weekly KPI section for the Mon-Fri before `batch_date` (Slack only)."""
    try:
        import kpi_scorecard
        mon = dt.date.fromisoformat(batch_date) - dt.timedelta(days=7)
        days = {d: load_day(api, d, CALLER_EMAIL) for d in ((mon + dt.timedelta(days=i)).isoformat() for i in range(5))}
        return "\n" + kpi_scorecard.weekly_lines(days, CALLER_EMAIL)
    except Exception as e:
        return f"\n(KPI scorecard failed: {e})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("plan", help="dry run: build Monday's batch and report it")
    pa = sub.add_parser("apply", help="build the batch and write it (only with --live)")
    pv = sub.add_parser("validate", help="re-read the written batch and write Monday's readiness marker")
    for s in (pp, pa, pv):
        s.add_argument("--batch-date", help="Monday the batch starts (default: next Monday, Central)")
        s.add_argument("--target", type=int, default=DEFAULT_TARGET)
        s.add_argument("--slack", action="store_true", help="post the report to Slack and Google Chat")
    pa.add_argument("--live", action="store_true", help="actually write to DataSift")
    pa.add_argument("--force", action="store_true", help="write even if this Monday's Batch tag exists")
    for s in (pa, pv):
        s.add_argument("--only-between", help="HH:MM-HH:MM Central; do nothing outside it")
    for s in (pp, pa):
        s.add_argument("--uuids-file", help="CSV (property_uuid, line) of a reviewed batch: take exactly those")
    args = ap.parse_args()

    if getattr(args, "only_between", None) and not _in_window(args.only_between):
        print(f"Outside window {args.only_between} Central (now {ct_now():%H:%M}); skipping.")
        return 0
    live = args.cmd == "apply" and args.live
    api = Sift(_env("REISIFT_API_KEY") or sys.exit("REISIFT_API_KEY missing"))
    batch_date = args.batch_date or next_monday(ct_now().date()).isoformat()
    if args.cmd == "validate":
        marker, text = validate(api, batch_date)
        print(text)
        if args.slack:
            _slack(text)
            _google_chat(text)
        return 0 if marker["ok"] else 1
    p = plan(api, args.target, pinned=load_pinned(args.uuids_file) if args.uuids_file else None)
    exists = f"Batch {batch_date}" in tag_uuids(api)
    text = report(p, batch_date, args.target, live=live, batch_exists=exists,
                  force=getattr(args, "force", False))
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
        _slack(text + kpi_week(api, batch_date))
        _google_chat(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
