"""Rahaf's morning KPI report in Google Chat (Phase 1, built 2026-10-28).

Posts the previous business day's numbers, her 60+ second calls and today's
follow-ups to the "Bluetail | Daily KPIs & Coaching" space at 9:50 AM Central
(before her 10:00 shift; Monday covers Friday). Recording-based coaching is
Phase 2 and is not here.

Why a snapshot: DataSift's record search drops calls after the fact, so the
morning job cannot rebuild yesterday from DataSift (KPI audit 2026-10-27). The
7:15 PM audit calls write_snapshot() with the models it already loaded, and the
file rides in that run's `audit-<run_id>` artifact. Runner disks do not persist,
so the morning workflow downloads recent artifacts into --snapshots/--markers
before running this.

Delivery safety:
- dry run unless --send; --send needs GOOGLE_CHAT_KPI_WEBHOOK_URL (never printed)
- one report per day: every run writes kpi_report_<day>.json to --out (uploaded as
  the run's artifact) with status attempted / posted / failed / uncertain /
  skipped / dry_run. "attempted" is written before the post, so a run that dies
  mid-post still leaves a record. A downloaded posted, uncertain or attempted
  marker blocks a repeat (the message is, or may be, in the space); failed,
  skipped and dry_run never block a retry. A timeout after the request was sent
  is "uncertain" and is not retried automatically. --force re-posts
- a missing snapshot sends Rahaf one brief notice (kpi_notice_<day>.json, same
  statuses, never repeated); every technical detail goes to internal Slack only
- a day with no recorded activity (holiday, day off, data gap) is shown neutrally,
  never as missed targets
- nothing here posts phone numbers beyond the last 4, owner names, recording
  URLs, transcripts or task notes
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import kpi_scorecard as k
import pm_text_sender as p

SNAPSHOT_VERSION = 1
OUT_DIR = p.ROOT / "output" / "kpi_report"
CHAT_URL_ENV = "GOOGLE_CHAT_KPI_WEBHOOK_URL"
CHAT_MAX_CHARS = 3800          # Google Chat caps a text message at 4,096 characters
FOLLOWUP_LIST_MAX = 10
POST_TRIES = 3
DELAYED_NOTICE = ("Good morning, Rahaf. Today's KPI report is delayed because yesterday's data isn't "
                  "available yet. That's a system issue, not a reflection of your work. Kaycie has been "
                  "told, and there is nothing you need to do. Start your day as usual.")


# ----------------------------------------------------------------- snapshot (written by the audit)
def build_snapshot(models: list[dict], caller: str, day: str, stats: dict | None = None,
                   now: dt.datetime | None = None) -> dict:
    """Everything the morning report needs from the audit's models, with phones cut to the last 4."""
    now = now or p.ct_now()
    kp = k.daily_kpis(models, caller)
    calls = [{"time": f"{v['ct']:%I:%M %p}", "street": v["street"], "last4": (v["to"] or "")[-4:],
              "dur": v["dur"], "mark": v["mark"], "id": v["id"], "uuid": v["uuid"]}
             for v in k.conversations(models, caller)]
    return {"version": SNAPSHOT_VERSION, "day": day, "caller": caller,
            "generated_at": now.isoformat(timespec="seconds"), "live": now.date().isoformat() == day,
            "stats": dict(stats or {}), "kpis": kp, "calls60": calls}


