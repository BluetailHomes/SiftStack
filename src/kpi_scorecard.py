"""Rahaf's KPI scorecard (built 2026-10-26).

Daily lines go at the end of the 7:15 PM audit; the weekly section goes in
the Sunday top-up report (Slack only). Everything here reads the per-record
day models that pm_text_sender.load_day() already builds, so Rahaf does no
extra reporting.

Targets stay at the floors until two clean weeks give a baseline. A missed
floor is shown as a gap, never lowered. Conversations and qualified leads
are coached numbers, not effort failures.

Dial counts here are what DataSift's record search surfaces, not the true
smrtPhone total (KPI audit 2026-10-27: live posts ran 3-33% low, re-runs for
a past day far lower), so they are labelled provisional. One call is logged on
every record of the dialed owner, so dials are counted once per call.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

DIAL_FLOOR = 150
CONV_FLOOR, CONV_GOAL = 10, 20          # calls >= 60s per day
QUAL_FLOOR, QUAL_GOAL = 1, 5            # qualified leads per week (proxy)
CONV_RATE_GOAL = 0.12                   # weekly, rising to 0.20
CONV_RATE_MIN_CN = 100                  # judge the rate only past this many CN calls
BLOCKS = ("10:15", "14:00", "16:45")    # dialing blocks, Central
BLOCK_SLACK_MIN = 45
CORRECT = {"CORRECT", "CORRECT_DNC"}

# Seller-advanced tiers, highest first. Status slugs as used in the account
# (legacy_archive.ACTIVE), compared after _slug() because the account mixes
# "warm lead" and "Warm Lead"; a record counts once, at its highest tier reached.
TIERS = (
    ("Offer made / under contract", {"underwrite", "push_deal", "under_contract", "transactions"}),
    ("New / warm / hot lead", {"new_lead", "warm_lead", "hot_lead"}),
    ("Callback scheduled", {"follow_up"}),
)
QUALIFIED_TIERS = ("Offer made / under contract", "New / warm / hot lead")
# End-of-day marks that mean nobody worth talking to answered.
NOT_A_CONVERSATION = {"NO_ANSWER", "WRONG", "DEAD", "WRONG_DNC"}
CONV_LIST_MAX = 10
RECORD_URL = "https://app.reisift.io/records/properties/{}"   # login required, unlike the recording URLs
COACH_DIR = Path(__file__).resolve().parent.parent / "output" / "call_coaching"
COACH_TYPES = ("cold_call", "lead_management", "closer")


def _slug(status) -> str:
    return str(status or "").strip().lower().replace(" ", "_").replace("-", "_")


def _tier(status) -> int | None:
    s = _slug(status)
    return next((i for i, (_, slugs) in enumerate(TIERS) if s in slugs), None)


def _mark_at_end(m: dict, number: str) -> str | None:
    marks = [x for x in m["marks"] if x["phone"] == number]
    if marks:
        return marks[-1]["to"]
    return (m["phones"].get(number) or {}).get("status")


def _call_key(c: dict):
    # `id` (smrtPhone external_id) when load_day provides it; otherwise from/to/second,
    # which matched external_id exactly on every day of Oct 19-27.
    return c.get("id") or (c["from"], c["to"], c["ct"])


def _call_groups(models: list[dict], caller: str) -> list[list[tuple[dict, dict]]]:
    """Outbound calls by the caller, one group per call, each listing every record it was logged on."""
    groups: dict = {}
    for m in models:
        for c in m["calls"]:
            if c["by"] == caller and c["dir"] == "outbound":
                groups.setdefault(_call_key(c), []).append((m, c))
    return sorted(groups.values(), key=lambda g: g[0][1]["ct"])


def _dials(models: list[dict], caller: str) -> list[tuple[dict, dict]]:
    return [g[0] for g in _call_groups(models, caller)]


def conversations(models: list[dict], caller: str, min_sec: int = 60) -> list[dict]:
    """Answered calls of min_sec+ not ending as voicemail/wrong/dead; unmarked ones are kept and flagged."""
    out = []
    for g in _call_groups(models, caller):
        m, c = g[0]
        if c["result"] != "answered" or (c["dur"] or 0) < min_sec:
            continue
        marks = [_mark_at_end(mm, cc["to"]) for mm, cc in g]
        if any(x in NOT_A_CONVERSATION for x in marks):
            continue
        mark = next((x for x in marks if x not in (None, "", "UNKNOWN")), None)
        out.append({"ct": c["ct"], "uuid": m["uuid"], "street": m.get("street") or "", "to": c["to"],
                    "dur": c["dur"], "mark": mark, "id": c.get("id")})
    return out


def daily_kpis(models: list[dict], caller: str) -> dict:
    groups = _call_groups(models, caller)
    dials = [g[0] for g in groups]
    cn = [g[0] for g in groups if g[0][1]["result"] == "answered" and any(_mark_at_end(m, c["to"]) in CORRECT for m, c in g)]
    blocks = {}
    for b in BLOCKS:
        h, mi = map(int, b.split(":"))
        centre = h * 60 + mi
        blocks[b] = any(abs(c["ct"].hour * 60 + c["ct"].minute - centre) <= BLOCK_SLACK_MIN for _, c in dials)
    # New-number marking: numbers dialed today that started the day unmarked, once per number
    # (the same number sits on every record of its owner; marked on any of them counts).
    seen: dict[str, bool] = {}          # number -> marked by end of day
    for m in models:
        for number in {c["to"] for c in m["calls"] if c["by"] == caller and c["dir"] == "outbound"}:
            marks = [x for x in m["marks"] if x["phone"] == number]
            start = marks[0]["frm"] if marks else (m["phones"].get(number) or {}).get("status")
            if start in (None, "", "UNKNOWN"):
                end = marks[-1]["to"] if marks else None
                seen[number] = seen.get(number, False) or end not in (None, "", "UNKNOWN")
    new, marked = len(seen), sum(seen.values())
    # Records that reached a qualified tier today (status changes are already day-scoped by load_day).
    qualified = {m["uuid"] for m in models for s in m["status_changes"]
                 if _tier(s["to"]) is not None and TIERS[_tier(s["to"])][0] in QUALIFIED_TIERS}
    return {
        "dials": len(dials), "rows": sum(len(g) for g in groups), "cn": len(cn),
        "conv60": sum((c["dur"] or 0) >= 60 for _, c in cn), "conv120": sum((c["dur"] or 0) >= 120 for _, c in cn),
        "blocks": blocks, "new_numbers": new, "new_marked": marked, "qualified": len(qualified),
    }


def _pct(a: int, b: int) -> str:
    return f"{a / b:.0%}" if b else "n/a"


def _warnings(models: list[dict], caller: str, k: dict, day: dt.date | None,
              stats: dict | None, today: dt.date | None) -> list[str]:
    out = []
    calls = [c for m in models for c in m["calls"] if c["by"] == caller]
    if day is None and calls:
        day = calls[0]["ct"].date()
    if today is None:
        tz = calls[0]["ct"].tzinfo if calls else None
        today = dt.datetime.now(tz).date() if tz else dt.date.today()
    if day is not None and day < today:
        out.append(f"re-run for past day {day}: DataSift record search drops many calls after the fact; "
                   "these counts are too low and re-running will not fix them - use smrtPhone for this day")
    else:
        out.append("calls from the last hour or two may not be visible yet; smrtPhone is the full count")
    if stats and stats.get("skipped"):
        out.append(f"{stats['skipped']} records skipped after API errors/rate limits - counts are incomplete")
    if stats and stats.get("errors"):
        out.append(f"{stats['errors']} source query errors - counts are incomplete")
    return out


def _conv_lines(models: list[dict], caller: str) -> list[str]:
    convs = conversations(models, caller)
    if not convs:
        return ["• Meaningful answered calls (≥60s, not voicemail/wrong/dead): none visible"]
    out = [f"• Meaningful answered calls (≥60s, not voicemail/wrong/dead): {len(convs)}"]
    for v in convs[:CONV_LIST_MAX]:
        mark = v["mark"] or "UNMARKED - mark it"
        ref = f"call {v['id']}" if v["id"] else f"<{RECORD_URL.format(v['uuid'])}|record>"
        out.append(f"   {v['ct']:%I:%M %p} · {v['street'] or '?'} · …{v['to'][-4:]} · {v['dur']}s · {mark} · {ref}")
    if len(convs) > CONV_LIST_MAX:
        out.append(f"   …and {len(convs) - CONV_LIST_MAX} more")
    return out


def daily_lines(models: list[dict], caller: str, day: dt.date | None = None,
                stats: dict | None = None, today: dt.date | None = None) -> str:
    """`stats` (optional, from load_day): {"selected", "skipped", "errors"}."""
    k = daily_kpis(models, caller)
    gaps = []
    if k["dials"] < DIAL_FLOOR:
        gaps.append(f"dials {k['dials']}/{DIAL_FLOOR}")
    if k["conv60"] < CONV_FLOOR:
        gaps.append(f"conversations {k['conv60']}/{CONV_FLOOR} (coached, not effort)")
    missed = [b for b, hit in k["blocks"].items() if not hit]
    if missed:
        gaps.append("blocks missed " + ", ".join(missed))
    if k["new_numbers"] and k["new_marked"] < k["new_numbers"]:
        gaps.append(f"{k['new_numbers'] - k['new_marked']} new numbers left unmarked")
    dupes = k["rows"] - k["dials"]
    out = [
        "", "*KPI scorecard (daily)*",
        f"• DataSift-visible dials {k['dials']} (provisional, not the smrtPhone total; floor {DIAL_FLOOR})"
        + (f" · {dupes} duplicate log rows on other records not counted" if dupes else "")
        + f" · CN contacts {k['cn']} · contact rate {_pct(k['cn'], k['dials'])}",
        f"• Conversations ≥60s {k['conv60']} (floor {CONV_FLOOR}, goal {CONV_GOAL}) · ≥120s {k['conv120']} · "
        f"conversation rate {_pct(k['conv60'], k['cn'])}",
        f"• Leads advanced to new/warm/hot or offer today: {k['qualified']}",
        *_conv_lines(models, caller),
        "• Dialing blocks: " + "  ".join(f"{b} {'✓' if hit else '✗'}" for b, hit in k["blocks"].items()),
        f"• New-number marking {k['new_marked']}/{k['new_numbers']} ({_pct(k['new_marked'], k['new_numbers'])})",
        "• Gaps: " + ("; ".join(gaps) if gaps else "none"),
    ]
    out += [f"⚠️ {w}" for w in _warnings(models, caller, k, day, stats, today)]
    return "\n".join(out)


def weekly_kpis(days: dict[str, list[dict]], caller: str) -> dict:
    """`days` maps ISO date -> that day's models (Mon-Fri)."""
    cn = conv60 = 0
    best: dict[str, int] = {}           # record uuid -> best tier index
    tasked, engaged = set(), set()
    photos = offers = 0
    for models in days.values():
        k = daily_kpis(models, caller)
        cn += k["cn"]
        conv60 += k["conv60"]
        for m in models:
            for s in m["status_changes"]:
                i = _tier(s["to"])
                if i is not None:
                    best[m["uuid"]] = min(best.get(m["uuid"], i), i)
            if any(c["by"] == caller and c["result"] == "answered" and (c["dur"] or 0) >= 60 for c in m["calls"]):
                engaged.add(m["uuid"])
                if m.get("open_tasks"):
                    tasked.add(m["uuid"])
            photos += sum(1 for t, _ in m["tags_added"] if "photo" in t.lower())
            offers += sum(1 for t, _ in m["tags_added"] if t.lower() in ("offer_pending", "offer made"))
    advanced = {label: sum(1 for v in best.values() if v == i) for i, (label, _) in enumerate(TIERS)}
    return {"cn": cn, "conv60": conv60, "advanced": advanced, "engaged": len(engaged),
            "engaged_tasked": len(tasked & engaged), "photos": photos, "offers": offers}