def write_snapshot(models: list[dict], caller: str, day: str, stats: dict | None = None,
                   folder: Path | None = None) -> Path:
    """The audit's one-line hook. `folder` should be inside the audit's uploaded artifact path."""
    folder = Path(folder or p.STATE_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"kpi_snapshot_{day}.json"
    path.write_text(json.dumps(build_snapshot(models, caller, day, stats)), encoding="utf-8")
    return path


def find_snapshot(folder: Path, day: str) -> dict | None:
    """Best snapshot for `day` among downloaded artifacts: a live one (written that evening)
    over a later re-run, then the newest. Unreadable or wrong-version files are skipped."""
    found = []
    for f in sorted(Path(folder).rglob(f"kpi_snapshot_{day}.json")) if Path(folder).exists() else []:
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(s, dict) and s.get("version") == SNAPSHOT_VERSION and s.get("day") == day and isinstance(s.get("kpis"), dict):
            found.append(s)
    if not found:
        return None
    return max(found, key=lambda s: (bool(s.get("live")), s.get("generated_at") or ""))


# ----------------------------------------------------------------- markers
BLOCKING = ("posted", "uncertain", "attempted")    # the message is, or may be, in the space


def _marker_block(folders: list[Path], name: str) -> str | None:
    """The status of any marker that blocks a repeat post, else None."""
    for folder in folders:
        for f in Path(folder).rglob(name) if Path(folder).exists() else []:
            try:
                status = json.loads(f.read_text(encoding="utf-8")).get("status")
            except (ValueError, OSError, AttributeError):
                continue
            if status in BLOCKING:
                return status
    return None


def _write_marker(out: Path, name: str, day: str, status: str, **extra) -> None:
    out.mkdir(parents=True, exist_ok=True)
    if status in ("skipped", "dry_run") and _marker_block([out], name):
        return      # a reused local --out folder: never overwrite the record of a real post
    (out / name).write_text(json.dumps({"day": day, "status": status,
                                        "at": p.ct_now().isoformat(timespec="seconds"), **extra}), encoding="utf-8")


def _deliver(text: str, url: str, post, out: Path, name: str, day: str) -> str:
    """One delivery with "attempted" on disk first. Returns posted / failed / uncertain."""
    _write_marker(out, name, day, "attempted")
    res = post(text, url)
    res = "posted" if res is True else "failed" if res is False else res
    _write_marker(out, name, day, res, chars=len(text))
    return res


def report_marker(day: str) -> str:
    return f"kpi_report_{day}.json"


def notice_marker(day: str) -> str:
    return f"kpi_notice_{day}.json"


# ----------------------------------------------------------------- follow-ups (DataSift tasks, read live)
def load_followups(api, caller: str, today: dt.date) -> dict:
    """Open tasks assigned to `caller`, read from /api/internal/task/ (verified 2026-10-28: it
    honours completed=false and ignores other filters, so the rest is filtered here).
    Returns {"ok": False, "why": ...} unless every page was read and the total matches the
    API's count, so a partial read is never shown as "no follow-ups"."""
    rows, off, count = [], 0, None
    try:
        for _ in range(50):
            r = api.call(f"/api/internal/task/?limit=100&offset={off}&completed=false")
            count = r.get("count")
            rows += r.get("results") or []
            off += 100
            if not r.get("next"):
                break
        else:
            return {"ok": False, "why": f"task list did not finish paging ({len(rows)} of {count})"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "why": f"task API error: {type(e).__name__}: {str(e)[:120]}"}
    if not isinstance(count, int) or count != len(rows) or len({t.get("uuid") for t in rows}) != len(rows):
        return {"ok": False, "why": f"task list incomplete ({len(rows)} read, API count {count})"}
    try:
        presets = {t.get("title") for t in (api.call("/api/internal/task-preset/?limit=100") or {}).get("results") or []}
    except Exception:  # noqa: BLE001
        presets = set()
    mine = []
    for t in rows:
        if t.get("completed") or ((t.get("assigned_to_user") or {}).get("email") != caller):
            continue
        due = p.to_ct(t.get("due_date") or "")
        mine.append({"due": due, "all_day": bool(t.get("all_day")), "uuid": t.get("assigned_to_property"),
                     # A hand-typed title can hold a seller's name; only task-preset titles are shown.
                     "title": t.get("title") if t.get("title") in presets else "Follow-up"})
    due_today = sorted((t for t in mine if t["due"] and t["due"].date() == today), key=lambda t: t["due"])
    overdue = sorted((t for t in mine if t["due"] and t["due"].date() < today), key=lambda t: t["due"], reverse=True)
    show = (due_today + overdue)[:FOLLOWUP_LIST_MAX]
    for t in show:
        try:
            det = api.call(f"/api/internal/property/{t['uuid']}/") if t["uuid"] else {}
            t["street"] = ((det or {}).get("address") or {}).get("street") or ""
        except Exception:  # noqa: BLE001
            t["street"] = ""
    return {"ok": True, "today": len(due_today), "overdue": len(overdue),
            "oldest": overdue[-1]["due"].date() if overdue else None,
            "tomorrow": sum(1 for t in mine if t["due"] and t["due"].date() == today + dt.timedelta(days=1)),
            "no_due": sum(1 for t in mine if not t["due"]), "show": show}


# ----------------------------------------------------------------- offers made (structured notes, read live)
# Kaycie's definition (2026-10-27): an offer counts when an approved, specific price or range
# is actually communicated to the seller or their representative. Captured as one DataSift
# property note per presentation, first line:
#     OFFER | INITIAL | $75,000 | VERBAL | APPROVED: Kaycie
#     OFFER | REVISED | $80,000 | TEXT | APPROVED: Kaycie
#     OFFER | WRITTEN FOLLOW-UP | $75,000 | WRITTEN | APPROVED: Kaycie
# Kaycie authorizes a price with her own note on the property, before it is presented:
#     PRICE APPROVED | $75,000 | Kaycie
# A note's "APPROVED: Kaycie" is only a claim, and neither note alone shows both events. An
# offer counts only when a Kaycie-written PRICE APPROVED note on the same property, at exactly
# the same price or range, was recorded before it; otherwise it waits for her review. A
# revised price needs its own approval; repeating an approved price needs none, and is never
# counted again. Nothing here ever sets or suggests a price.
# Channel (Kaycie, 2026-10-28): VERBAL = a live conversation. VOICEMAIL is recorded as offer
# outreach, never counted (the seller may not have heard it) and doesn't make a later live
# presentation a repeat. TEXT counts only when DataSift logged the text going out with that
# price and it didn't fail; WRITTEN only when the note has a "SENT:" line saying how and when.
# A mistake is fixed by a NEW note (OFFER line + a "CORRECTION" line) that replaces the offer
# note just before it on that property. Both stay in DataSift, and the correction is judged at
# the original's time, so a later approval never authorizes an earlier offer.
OFFER_DAY_GOAL, OFFER_WEEK_GOAL = 10, 50
OFFERS_TRACKED_FROM = dt.date(2026, 10, 30)    # first day of structured capture; earlier = "Not tracked"
REVIEW_LOOKBACK_DAYS = 14                      # open review items are re-listed this long
REVIEW = ("malformed", "not_kaycie", "unverified", "send_unconfirmed", "voicemail_unverified", "followup_new_price")
APPROVER_NAME, APPROVER_EMAIL = "kaycie", "kaycie@bluetailrealestate.com"
TEXT_WINDOW = (dt.timedelta(hours=12), dt.timedelta(hours=1))   # sent text up to 12h before / 1h after the note
_AMT = r"\$?\s?(\d[\d,]*(?:\.\d{1,2})?)\s?(k)?"
_OFFER = re.compile(rf"^OFFER\s*\|\s*(INITIAL|REVISED|WRITTEN\s+FOLLOW-?UP)\s*\|\s*{_AMT}\s*(?:(?:-|–|—|to)\s*{_AMT})?\s*\|"
                    r"\s*(VERBAL|VOICEMAIL|TEXT|WRITTEN)\s*\|\s*APPROVED:\s*([A-Za-z][A-Za-z .'-]*?)\s*$", re.I)
_APPROVAL = re.compile(rf"^PRICE\s+APPROVED\s*\|\s*{_AMT}\s*(?:(?:-|–|—|to)\s*{_AMT})?\s*(?:\|\s*[A-Za-z][A-Za-z .'-]*)?$", re.I)
_DOLLARS = re.compile(r"\$\s?\d{2,3}(?:,\d{3})+|\$\s?\d{2,4}\s?k\b", re.I)


def _amount(num: str | None, k_: str | None) -> int | None:
    if not num:
        return None
    v = float(num.replace(",", "")) * (1000 if k_ else 1)
    return int(round(v)) if 1_000 <= v <= 10_000_000 else None


def _amounts(body: str) -> list[int]:
    """Dollar amounts in a text message ("$75,000", "$75k")."""
    out = []
    for m in _DOLLARS.finditer(body or ""):
        s = m.group(0).lstrip("$").strip().lower()
        v = _amount(s.rstrip("k").strip(), "k" if s.endswith("k") else None)
        if v:
            out.append(v)
    return out


def parse_offer_note(text: str) -> dict | None:
    """None when the note is not an OFFER/PRICE APPROVED note. {"kind": "malformed"} when it
    starts like one but does not parse, so it can be flagged instead of guessed at."""
    first = (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""
    up = first.upper()
    if not (up.startswith("OFFER") and "|" in first) and not up.startswith("PRICE APPROVED"):
        return None
    m = _OFFER.match(first)
    if m:
        kind, lo, hi = re.sub(r"\s+", " ", m.group(1).upper()).replace("FOLLOWUP", "FOLLOW-UP"), _amount(m.group(2), m.group(3)), _amount(m.group(4), m.group(5))
        if lo and (m.group(4) is None or (hi and hi > lo)):
            rest = [ln.strip().upper() for ln in text.strip().splitlines()[1:]]
            out = {"kind": kind, "low": lo, "high": hi or lo, "method": m.group(6).upper(), "approved": m.group(7).strip()}
            if any(ln.startswith("CORRECTION") for ln in rest):
                out["correction"] = True
            if any(re.match(r"SENT\s*:\s*\S", ln) for ln in rest):
                out["sent_line"] = True
            return out
    m = _APPROVAL.match(first)
    if m:
        lo, hi = _amount(m.group(1), m.group(2)), _amount(m.group(3), m.group(4))
        if lo and (m.group(3) is None or (hi and hi > lo)):
            return {"kind": "PRICE APPROVED", "low": lo, "high": hi or lo}
    return {"kind": "malformed"}


def _note(e: dict) -> dict | None:
    """An OFFER-related note from a property log row, or None."""
    if e.get("event_type") not in ("property.message.added", "property.message.updated"):
        return None
    pl = e.get("payload") or {}
    text = pl.get("message") if isinstance(pl.get("message"), str) else (pl.get("message_obj") or {}).get("message")
    if isinstance(text, list):          # an edit logs [old text, new text]
        text = text[-1] if text and isinstance(text[-1], str) else ""
    parsed = parse_offer_note(text or "")
    if parsed is None:
        return None
    return {"ts": p.to_ct(e.get("timestamp")), "author": ((e.get("author_extra_info") or {}).get("email") or e.get("author") or "").lower(),
            "property": e.get("resource") if e.get("resource_type", "property") == "property" else None,
            "street": (((pl.get("extra_info") or {}).get("property") or {}).get("address") or {}).get("street") or "",
            "edited": e.get("event_type") == "property.message.updated", "id": e.get("id"), **parsed}


def _text_sent(n: dict, texts: list[dict] | None) -> bool:
    """A logged, not-failed outgoing text with this offer's price(s), near the note's time."""
    lo = n["ts"] - TEXT_WINDOW[0]
    hi = max(n["ts"], n.get("logged_ts") or n["ts"]) + TEXT_WINDOW[1]
    want = {n["low"], n["high"]}
    return any(t["ts"] and lo <= t["ts"] <= hi and want <= set(t["amounts"]) and not t.get("failed") for t in texts or [])


def _apply_corrections(ns: list[dict]) -> list[dict]:
    """Each valid correction note replaces the offer note just before it on the property (a
    correction of a correction keeps the first note's time). Returns the replaced originals,
    which stay as audit events with status "corrected"; the notes themselves are untouched in DataSift."""
    replaced, last = [], None
    for n in ns:
        if n["kind"] == "PRICE APPROVED":
            continue
        if n.get("correction") and last is not None:
            n["logged_ts"], n["ts"], n["corrects"] = n["ts"], last["ts"], last.get("id")
            replaced.append(last)
        last = n
    return replaced


def classify_offers(notes: list[dict], texts: list[dict] | None = None, texts_since: dt.date | None = None,
                    approver_email: str = APPROVER_EMAIL) -> tuple[list[dict], list[tuple]]:
    """Apply the counting rules to every OFFER-related note on the properties involved (full
    history, so a price repeated from weeks ago is not new). `texts` are the logged outgoing
    texts from `texts_since` on; TEXT offers older than that aren't checked (no log was read).
    Returns (offer events, [(time, flag)])."""
    flags, events, by_prop = [], [], {}
    for n in notes:
        if n.get("edited"):
            flags.append((n["ts"], f"an offer note on {_ref(n)} was edited on {_d(n['ts'].date())}; edits are not re-read, "
                                   "check the count (fix mistakes with a new CORRECTION note, not an edit)"))
            continue
        by_prop.setdefault(n.get("property"), []).append(dict(n))
    for prop, ns in by_prop.items():
        ns.sort(key=lambda n: n["ts"])
        approvals = []
        for n in ns:
            if n["kind"] == "PRICE APPROVED":
                if n["author"] == approver_email:
                    approvals.append((n["ts"], n["low"], n["high"]))
                else:
                    flags.append((n["ts"], f"PRICE APPROVED note on {_ref(n)} was written by {n['author'] or 'unknown'}, not Kaycie; ignored"))
        replaced = _apply_corrections(ns)
        events += [dict(o, status="corrected", category="") for o in replaced]
        presented: set[tuple[int, int]] = set()
        for n in sorted((n for n in ns if n["kind"] != "PRICE APPROVED" and not any(n is o for o in replaced)), key=lambda n: n["ts"]):
            ev = dict(n, status="", category="")
            events.append(ev)

            def flag(status: str, msg: str) -> None:
                ev["status"] = status
                flags.append((ev["ts"], msg))
            if n.get("logged_ts"):
                flags.append((n["logged_ts"], f"offer note on {_ref(n)} from {_d(n['ts'].date())} was corrected by {n['author']} on "
                                              f"{_d(n['logged_ts'].date())}; both notes kept, judged at the original time"))
            if n["kind"] == "malformed":
                flag("malformed", f"offer note on {_ref(n)} ({_d(n['ts'].date())}, by {n['author']}) does not match the format; "
                                  "not counted until corrected")
                continue
            if n["approved"].split()[0].lower() != APPROVER_NAME:
                flag("not_kaycie", f"offer note on {_ref(n)} claims approval by {n['approved']!r}; only Kaycie approves prices, not counted")
                continue
            key = (n["low"], n["high"])
            same = [ts for ts, lo, hi in approvals if (lo, hi) == key]
            verified = any(ts <= n["ts"] for ts in same)
            why = (f"Kaycie's PRICE APPROVED note at that price was recorded after it ({min(same):%a %I:%M %p}); approval must come first"
                   if same else "there is no Kaycie PRICE APPROVED note on that property at exactly this price/range")
            if n["method"] == "VOICEMAIL":
                if verified:
                    ev["status"] = "voicemail"
                else:
                    flag("voicemail_unverified", f"voicemail with a price on {_ref(n)} by {n['author']}: {why}; Kaycie to review")
                continue
            if n["kind"] == "WRITTEN FOLLOW-UP":
                ev["status"] = "followup"
                if key not in presented:
                    flag("followup_new_price", f"written follow-up on {_ref(n)} is at a price not presented before; should it be INITIAL or REVISED? not counted")
                continue
            if key in presented:
                ev["status"] = "repeat"
                continue
            ev["category"] = "revised" if presented else "initial"
            if ev["category"].upper() != n["kind"]:
                flags.append((n["ts"], f"offer note on {_ref(n)} says {n['kind']} but is the property's {ev['category']} offer; "
                                       f"treated as {ev['category']}"))
            if not verified:
                flag("unverified", f"offer on {_ref(n)} by {n['author']} says APPROVED: Kaycie but {why}; not counted, Kaycie to review")
                presented.add(key)
                continue
            checked = texts_since is None or n["ts"].date() >= texts_since
            if n["method"] == "TEXT" and checked and not _text_sent(n, texts):
                flag("send_unconfirmed", f"TEXT offer on {_ref(n)} by {n['author']}: no logged, delivered text with that price near "
                                         f"{n['ts']:%a %I:%M %p}; not counted until confirmed or corrected")
                continue
            if n["method"] == "WRITTEN" and not n.get("sent_line"):
                flag("send_unconfirmed", f"WRITTEN offer on {_ref(n)} by {n['author']} has no 'SENT:' line (how and when it was sent); "
                                         "not counted until corrected")
                continue
            ev["status"] = "counted"
            presented.add(key)
    return events, flags


def _ref(n: dict) -> str:
    return f"<{k.RECORD_URL.format(n['property'])}|{n.get('street') or 'record'}>" if n.get("property") else (n.get("street") or "an unknown record")


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def _logs(api, path: str, stop_before: dt.date | None, max_pages: int):
    """Yield property-log rows newest first until `stop_before` (Central date) is passed.
    Raises RuntimeError when the page cap is hit first, so a partial read never looks complete."""
    for _ in range(max_pages):
        r = api.call(path) or {}
        rows = r.get("results") or []
        older = False
        for e in rows:
            t = p.to_ct(e.get("timestamp"))
            if stop_before and t is not None and t.date() < stop_before:
                older = True
                continue
            yield e
        cur, nxt = r.get("next_cursor"), r.get("next")
        if older or not rows or not (cur or nxt):
            return
        if cur:
            path = path.split("&cursor=")[0] + "&cursor=" + urllib.parse.quote(str(cur))
        else:
            off = int(re.search(r"offset=(\d+)", path).group(1)) + len(rows)
            path = re.sub(r"offset=\d+", f"offset={off}", path)
    raise RuntimeError(f"log read hit the {max_pages}-page cap")


def load_offers(api, since: dt.date, max_pages: int = 800) -> dict:
    """OFFER notes from `since` (Central) on, plus each involved property's full note history.
    Also returns the sent texts that mention a dollar amount ({ts, amounts, uuid, failed}): the
    evidence a TEXT offer went out, and the internal missing-note check."""
    try:
        recent, texts, failed = [], [], set()
        for e in _logs(api, "/api/internal/property/logs/?limit=100", since, max_pages):
            n = _note(e)
            sms = ((e.get("payload") or {}).get("sms") or {})
            if n:
                recent.append(n)
            elif e.get("event_type") == "owner.sms.updated":
                if re.search(r"fail|undeliver|error|reject", f"{sms.get('status') or ''} {sms.get('status_message') or ''}", re.I):
                    failed.add(sms.get("uuid"))
            elif e.get("event_type") == "owner.sms.sent":
                amounts = _amounts(sms.get("message") or "")
                if amounts:    # sent-text rows link to the owner, not the property; keep time + amounts only
                    texts.append({"ts": p.to_ct(e.get("timestamp")), "amounts": amounts, "uuid": sms.get("uuid"),
                                  "failed": bool(re.search(r"fail|undeliver|error|reject", str(sms.get("status") or ""), re.I))})
        for t in texts:
            t["failed"] = t["failed"] or (t["uuid"] is not None and t["uuid"] in failed)
        notes = []
        for prop in sorted({n["property"] for n in recent if n["property"]}):
            for e in _logs(api, f"/api/internal/property/{prop}/logs/?limit=250&offset=0", None, 40):
                n = _note(e)
                if n:
                    n["property"] = n["property"] or prop
                    notes.append(n)
        notes += [n for n in recent if not n["property"]]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "why": f"offer note read failed: {type(e).__name__}: {str(e)[:120]}"}
    events, flags = classify_offers(notes, texts, since)
    return {"ok": True, "events": events, "flags": flags, "texts": texts}


def offers_since(rday: dt.date) -> dt.date:
    """Log window: the report week, or further back (never before tracking) so open review items stay listed."""
    return min(week_start(rday), max(OFFERS_TRACKED_FROM, rday - dt.timedelta(days=REVIEW_LOOKBACK_DAYS)))


def summarize_offers(of: dict, day: dt.date, tracked_from: dt.date | None = None) -> dict:
    """Day and week-to-date numbers. A day before tracking began is "not tracked", a failed read
    "unavailable"; only a complete read of a tracked day shows 0."""
    tracked_from = tracked_from or OFFERS_TRACKED_FROM
    if not of.get("ok"):
        return {"state": "unavailable"}
    if day < tracked_from:
        return {"state": "not_tracked", "from": tracked_from}
    ws = week_start(day)

    def period(a: dt.date, b: dt.date) -> dict:
        ev = [e for e in of["events"] if e.get("ts") and a <= e["ts"].date() <= b and e["ts"].date() >= tracked_from]
        c = [e for e in ev if e["status"] == "counted"]
        return {"offers": len(c), "initial": sum(e["category"] == "initial" for e in c),
                "revised": sum(e["category"] == "revised" for e in c),
                "live": sum(e["method"] == "VERBAL" for e in c), "texts": sum(e["method"] == "TEXT" for e in c),
                "written": sum(e["method"] == "WRITTEN" for e in c),
                "properties": len({e["property"] for e in c}), "followups": sum(e["status"] == "followup" for e in ev),
                "voicemails": sum(e["status"] == "voicemail" for e in ev),
                "review": sum(e["status"] in REVIEW for e in ev),
                "presenters": dict(sorted({a_: sum(e["author"] == a_ for e in c) for a_ in {e["author"] for e in c}}.items()))}
    return {"state": "ok", "day": period(day, day), "week": period(ws, day), "week_start": ws,
            "partial_week": ws < tracked_from, "from": tracked_from}


def unlogged_text_check(of: dict, day: dt.date) -> str | None:
    """Internal hint, never a count: more dollar-amount texts went out on `day` than offer notes
    were logged. Text rows can't be tied to a property cheaply, so this compares totals only."""
    sent = sum(1 for t in of.get("texts") or [] if t["ts"] and t["ts"].date() == day and not t.get("failed"))
    logged = sum(1 for e in of.get("events") or [] if e.get("ts") and e["ts"].date() == day)
    if sent > logged:
        return (f"{sent} sent text(s) mentioned a dollar amount but only {logged} offer note(s) were logged; "
                f"if an approved price went out by text it needs an OFFER note (not counted)")
    return None


def _offer_lines(o: dict, day: dt.date, today: dt.date | None = None) -> list[str]:
    head = "*Offers Made* (team · approved prices presented to sellers)"
    if o["state"] == "unavailable":
        return [head, "• Unavailable this morning (a data issue, not a zero). Kaycie has been told."]
    if o["state"] == "not_tracked":
        when = "today" if today == o["from"] else _d(o["from"])
        return [head, f"• Not tracked yet. Tracking starts {when}: log every offer as an OFFER note right after you present it."]
    d, w = o["day"], o["week"]
    week_label = "Week final" if day.weekday() == 4 else "Week to date"
    week = (f"*{w['offers']}* (partial week: tracking began {_d(o['from'])}, so not compared to {OFFER_WEEK_GOAL})"
            if o["partial_week"] else f"*{w['offers']} / {OFFER_WEEK_GOAL}*")
    lines = [head, f"• {_d(day)}: *{d['offers']} / {OFFER_DAY_GOAL}* · {week_label}: {week}",
             f"• Initial {d['initial']} · Revised {d['revised']} · Live call {d['live']} · Text {d['texts']} · Written {d['written']} · "
             f"Properties {d['properties']} (week: {w['properties']})"]
    if w["voicemails"]:
        lines.append(f"• Voicemails with an approved price this week: {w['voicemails']} (outreach, not counted as offers)")
    if w["followups"]:
        lines.append(f"• Written follow-ups at an unchanged price this week: {w['followups']} (recorded, not counted again)")
    if w["review"]:
        lines.append(f"• Waiting for review or a correction this week: {w['review']} (not counted yet)")
    return lines


# ----------------------------------------------------------------- report
def _d(day: dt.date) -> str:
    return f"{day:%a %b} {day.day}"


def no_activity(s: dict) -> bool:
    """Nothing recorded at all: a holiday, a day off or a data gap. Never shown as missed goals."""
    kp = s["kpis"]
    return not (kp.get("dials") or kp.get("cn") or s.get("calls60"))


def _focus(s: dict, fu: dict) -> str:
    kp = s["kpis"]
    if no_activity(s):
        if fu.get("ok") and fu["today"]:
            return f"Start with today's {fu['today']} follow-up{'s' if fu['today'] != 1 else ''}, then your call list."
        return "All three dialing blocks today (10:15, 2:00 and 4:45)."
    missed = [b for b, hit in (kp.get("blocks") or {}).items() if not hit]
    unmarked_calls = sum(1 for c in s.get("calls60") or [] if not c.get("mark"))
    unmarked_new = (kp.get("new_numbers") or 0) - (kp.get("new_marked") or 0)
    if missed:
        return "Dial in all three blocks today (10:15, 2:00 and 4:45); yesterday missed " + ", ".join(missed) + "."
    if unmarked_calls:
        return f"Mark the outcome on every 60+ second call ({unmarked_calls} left unmarked yesterday)."
    if unmarked_new > 0:
        return f"Mark every new number right after you dial it ({unmarked_new} left unmarked yesterday)."
    if fu.get("ok") and fu["today"]:
        return f"Start with today's {fu['today']} follow-up{'s' if fu['today'] != 1 else ''}, then your call list."
    return "Keep yesterday's habits going: all three blocks, every number marked."


def format_report(s: dict, fu: dict, today: dt.date, offers: dict | None = None) -> str:
    day = dt.date.fromisoformat(s["day"])
    kp = s["kpis"]
    st = s.get("stats") or {}
    out = [f"*Good morning, Rahaf. Your numbers for {_d(day)}*", ""]
    out += _offer_lines(offers or {"state": "unavailable"}, day, today) + [""]
    if no_activity(s):
        out += ["*Activity* (from DataSift)",
                f"• No dialing activity was recorded in DataSift for {_d(day)}. If that was a day off or a holiday, "
                "there's nothing to do; otherwise it's likely a data issue and Kaycie will check. "
                "It isn't counted against your goals.",
                "• Photos received: Not tracked"]
    else:
        out += ["*Activity* (from DataSift)",
           f"• Dials: {kp['dials']} visible in DataSift (provisional; the smrtPhone total may be higher) · target {k.DIAL_FLOOR}",
           f"• Correct-number contacts: {kp['cn']}",
           f"• 60+ second calls with a correct-number contact: {kp['conv60']} (2+ minutes: {kp['conv120']}) · floor {k.CONV_FLOOR}, goal {k.CONV_GOAL}",
           f"• Leads advanced to new/warm/hot or offer: {kp['qualified']}",
           "• Dialing blocks: " + "  ".join(f"{b} {'✓' if hit else '✗'}" for b, hit in (kp.get("blocks") or {}).items()),
           f"• New numbers marked: {kp['new_marked']}/{kp['new_numbers']}",
           "• Photos received: Not tracked"]
    if not s.get("live") or st.get("skipped") or st.get("errors"):
        out.append("• Some of yesterday's activity may be missing from these counts (a data issue, not your work).")
    calls = s.get("calls60") or []
    out += ["", f"*All answered 60+ second calls* ({len(calls)}; length only, content not reviewed)"]
    if not calls:
        out.append("• None visible in DataSift.")
    for c in calls[:k.CONV_LIST_MAX]:
        ref = f"call {c['id']}" if c.get("id") else f"<{k.RECORD_URL.format(c['uuid'])}|open record>"
        out.append(f"• {c['time']} · {c['street'] or '?'} · …{c['last4']} · {c['dur']}s · {c.get('mark') or 'UNMARKED - please mark it'} · {ref}")
    if len(calls) > k.CONV_LIST_MAX:
        out.append(f"• …and {len(calls) - k.CONV_LIST_MAX} more")
    out += ["", f"*Follow-ups for {_d(today)}* (DataSift tasks)"]
    if not fu.get("ok"):
        out.append("• Follow-ups are unavailable this morning. Please check your tasks in DataSift.")
    else:
        line = f"• Due today: {fu['today']} · Overdue: {fu['overdue']}"
        if fu.get("oldest"):
            line += f" (oldest {_d(fu['oldest'])})"
        out.append(line + f" · Due tomorrow: {fu['tomorrow']}")
        for t in fu["show"]:
            when = "today" if t["due"].date() == today else _d(t["due"].date())
            if not t["all_day"]:
                when += f" {t['due']:%I:%M %p}".replace(" 0", " ")
            where = f"<{k.RECORD_URL.format(t['uuid'])}|{t['street'] or 'open record'}>" if t.get("uuid") else (t["street"] or "?")
            out.append(f"• {t['title']} · {where} · due {when}")
        shown = len(fu["show"])
        if fu["today"] + fu["overdue"] > shown:
            out.append(f"• …and {fu['today'] + fu['overdue'] - shown} more in DataSift")
    out += ["", f"*Today's focus:* {_focus(s, fu)}"]
    return _fit("\n".join(out))


def _fit(text: str, limit: int = CHAT_MAX_CHARS) -> str:
    """Drop list lines from the end of the longest list until the message fits."""
    lines = text.splitlines()
    while len("\n".join(lines)) > limit:
        idx = max((i for i, ln in enumerate(lines) if ln.startswith("• ") and " · " in ln and "…and" not in ln), default=None)
        if idx is None:
            return "\n".join(lines)[:limit]
        lines.pop(idx)
    return "\n".join(lines)


# ----------------------------------------------------------------- delivery
def post_chat(text: str, url: str, tries: int = POST_TRIES, sleep=time.sleep, opener=urllib.request.urlopen) -> str:
    """POST to a Google Chat incoming webhook; returns posted / failed / uncertain. Retries
    429/5xx and failures to connect, where the message was not accepted. A timeout or dropped
    connection after sending may have delivered it, so that is "uncertain" and not retried
    (a retry could double-post). A 4xx is a setup problem. Never prints the URL."""
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"content-type": "application/json; charset=UTF-8"})
    for attempt in range(1, tries + 1):
        try:
            opener(req, timeout=20).read()
            return "posted"
        except urllib.error.HTTPError as e:
            print(f"(Google Chat post attempt {attempt}: HTTP {e.code})")
            if e.code != 429 and e.code < 500:
                return "failed"
        except urllib.error.URLError as e:
            print(f"(Google Chat post attempt {attempt}: {type(e.reason).__name__})")
            if isinstance(e.reason, (TimeoutError, ConnectionResetError)):
                return "uncertain"
        except (TimeoutError, ConnectionResetError, http.client.HTTPException) as e:
            print(f"(Google Chat post attempt {attempt}: {type(e).__name__})")
            return "uncertain"
        except Exception as e:  # noqa: BLE001
            print(f"(Google Chat post attempt {attempt}: {type(e).__name__})")
            return "uncertain"
        if attempt < tries:
            sleep(5 * attempt)
    return "failed"