def latest_coaching(kind: str, folder: Path | None = None) -> tuple[str, dict] | None:
    folder = folder or COACH_DIR
    files = sorted(folder.glob(f"rahaf_{kind}_coaching_*.xlsx"))
    if not files:
        return None
    from openpyxl import load_workbook
    ws = load_workbook(files[-1], read_only=True, data_only=True)["Caller Scorecard"]
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return None
    return files[-1].stem.rsplit("_", 1)[-1], dict(zip(rows[0], rows[1]))


def weekly_lines(days: dict[str, list[dict]], caller: str, today: dt.date | None = None) -> str:
    k = weekly_kpis(days, caller)
    qual = sum(k["advanced"][t] for t in QUALIFIED_TIERS)
    # Tags are the only structured signal and are rarely applied, so zero means "not tracked", not "none".
    tracked = lambda n: f"at least {n} (tag-based; notes not counted)" if n else "not tracked yet"
    rate = (f"{k['conv60'] / k['cn']:.0%} (goal {CONV_RATE_GOAL:.0%})" if k["cn"] >= CONV_RATE_MIN_CN
            else f"not judged yet ({k['cn']}/{CONV_RATE_MIN_CN} CN calls)")
    out = ["", "*KPI scorecard (week)*",
           f"• Conversation rate: {rate}",
           "• Sellers advanced: " + ", ".join(f"{n} {lbl}" for lbl, n in k["advanced"].items()),
           f"• Qualified leads (proxy: new/warm/hot lead or better) {qual} (floor {QUAL_FLOOR}, goal {QUAL_GOAL})"
           + ("" if qual >= QUAL_FLOOR else " - gap, coached"),
           f"• Photos received: {tracked(k['photos'])} · offers presented: {tracked(k['offers'])}",
           "• Counts come from DataSift-visible activity only; the week's dials/CN are provisional",
           f"• Engaged sellers with a dated task {k['engaged_tasked']}/{k['engaged']}"]
    for kind in COACH_TYPES:
        got = latest_coaching(kind)
        if got:
            date, row = got
            out.append(f"• Coaching ({kind.replace('_', ' ')}, {date}): avg {row.get('Avg Total')} · "
                       f"weakest {row.get('Recurring Lowest Category')}")
    return "\n".join(out)