def alert_internal(text: str) -> bool:
    """Internal monitoring only (Slack). Never Rahaf's space."""
    print(text)
    return p._slack(text)


def run(args, post=post_chat, alert=alert_internal, followups=None, offers=None) -> int:
    """0 = posted, already posted, or dry run. 1 = could not deliver. 2 = no snapshot (notice handled)."""
    if args.only_between and not p._in_window(args.only_between):
        print(f"Outside window {args.only_between} Central (now {p.ct_now():%H:%M}); skipping.")
        return 0
    today = dt.date.fromisoformat(args.today) if args.today else p.ct_now().date()
    day = args.day or p.prev_business_day(today).isoformat()
    out = Path(args.out)
    markers = [Path(args.markers), out]
    say = alert if args.alert else print
    block = args.send and not args.force and _marker_block(markers, report_marker(day))
    if block:
        _write_marker(out, report_marker(day), day, "skipped", reason=f"earlier run {block}")
        if block == "posted":
            print(f"KPI report for {day} was already posted; not posting again (use --force to re-post).")
        else:
            say(f":warning: Daily KPI report for {day}: an earlier run's post is {block} (it may have reached the space). "
                "Not posting again. Check the Google Chat space; if the report isn't there, re-run with --send --force.")
        return 0
    url = p._env(CHAT_URL_ENV) if args.send else ""
    if args.send and not url:
        say(f":warning: Daily KPI report for {day} not sent: {CHAT_URL_ENV} is not set.")
        return 1
    snap = find_snapshot(Path(args.snapshots), day)
    if snap is None:
        say(f":warning: Daily KPI report for {day}: no KPI snapshot from the 7:15 PM audit was found "
            f"(audit did not run, failed, or its artifact was not downloaded). Re-run the audit with day {day}, then this report with --force.")
        if not args.send:
            _write_marker(out, notice_marker(day), day, "dry_run")
            print("(dry run) would send Rahaf:\n" + DELAYED_NOTICE)
        elif nb := _marker_block(markers, notice_marker(day)):
            _write_marker(out, notice_marker(day), day, "skipped", reason=f"earlier run {nb}")
            print(f"Delayed notice for this day is already {nb}; not repeating.")
        else:
            res = _deliver(DELAYED_NOTICE, url, post, out, notice_marker(day), day)
            if res != "posted":
                say(f":x: Daily KPI report for {day}: the delayed notice to Rahaf {res} "
                    + ("(may have arrived; check the space before re-sending)." if res == "uncertain" else "to post."))
        return 2
    st = snap.get("stats") or {}
    if not snap.get("live") or st.get("skipped") or st.get("errors"):
        say(f":warning: Daily KPI report for {day} uses an incomplete snapshot (live={snap.get('live')}, "
            f"skipped={st.get('skipped', 0)}, errors={st.get('errors', 0)}); Rahaf sees a generic 'may be missing' note.")
    if no_activity(snap):
        say(f":warning: Daily KPI report for {day}: the snapshot has no dials, contacts or calls. Holiday/day off, "
            "or a DataSift data gap? Rahaf sees a neutral 'no activity recorded' line, no targets.")
    if followups is None:
        key = p._env("REISIFT_API_KEY")
        fu = load_followups(p.Sift(key), snap.get("caller") or p.CALLER_EMAIL, today) if key else {"ok": False, "why": "REISIFT_API_KEY missing"}
    else:
        fu = followups
    if not fu.get("ok"):
        say(f":warning: Daily KPI report for {day}: follow-ups shown as unavailable ({fu.get('why')}).")
    rday = dt.date.fromisoformat(day)
    if offers is None:
        key = p._env("REISIFT_API_KEY")
        offers = load_offers(p.Sift(key), offers_since(rday)) if key else {"ok": False, "why": "REISIFT_API_KEY missing"}
    om = summarize_offers(offers, rday)
    if om["state"] == "unavailable":
        say(f":warning: Daily KPI report for {day}: Offers Made shown as unavailable ({offers.get('why')}).")
    elif om["state"] == "ok":
        issues = [msg for ts, msg in offers["flags"] if ts and ts.date() == rday]
        hint = unlogged_text_check(offers, rday)
        if hint:
            issues.append(hint)
        if om["week"]["review"]:
            issues.append(f"{om['week']['review']} offer note(s) this week are waiting for review or a correction (not counted).")
        start = max(OFFERS_TRACKED_FROM, offers_since(rday))
        still = sorted((e for e in offers["events"] if e["status"] in REVIEW and e.get("ts") and start <= e["ts"].date() < rday),
                       key=lambda e: e["ts"])
        if still:
            issues.append(f"Still open from earlier days ({len(still)}; fix with a CORRECTION note or a ruling): "
                          + "; ".join(f"{_ref(e)} {_d(e['ts'].date())} ({e['status'].replace('_', ' ')})" for e in still[:10])
                          + (" …" if len(still) > 10 else ""))
        if issues:
            say(f":mag: Offers Made review for {day} (internal):\n" + "\n".join(f"• {m}" for m in issues))
    text = format_report(snap, fu, today, om)
    if not args.send:
        _write_marker(out, report_marker(day), day, "dry_run", chars=len(text))
        print("(dry run; not posted)\n" + text)
        return 0
    res = _deliver(text, url, post, out, report_marker(day), day)
    if res == "posted":
        print(f"KPI report for {day} posted to Google Chat.")
        return 0
    if res == "uncertain":
        say(f":warning: Daily KPI report for {day}: the Google Chat post timed out after sending and may have arrived. "
            "Not retrying automatically. Check the space; if it isn't there, re-run with --send --force.")
    else:
        say(f":x: Daily KPI report for {day} failed to post to Google Chat. Safe to re-run with --send.")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--day", help="report day YYYY-MM-DD (default: previous business day, Central)")
    ap.add_argument("--today", help=argparse.SUPPRESS)
    ap.add_argument("--snapshots", default=str(OUT_DIR / "snapshots"), help="folder of downloaded audit artifacts")
    ap.add_argument("--markers", default=str(OUT_DIR / "markers"), help="folder of downloaded kpi-report artifacts")
    ap.add_argument("--out", default=str(OUT_DIR / "posted"), help="where this run writes its markers (upload as artifact)")
    ap.add_argument("--send", action="store_true", help="post to Google Chat (default: dry run)")
    ap.add_argument("--force", action="store_true", help="post even if this day's report was already posted")
    ap.add_argument("--alert", action="store_true", help="send technical problems to internal Slack")
    ap.add_argument("--only-between", help="HH:MM-HH:MM Central; outside it, skip")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")    # Windows consoles can't print ✓/✗
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
