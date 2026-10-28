"""pm_text_sender.py - afternoon voicemail follow-up texts + end-of-day caller audit.

Why this exists
---------------
Rahaf's Jackson Priority 1 attempt is: automated morning text, call, voicemail,
and an afternoon text to every number she left a voicemail on. The afternoon
text was the one manual step, and on day one (2026-10-19) none of the 47 went
out. DataSift drips can't do it (they text the primary phone, which matched the
voicemail number on only 18 of 48 that day), so this script sends the text to
the exact number through smrtPhone's SMS API.

How a voicemail is detected (backtested against transcripts of all 72 day-one
recordings): an outbound call by the caller that smrtPhone logged as answered
for >= MIN_VM_SECONDS, on a number the caller marked No Answer that day. That
rule matched 45 of 48 real voicemails and misfired 3 times. The caller's mark
is the gate, so "mark every number you dial" is the only manual step left.

Modes
-----
    python src/pm_text_sender.py pm-texts            # dry run: list what would be sent
    python src/pm_text_sender.py pm-texts --send     # send for real
    python src/pm_text_sender.py pm-texts --day 2026-10-19   # backtest a past day (dry run only)
    python src/pm_text_sender.py audit               # end-of-day checklist (stdout + Slack if webhook set)
    python src/pm_text_sender.py line-watch          # alert when a line crosses 20 texts today
    python src/pm_text_sender.py call-watch          # post inbound calls/voicemails to Google Chat + Slack

Env: REISIFT_API_KEY, SMRTPHONE_API_TOKEN (pm-texts --send), SLACK_WEBHOOK_URL,
GOOGLE_CHAT_WEBHOOK_URL (line-watch, call-watch) (optional). Caller/lines are constants below; change them there when the
roster changes.

Safety rules baked in: never texts a number marked Correct/Wrong/Dead/DNC, an
owner flagged dnc/opt_out, a record that has left Prospecting, a number that
replied or called back that day, or a number already texted from an afternoon
line that day. Hard cap MAX_PER_LINE per afternoon line per day; overflow is
reported, not sent. Scheduled runs only send between SEND_WINDOW hours Central.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

API = "https://apiv2.reisift.io"
SMS_API = "https://phone.smrt.studio/sms/send"
ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "output" / "pm_texts"

CALLER_EMAIL = "rahaf@bluetailhomes.com"
CALLER_USER_UUID = "7cf06756-3bca-41be-9c02-ccc27b954007"
PM_LINES = ["8166084051", "8167100226", "8167100554"]          # afternoon voicemail texts
CALL_LINES = {"8167889485", "8167595422", "8167100448", "8167100146"}
DRIP_LINES = {"8162545235", "8169553316", "8167889952", "8168971724", "8168971637"}
CALLBACK_LINE = "8169553574"
# Legacy follow-up lines: KC (816) 788-9386, OK (405) 764-1708, NM (505) 520-1714.
# Each lead is called, voicemailed and texted from its own market's line.
LEGACY_LINES = {"8167889386", "4057641708", "5055201714"}
LEGACY_TEXT_CAP = 20
LEGACY_TAG_PREFIX = "Legacy Recall"
MAX_PER_LINE = 20
MIN_VM_SECONDS = 20
SHIFT = ("10:00", "18:00")   # Rahaf's shift, Central, no lunch (her request, from 2026-10-26)
DIAL_FLOOR = 150             # daily dial floor (rahaf-kpi-scorecard production standards)
SESSION_GAP_MIN = 15         # actions closer than this count as continuous work
BREAK_MIN = 30               # gaps at least this long inside the shift are listed
SEND_WINDOW = (16, 18)   # Central hours during which a scheduled --send is allowed
TERMINAL_MARKS = {"CORRECT", "WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC"}   # marks that end the afternoon text for a number
BAD_MARKS = {"WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC"}                    # marks that disqualify a number as the drip's primary phone

TEMPLATES = {
    1: "Hey {first}, it's Britney. Just wanted to make sure you got my voicemail about {street}.",
    2: "Hey {first}, it's Britney. I left you a voicemail earlier about {street}. Any plans for it?",
    3: "Hey {first}, it's Britney. I tried you again this morning about {street}. Happy to talk whenever works.",
    4: "Hey {first}, it's Britney. Just checking you got my voicemail about {street}.",
    5: "Hey {first}, it's Britney. I left one more voicemail about {street}. Thanks for your time either way.",
}


# ----------------------------------------------------------------- time
def _central_offset(when_utc: dt.datetime) -> dt.timedelta:
    """US Central: CDT (-5) from the 2nd Sunday of March to the 1st Sunday of November, else CST (-6)."""
    y = when_utc.year
    march = dt.datetime(y, 3, 1)
    start = march + dt.timedelta(days=(6 - march.weekday()) % 7 + 7, hours=8)   # 2nd Sunday 2am CST = 08:00 UTC
    nov = dt.datetime(y, 11, 1)
    end = nov + dt.timedelta(days=(6 - nov.weekday()) % 7, hours=7)              # 1st Sunday 2am CDT = 07:00 UTC
    return dt.timedelta(hours=-5) if start <= when_utc.replace(tzinfo=None) < end else dt.timedelta(hours=-6)


def to_ct(ts: str) -> dt.datetime | None:
    try:
        u = dt.datetime.strptime(ts[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")   # UTC, "T" or space
    except (ValueError, TypeError):
        return None
    return (u + _central_offset(u)).replace(tzinfo=dt.timezone(_central_offset(u)))


def ct_now() -> dt.datetime:
    u = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return (u + _central_offset(u)).replace(tzinfo=dt.timezone(_central_offset(u)))


# ----------------------------------------------------------------- api
def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
    return v


class Sift:
    def __init__(self, key: str):
        self.key, self._last = key, 0.0

    def call(self, path, method="GET", body=None, override=None):
        gap = 0.3 - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.time()
        data = json.dumps(body).encode() if body is not None else None
        h = {"authorization": "Api-Key " + self.key, "accept": "application/json", "user-agent": "SiftStack pm_text_sender/1.0"}
        if data is not None:
            h["content-type"] = "application/json"
        if override:
            h["x-http-method-override"] = override
        req = urllib.request.Request(API + path, data=data, method=method, headers=h)
        for attempt in range(12):
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    t = r.read().decode()
                    return json.loads(t) if t.strip().startswith(("{", "[")) else t
            except urllib.error.HTTPError as e:
                txt = e.read().decode(errors="replace")
                if e.code == 429 and attempt < 11:
                    # The server's "available in N seconds" hint is often too short when
                    # throttling persists, so back off at least 3s, 6s, 9s, ... as well.
                    m = re.search(r"available in (\d+)\s*second", txt, re.I)
                    hint = int(m.group(1)) + 1 if m else 0
                    time.sleep(min(max(hint, 3 * (attempt + 1)), 300))
                    self._last = time.time()
                    continue
                if e.code >= 500 and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"HTTP {e.code} {method} {path}: {txt[:300]}")
        raise RuntimeError("gave up " + path)

    def search(self, must: dict, limit: int = 200) -> list[dict]:
        out, off = [], 0
        while True:
            r = self.call("/api/internal/property/", "POST", {"limit": limit, "offset": off, "query": {"must": must}}, override="GET")
            rows = r.get("results") or r.get("data") or []
            out.extend(rows)
            off += limit
            if len(rows) < limit or off > 20000:
                return out


# ----------------------------------------------------------------- model
def _who(e: dict) -> str:
    a = e.get("author_extra_info") or {}
    return a.get("email") or e.get("author") or ""


def _actor(e: dict) -> str:
    """Who did it: the smrtPhone user on a call/SMS event, else the log author."""
    p = e.get("payload") or {}
    for k in ("call", "sms"):
        x = p.get(k)
        if isinstance(x, dict) and (x.get("external_user") or {}).get("email"):
            return x["external_user"]["email"]
    return _who(e)


def load_day(api: Sift, day: str, caller: str, stats: dict | None = None) -> list[dict]:
    """Every record with activity on `day` (Central), modelled for the rules below.
    `stats`, when given, is filled with {"selected", "skipped", "errors"}: records the search
    returned, records dropped because their logs or detail could not be read, and failed
    source queries (the caller adds its own). A skipped record never stops the day."""
    stats = stats if stats is not None else {}
    for k in ("selected", "skipped", "errors"):
        stats.setdefault(k, 0)
    d0 = dt.date.fromisoformat(day)
    rows = api.search({"property_type": "clean", "updated": [(d0 - dt.timedelta(days=1)).isoformat(), (d0 + dt.timedelta(days=1)).isoformat()]})
    stats["selected"] += len(rows)
    models = []
    for rec in rows:
        u = rec.get("uuid")
        try:
            evs = (api.call(f"/api/internal/property/{u}/logs/?limit=250&offset=0") or {}).get("results") or []
        except RuntimeError:
            stats["skipped"] += 1
            continue
        today, all_call_days = [], set()
        for e in evs:
            t = to_ct(e.get("timestamp", ""))
            if not t:
                continue
            e["_ct"] = t
            if e.get("event_type") == "owner.call.made":
                c = (e.get("payload") or {}).get("call") or {}
                if (c.get("external_user") or {}).get("email") == caller and c.get("direction") == "outbound":
                    all_call_days.add(t.date().isoformat())
            if t.date().isoformat() == day:
                today.append(e)
        if not today:
            continue
        today.sort(key=lambda e: e["_ct"])
        try:
            det = api.call(f"/api/internal/property/{u}/")
        except RuntimeError:
            stats["skipped"] += 1
            continue
        try:
            # A malformed log row or detail costs that one record, not the whole day.
            owner = det.get("owner") or {}
            phones = _all_phones(det)
            calls: dict[str, dict] = {}
            for e in today:
                et = e["event_type"]
                if et.startswith("owner.call."):
                    c = e["payload"]["call"]
                    cc = calls.setdefault(c["uuid"], {"ct": e["_ct"], "id": c.get("external_id"), "from": c.get("origin_number") or "", "to": c.get("destination_number") or "",
                                                      "dir": c.get("direction"), "by": (c.get("external_user") or {}).get("email"), "dur": None, "result": None, "rec": None})
                    if et == "owner.call.answered":
                        cc.update(result="answered", dur=c.get("duration"), rec=c.get("recording"))
                    elif et == "owner.call.noanswer":
                        cc["result"] = "noanswer"
                    elif et in ("owner.call.missed", "owner.call.received"):
                        cc["result"] = "inbound"
            m = {
                "uuid": u, "street": (det.get("address") or {}).get("street") or "", "status": det.get("status"),
                "assigned_to": det.get("assigned_to"), "tags": det.get("tags") or [], "attempts": det.get("predictivecall_attempts") or 0,
                "owner_first": (owner.get("first_name") or "").strip(), "owner_dnc": bool(owner.get("dnc")), "owner_opt_out": bool(owner.get("opt_out")),
                "phones": phones, "stop": stop_policy(owner, phones, dnc_origins(evs)),
                "primary": _d10((owner.get("primary_phone") or {}).get("number") if isinstance(owner.get("primary_phone"), dict) else owner.get("primary_phone")),
                "open_tasks": rec.get("tasks") if isinstance(rec.get("tasks"), int) else None,
                "calls": sorted(calls.values(), key=lambda c: c["ct"]), "call_days": all_call_days,
                "sms_out": [{"ct": e["_ct"], "from": e["payload"]["sms"].get("origin_number") or "", "to": e["payload"]["sms"].get("destination_number") or "",
                             "manual": bool((e["payload"]["sms"].get("external_user") or {}).get("email")),
                             "by": (e["payload"]["sms"].get("external_user") or {}).get("email")} for e in today if e["event_type"] == "owner.sms.sent"],
                "sms_in": [{"ct": e["_ct"], "from": e["payload"]["sms"].get("origin_number") or "", "text": e["payload"]["sms"].get("message") or ""}
                           for e in today if e["event_type"] == "owner.sms.received"],
                "marks": [{"ct": e["_ct"], "phone": e["payload"]["owner"].get("phone"), "frm": (e["payload"]["owner"].get("status") or [None, None])[0], "to": (e["payload"]["owner"].get("status") or [None, None])[-1], "by": _who(e)}
                          for e in today if e["event_type"] == "owner.phone.status.updated"],
                "status_changes": [{"ct": e["_ct"], "to": (e["payload"]["property"].get("status") or [None, None])[-1], "by": _who(e)} for e in today if e["event_type"] == "property.status.updated"],
                "attempt_bumps": [e["_ct"] for e in today if e["event_type"] == "property.predictivecall.attempts.increased" and _who(e) == caller],
                "notes": [e["_ct"] for e in today if e["event_type"] in ("property.message.added", "property.message.updated") and _who(e) == caller],
                "actions": [e["_ct"] for e in today if _actor(e) == caller],
                "task_events": sum(1 for e in today if "task" in e["event_type"] and _actor(e) == caller),
                "tags_added": [(t, _who(e)) for e in today if e["event_type"] == "property.tags.added" for t in ((e.get("payload") or {}).get("tags") or [])],
            }
        except (KeyError, TypeError, AttributeError, ValueError):
            stats["skipped"] += 1
            continue
        models.append(m)
    return models


def tier(m: dict, number: str) -> str:
    p = m["phones"].get(number)
    tags = [t for t in (p.get("tags") or []) if t.startswith("Dial") or t == "Drop"] if p else []
    return tags[0] if tags else ("(not on record)" if not p else "(untagged)")


# A seller's sign-off ("okay", "thanks", a thumbs up) needs no reply.
_ACK_RE = re.compile(r"^(ok(ay)?|k|kk|thanks?( you)?|thx|ty|sounds good|got it|will do|np|no problem)"
                     r"( thanks?( you)?)?$", re.I)


# An opt-out text: the SMS responder marks that number DNC itself, so no reply is owed.
_STOP_RE = re.compile(r"^(stop|stop all|unsubscribe|cancel|end|quit|remove me|opt out)$", re.I)
DNC_MARKS = {"DNC", "CORRECT_DNC", "WRONG_DNC"}


def is_stop(text: str) -> bool:
    return bool(_STOP_RE.match(re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text or "")).strip()))


# ----------------------------------------------------------------- STOP policy
# One rule for every job that reaches out (eligibility, top-up, am-start, pm-texts, extra-texts,
# drip guard, calling review list):
#   confirmed owner STOP   (owner DNC/opt-out, a CORRECT_DNC number, or DNC on a number that was
#                           CORRECT)                       -> suppress all outreach to the owner
#   confirmed wrong-number STOP (WRONG_DNC)                -> suppress that number only
#   ambiguous STOP (plain DNC on a number never confirmed) -> suppress that number, hold the
#                                                             record for human review
# A DNC phone whose history can't be read counts as ambiguous, so the rule fails safe.
def dnc_origins(evs: list[dict]) -> dict[str, str | None]:
    """{phone: status it had just before its latest move into a DNC mark} from property logs."""
    out = {}
    for e in sorted(evs, key=lambda e: e.get("timestamp") or ""):
        if e.get("event_type") != "owner.phone.status.updated":
            continue
        o = (e.get("payload") or {}).get("owner") or {}
        st = o.get("status") or [None, None]
        old, new = (st[0], st[-1]) if isinstance(st, list) else (None, st)
        if new in DNC_MARKS and old not in DNC_MARKS:
            out[_d10(o.get("phone"))] = old
    return out


def stop_policy(owner: dict, phones: dict, origins: dict | None) -> dict:
    """{owner_stop, blocked (numbers never to contact), review (numbers needing a human), hold, why}.
    `phones` = {number: phone dict}; `origins` = dnc_origins() of the record's logs (None = unread)."""
    blocked, review, why = set(), set(), []
    owner_stop = bool(owner.get("dnc") or owner.get("opt_out"))
    if owner_stop:
        why.append("owner DNC/opt-out")
    for n, ph in phones.items():
        st = (ph.get("status") or "").upper()
        if st not in DNC_MARKS:
            continue
        n = _d10(n)
        blocked.add(n)
        prior = (origins or {}).get(n)
        if st == "WRONG_DNC":
            continue
        # A CORRECT_DNC mark alone is not proof the owner opted out: the SMS responder writes it
        # for any STOP from a number not already marked Wrong/Dead. Only a number confirmed
        # Correct before its STOP makes this an owner STOP; otherwise hold for identity review.
        if prior == "CORRECT":
            owner_stop = True
            why.append(f"owner STOP on ...{n[-4:]}")
        else:
            review.add(n)
            why.append(f"ambiguous STOP on ...{n[-4:]} (was {prior or 'unknown'})")
    return {"owner_stop": owner_stop, "blocked": blocked, "review": review,
            "hold": owner_stop or bool(review), "why": "; ".join(why)}


def _all_phones(det: dict) -> dict:
    owner = det.get("owner") or {}
    phones = {p["number"]: p for p in (owner.get("phones") or [])}
    for so in det.get("secondary_owners") or []:
        for p in so.get("phones") or []:
            phones.setdefault(p["number"], p)
    return phones


def record_stop(api: Sift, uuid: str, det: dict) -> dict:
    """stop_policy() for a property detail; reads its logs only when a phone carries a DNC mark."""
    phones = _all_phones(det)
    origins = {}
    if any((p.get("status") or "").upper() in DNC_MARKS for p in phones.values()):
        try:
            evs = (api.call(f"/api/internal/property/{uuid}/logs/?limit=250&offset=0") or {}).get("results") or []
            origins = dnc_origins(evs)
        except RuntimeError:
            origins = None
    return stop_policy(det.get("owner") or {}, phones, origins)


def _eligible(p: dict) -> bool:
    """A Dial First/Second number not yet marked Correct/Wrong/Dead/DNC."""
    return any(t in ("Dial First", "Dial Second") for t in p.get("tags") or []) and p.get("status") not in TERMINAL_MARKS


def is_ack(text: str) -> bool:
    t = re.sub(r"[^\w\s]", "", text or "").strip()
    return bool(text and text.strip()) and "?" not in text and (not t or bool(_ACK_RE.match(re.sub(r"\s+", " ", t))))


def final_mark(m: dict, number: str, caller: str) -> str | None:
    """The mark in effect on `number` at the end of the day.

    A mark made today by the caller wins. Otherwise the phone's stored status
    counts, because a number marked No Answer yesterday stays No Answer today:
    the smrtPhone pop-up writes the mark once, and clicking the same icon again
    in Sift would clear it (Rahaf, 2026-10-20). Only an UNKNOWN/blank status
    means the number was never marked.
    """
    marks = [x for x in m["marks"] if x["phone"] == number and x["by"] == caller]
    if marks:
        return marks[-1]["to"]
    cur = (m["phones"].get(number) or {}).get("status")
    return cur if cur and cur != "UNKNOWN" else None


# ----------------------------------------------------------------- pm texts
def plan_pm_texts(models: list[dict], day: str, caller: str, already_sent: set[str],
                  prior_line_counts: Counter | None = None) -> tuple[list[dict], list[dict]]:
    """Return (to_send, skipped). One text per number per day, exact number the voicemail went to."""
    to_send, skipped, seen_numbers = [], [], set()
    for m in models:
        best: dict[str, dict] = {}
        for c in m["calls"]:
            if c["by"] == caller and c["dir"] == "outbound" and c["result"] == "answered":
                if c["to"] not in best or (c["dur"] or 0) > (best[c["to"]]["dur"] or 0):
                    best[c["to"]] = c
        stop = m.get("stop") or stop_policy({"dnc": m.get("owner_dnc"), "opt_out": m.get("owner_opt_out")}, m["phones"], None)
        for n, c in best.items():
            if n in seen_numbers:
                continue
            why = None
            if (c["dur"] or 0) < MIN_VM_SECONDS:
                why = f"answered only {c['dur']}s (greeting hang-up or short)"
            elif final_mark(m, n, caller) != "NO_ANSWER":
                why = f"number not marked No Answer (mark={final_mark(m, n, caller)})"
            elif (m["phones"].get(n) or {}).get("status") in TERMINAL_MARKS:
                why = f"number now {(m['phones'].get(n) or {}).get('status')}"
            elif stop["hold"] or _d10(n) in stop["blocked"]:
                why = f"STOP policy hold ({stop['why'] or 'number DNC'})"
            elif any(s["to"] == n and s["from"] in DRIP_LINES for s in m["sms_out"]):
                why = "drip already texted this number today"
            elif (m["phones"].get(n) or {}).get("type") not in (None, "", "MOBILE"):
                # smrtPhone rejects a text to a landline/VoIP line with HTTP 400 (2 a day on
                # 2026-10-21, both confirmed non-mobile by Trestle). A number with no type on
                # the record still gets tried.
                why = f"number is {(m['phones'].get(n) or {}).get('type')}, not mobile (can't take a text)"
            elif m["status"] != "prospecting":
                why = f"record status is {m['status']!r}, not prospecting"
            elif any(s["from"] == n for s in m["sms_in"]) or any(x["dir"] == "inbound" and x["from"] == n for x in m["calls"]):
                why = "number replied or called back today"
            elif any(s["to"] == n and s["from"] in PM_LINES for s in m["sms_out"]) or n in already_sent:
                why = "afternoon text already sent today"
            elif not m["street"]:
                why = "record has no street address"
            seen_numbers.add(n)
            # Day N = the record's attempt number: Call Attempts after today's bump, or +1 if not bumped yet.
            day_no = max(1, min(5, m["attempts"] if m["attempt_bumps"] else m["attempts"] + 1))
            first = m["owner_first"].split()[0].title() if m["owner_first"] else ""
            msg = TEMPLATES[day_no].format(first=first, street=m["street"]).replace("Hey , ", "Hey, ")
            entry = {"uuid": m["uuid"], "street": m["street"], "owner_first": first, "to": n, "tier": tier(m, n),
                     "vm_time": c["ct"].strftime("%H:%M"), "vm_seconds": c["dur"], "day": day_no, "message": msg}
            if why:
                skipped.append({**entry, "why": why})
            else:
                to_send.append(entry)
    # line rotation + per-line cap. The cap counts texts already sent from each line earlier
    # today (a retry after a partial run), so a line never goes past MAX_PER_LINE in a day.
    counts, i = Counter(prior_line_counts or {}), 0
    for e in to_send:
        free = [PM_LINES[(i + k) % len(PM_LINES)] for k in range(len(PM_LINES))]
        free = [ln for ln in free if counts[ln] < MAX_PER_LINE]
        i += 1
        if not free:
            e["line"], e["overflow"] = None, True
            continue
        counts[free[0]] += 1
        e["line"], e["overflow"] = free[0], False
    return to_send, skipped


def running_drip(api: Sift, uuid: str) -> bool:
    h = api.call(f"/api/internal/drip-campaign-history/by-property/?property_uuid={uuid}")
    rows = h.get("results") if isinstance(h, dict) else h
    return any(x.get("status") == "running" for x in rows or [])


def drop_drip_primaries(api: Sift, to_send: list[dict], skipped: list[dict], models: list[dict]) -> list[dict]:
    """The drip texts the record's primary phone every morning, so an afternoon text to that same
    number would be a second text that day. A record whose drip state can't be read is held too."""
    prim = {m["uuid"]: m.get("primary") for m in models}
    keep = []
    for e in to_send:
        if _d10(e["to"]) != prim.get(e["uuid"]):
            keep.append(e)
            continue
        try:
            on_drip = running_drip(api, e["uuid"])
        except RuntimeError as ex:
            skipped.append({**e, "why": f"drip state unreadable, held ({str(ex)[:80]})"})
            continue
        if on_drip:
            skipped.append({**e, "why": "primary phone is on a running drip (would double-text)"})
        else:
            keep.append(e)
    return keep


# ----------------------------------------------------------------- drip state
# The P1 morning drip: 5 texts, one day apart, each 86400s after the last one actually went out,
# so sends drift later every day. Its line comes from the drip's title ("... Morning Texts Line C ...").
DRIP_SENDS = 5
_DRIP_LINE_RE = re.compile(r"Morning Texts (Line [A-E])")


def drip_rows(api: Sift, uuid: str) -> list[dict]:
    h = api.call(f"/api/internal/drip-campaign-history/by-property/?property_uuid={uuid}")
    return (h.get("results") if isinstance(h, dict) else h) or []


def drip_line(h: dict) -> str | None:
    """'SMS Line C' for a P1 morning-text drip history, None for any other drip."""
    src = (((h.get("details") or {}).get("event_extra_info") or {}).get("source") or {}).get("title") or ""
    m = _DRIP_LINE_RE.search(src)
    return "SMS " + m.group(1) if m else None


def drip_histories(api: Sift) -> list[dict]:
    """Every P1 morning-text drip history in the account (the list endpoint ignores its drip filter)."""
    out, off = [], 0
    while True:
        r = api.call(f"/api/internal/drip-campaign-history/?limit=100&offset={off}")
        rows = r.get("results") or []
        out.extend(h for h in rows if drip_line(h))
        off += 100
        if not r.get("next") or not rows or off > 20000:
            return out


def classify_drip(h: dict, now: dt.datetime) -> dict:
    """Where a P1 drip stands at `now` (Central):
    completed      all 5 texts went out (or DataSift closed it)
    delayed        its next text is due today, possibly overdue or late in the evening: keep it
    weekend-bound  its next text would go out on a Saturday or Sunday: the only removal candidate
    later-weekday  its next text is due on a later weekday: keep it
    failed/...     DataSift stopped it (e.g. no primary phone): nothing more will send, flagged
    needs_review is set when later texts would land on a weekend or the state can't be read."""
    d = h.get("details") or {}
    sent = sum(1 for x in d.get("log") or [] if x.get("action") == "send_sms" and x.get("status") == "ok")
    out = {"history_uuid": h.get("uuid"), "line": drip_line(h), "sent": sent, "status": h.get("status"),
           "next": None, "after_hours": False, "needs_review": False}
    if h.get("status") == "completed" or sent >= DRIP_SENDS:
        return {**out, "state": "completed"}
    if h.get("status") != "running":
        return {**out, "state": h.get("status") or "unknown", "needs_review": True}
    nxt = to_ct(d.get("next_run_at") or "")
    if nxt is None:
        return {**out, "state": "unknown", "needs_review": True}
    out["next"] = nxt.strftime("%a %Y-%m-%d %H:%M")
    rest_on_weekend = any((nxt + dt.timedelta(days=i)).weekday() >= 5 for i in range(DRIP_SENDS - sent))
    if nxt.date() <= now.date():
        return {**out, "state": "delayed", "after_hours": nxt.hour >= 19, "needs_review": rest_on_weekend}
    if nxt.weekday() >= 5:
        return {**out, "state": "weekend-bound"}
    return {**out, "state": "later-weekday", "needs_review": rest_on_weekend}


def send_sms(token: str, line: str, to: str, message: str) -> tuple[bool, str]:
    body = urllib.parse.urlencode({"from": line, "to": to, "message": message}).encode()
    req = urllib.request.Request(SMS_API, data=body, method="POST",
                                 headers={"X-Auth-smrtPhone": token, "content-type": "application/x-www-form-urlencoded", "user-agent": "SiftStack pm_text_sender/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return True, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} {e.read().decode(errors='replace')[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def run_pm_texts(args) -> int:
    api = Sift(_env("REISIFT_API_KEY") or sys.exit("REISIFT_API_KEY missing"))
    day = args.day or ct_now().date().isoformat()
    live = bool(args.send)
    if live and args.day:
        sys.exit("--send is only allowed for today (backtests are dry-run only)")
    if live and not args.force and not _in_window(args.only_between or f"{SEND_WINDOW[0]}-{SEND_WINDOW[1]}"):
        print(f"Outside send window {args.only_between or SEND_WINDOW} Central (now {ct_now():%H:%M}); nothing sent. Use --force to override.")
        return 0
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / f"sent_{day}.json"
    sent_log = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else []
    already = {s["to"] for s in sent_log if s.get("ok")}

    models = load_day(api, day, CALLER_EMAIL)
    prior_counts = Counter(s.get("line") for s in sent_log if s.get("ok") and s.get("line"))
    to_send, skipped = plan_pm_texts(models, day, CALLER_EMAIL, already, prior_counts)
    to_send = drop_drip_primaries(api, to_send, skipped, models)
    lines = [f"Afternoon texts for {day} ({'LIVE' if live else 'DRY RUN'}): {sum(1 for e in to_send if not e['overflow'])} to send, "
             f"{sum(1 for e in to_send if e['overflow'])} over line cap, {len(skipped)} skipped"]
    for e in to_send:
        lines.append(f"  {'OVERFLOW' if e['overflow'] else e['line'][-4:]} -> ...{e['to'][-4:]} ({e['tier']}) | {e['street']} | {e['owner_first'] or '(no first name)'} | day {e['day']} | vm {e['vm_time']} {e['vm_seconds']}s")
    sk = Counter(s["why"].split(" (")[0] for s in skipped)
    lines.append("Skipped: " + ", ".join(f"{k} x{v}" for k, v in sk.most_common()))
    results = []
    if live:
        token = _env("SMRTPHONE_API_TOKEN") or sys.exit("SMRTPHONE_API_TOKEN missing")
        state_file.write_text(json.dumps(sent_log, indent=1, default=str), encoding="utf-8")
        for e in to_send:
            if e["overflow"]:
                continue
            ok, resp = send_sms(token, e["line"], e["to"], e["message"])
            results.append({**e, "ok": ok, "resp": resp, "sent_at": ct_now().isoformat()})
            sent_log.append(results[-1])
            state_file.write_text(json.dumps(sent_log, indent=1, default=str), encoding="utf-8")
            time.sleep(1.5)
        fails = [r for r in results if not r["ok"]]
        lines.append(f"Sent {len(results) - len(fails)}, failed {len(fails)}" + ("".join(f"\n  FAILED ...{r['to'][-4:]} {r['street']}: {r['resp']}" for r in fails)))
    report = "\n".join(lines)
    print(report)
    (STATE_DIR / f"plan_{day}.json").write_text(json.dumps({"to_send": to_send, "skipped": skipped, "results": results}, indent=1, default=str), encoding="utf-8")
    if args.slack:
        _slack(report)
    return 0


# ----------------------------------------------------------------- audit
def _hm(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m"


def work_time(models: list[dict], caller: str) -> list[str]:
    """Desk time from every DataSift action by the caller (dials, marks, notes, statuses, texts)."""
    acts = sorted({t for m in models for t in m.get("actions", [])})
    s0, s1 = (int(x[:2]) * 60 + int(x[3:]) for x in SHIFT)
    if not acts:
        return [f":warning: Desk time (shift {SHIFT[0]}-{SHIFT[1]}): no DataSift activity today."]
    mins = lambda t: t.hour * 60 + t.minute
    first, last = acts[0], acts[-1]
    active, breaks, start = 0, [], acts[0]
    for a, b in zip(acts, acts[1:]):
        gap = (b - a).total_seconds() / 60
        if gap > SESSION_GAP_MIN:
            active += (a - start).total_seconds() / 60
            start = b
            if gap >= BREAK_MIN and mins(b) > s0 and mins(a) < s1:
                breaks.append(f"{a:%H:%M}-{b:%H:%M} ({_hm(int(gap))})")
    active += (last - start).total_seconds() / 60
    txt = f"Desk time (shift {SHIFT[0]}-{SHIFT[1]}): first action {first:%H:%M}, last {last:%H:%M}, recorded activity about {_hm(int(active))} of {_hm(s1 - s0)} (from DataSift actions, not verified hours)."
    flags = []
    if mins(first) > s0 + 15:
        flags.append(f"started {_hm(mins(first) - s0)} late")
    if mins(last) < s1 - 15:
        flags.append(f"ended {_hm(s1 - mins(last))} early")
    out = [(":warning: " if flags else "") + txt + (" " + ", ".join(flags).capitalize() + "." if flags else "")]
    out.append("Gaps over 30 min: " + ("; ".join(breaks) if breaks else "none") + ".")
    return out


# Legacy follow-up presets Rahaf works each day (ZZ LEGACY TRIAGE folder), by exact title.
LEGACY_PRESETS = {"01 Finish Out": "01. Active - finish out", "06 NI Recall": "06. Legacy NI Recall - Jackson"}


def load_legacy_presets(api: Sift, day: str) -> dict:
    """{label: {"in_preset": uuids now in the preset, "tagged_today": uuids with the preset's tag updated today}}.

    A record Rahaf works can leave the preset the same day (status change, excluded
    tag), so the base is the preset now plus anything with its tag touched today.
    """
    rows = api.call("/api/internal/filter-preset/?limit=500")
    rows = rows.get("results") or rows.get("data") or rows
    d0 = dt.date.fromisoformat(day)
    out = {}
    for label, title in LEGACY_PRESETS.items():
        pz = next((x for x in rows if str(x.get("title", "")).strip() == title), None)
        if not pz:
            out[label] = None
            continue
        must = (pz.get("filters") or {}).get("must") or {}
        now = {r["uuid"] for r in api.search(must)}
        tagged = set()
        if must.get("any_tags"):
            tagged = {r["uuid"] for r in api.search({"any_tags": must["any_tags"],
                      "updated": [(d0 - dt.timedelta(days=1)).isoformat(), (d0 + dt.timedelta(days=1)).isoformat()]})}
        out[label] = {"in_preset": now, "tagged_today": tagged}
    return out


def legacy_preset_lines(models: list[dict], legacy: dict, caller: str) -> list[str]:
    """Worked = a call, text, note, phone mark or status change by the caller. Task completions alone are listed apart."""
    by_uuid = {m["uuid"]: m for m in models}
    parts, idle = [], []
    for label, sets in legacy.items():
        if sets is None:
            parts.append(f"{label}: preset not found")
            continue
        # The tag is broad (06 matches the old "Bluetail 1.0" tag), so the "left the preset
        # today" extension skips Priority 1 records (their calls are P1 work) and records
        # sitting in another legacy preset.
        others = set().union(*(s["in_preset"] for k, s in legacy.items() if k != label and s))
        base = sets["in_preset"] | {u for u in sets["tagged_today"] - others
                                    if u in by_uuid and "Priority 1" not in by_uuid[u]["tags"]}
        calls = texts = 0
        worked, tasks_only = set(), set()
        for u in base:
            m = by_uuid.get(u)
            if not m:
                continue
            c = sum(1 for x in m["calls"] if x["by"] == caller and x["dir"] == "outbound")
            t = sum(1 for x in m["sms_out"] if x["by"] == caller)
            other = len(m["notes"]) + sum(1 for x in m["marks"] if x["by"] == caller) + sum(1 for x in m["status_changes"] if x["by"] == caller)
            calls, texts = calls + c, texts + t
            if c or t or other:
                worked.add(u)
            elif m.get("task_events"):
                tasks_only.add(u)
        parts.append(f"{label} worked {len(worked)} of {len(base)} (calls {calls}, texts {texts})"
                     + (f", {len(tasks_only)} with task completions only" if tasks_only else ""))
        if base and not worked:
            idle.append(label)
    return [(":warning: " if idle else "") + "Legacy presets: " + "; ".join(parts) + "."
            + (f" No follow-up on {', '.join(idle)}." if idle else "")]


def build_audit(models: list[dict], day: str, caller: str, legacy: dict | None = None) -> str:
    out = [f"*Caller audit {day} ({caller})*"]
    out += work_time(models, caller)
    if legacy:
        out += legacy_preset_lines(models, legacy, caller)
    dials = [(m, c) for m in models for c in m["calls"] if c["by"] == caller and c["dir"] == "outbound"]
    if not dials:
        out.append("No outbound calls logged for this caller today.")
        return "\n".join(out)
    # One smrtPhone call to a number shared by several owners is logged on each record, so the
    # header counts each call once (same key as kpi_scorecard._call_key); the per-record checks
    # below still look at every record the call was logged on.
    uniq = list({(c.get("id") or (c["from"], c["to"], c["ct"])): c for _, c in dials}.values())
    by_line = Counter(c["from"] for c in uniq)
    by_hour = Counter(c["ct"].hour for c in uniq)
    first, last = min(c["ct"] for c in uniq), max(c["ct"] for c in uniq)
    dup = len(dials) - len(uniq)
    out.append(f"Dials {len(uniq)} (floor {DIAL_FLOOR}) on {len({m['uuid'] for m, _ in dials})} records, {first:%H:%M} to {last:%H:%M}. "
               f"Answered {sum(1 for c in uniq if c['result'] == 'answered')}, no answer {sum(1 for c in uniq if c['result'] == 'noanswer')}."
               + (f" ({dup} more log rows are the same calls on other records.)" if dup else ""))
    line_txt = ", ".join(f"...{l[-4:]}={n}{' OVER CAP' if n > MAX_PER_LINE else ''}" for l, n in by_line.most_common())
    unused = [l[-4:] for l in CALL_LINES if l not in by_line]
    s0, s1 = int(SHIFT[0][:2]), int(SHIFT[1][:2])
    out.append("Dials by hour: " + ", ".join(f"{h if h <= 12 else h - 12}{'am' if h < 12 else 'pm'} {by_hour[h]}"
                                             for h in range(min(s0, *by_hour), max(s1 - 1, *by_hour) + 1)) + ".")
    out.append(f"Lines: {line_txt}" + (f"; unused call lines ...{', ...'.join(unused)}" if unused else ""))
    if first.hour * 60 + first.minute > 10 * 60 + 30:
        out.append(f":warning: First dial at {first:%H:%M} (plan: 10:05).")

    unmarked = [(m["street"], c["to"][-4:], tier(m, c["to"])) for m, c in dials if final_mark(m, c["to"], caller) is None]
    unmarked = sorted(set(unmarked))
    if unmarked:
        out.append(f":warning: {len(unmarked)} dialed numbers never marked: " + "; ".join(f"{s} ...{n} ({t})" for s, n, t in unmarked[:12]) + (" ..." if len(unmarked) > 12 else ""))
    low = [(m["street"], c["to"][-4:], tier(m, c["to"])) for m, c in dials if tier(m, c["to"]) not in ("Dial First", "Dial Second")]
    if low:
        out.append(f":warning: {len(low)} dials on non Dial First/Second numbers: " + "; ".join(f"{s} ...{n} ({t})" for s, n, t in low[:10]) + (" ..." if len(low) > 10 else ""))
    no_bump = sorted({m["street"] for m, _ in dials if m["status"] == "prospecting" and not m["attempt_bumps"]})
    if no_bump:
        out.append(f":warning: {len(no_bump)} records called, still Prospecting, Call Attempts not bumped: " + "; ".join(no_bump[:12]))
    early_bump = []
    for m in {m["uuid"]: m for m, _ in dials}.values():
        if not m["attempt_bumps"]:
            continue
        dialed = {c["to"] for c in m["calls"] if c["by"] == caller and c["dir"] == "outbound"}
        left = [n for n, p in m["phones"].items() if _eligible(p) and n not in dialed]
        if left:
            early_bump.append(f"{m['street']} (undialed ...{', ...'.join(n[-4:] for n in left)})")
    if early_bump:
        out.append(f":warning: Call Attempts bumped with an eligible number still undialed: " + "; ".join(early_bump[:10]))
    live = [(m["street"], c["to"][-4:], c["dur"]) for m, c in dials
            if c["result"] == "answered" and (c["dur"] or 0) >= 45 and not m["notes"] and final_mark(m, c["to"], caller) != "NO_ANSWER"]
    if live:
        out.append(f":eyes: {len(live)} answered calls of 45s+ not marked No Answer and no note (possible conversation): " + "; ".join(f"{s} ...{n} {d}s" for s, n, d in live[:10]))
    callbacks, wrong_not_dnc = [], []
    for m in models:
        if "responder test" in m["street"].lower():
            continue
        # A number that texted in and ends the day Wrong (not Wrong + DNC) can still be texted again.
        for n in sorted({s["from"] for s in m["sms_in"]}):
            if (m["phones"].get(n) or {}).get("status") == "WRONG":
                wrong_not_dnc.append(f"{m['street']} ...{n[-4:]}")
        # After-shift inbounds are tomorrow's work, not a miss today.
        # Sign-offs and STOPs need no reply; a STOP, or a text from a number already DNC, is handled by the responder.
        inbound = sorted(t for t in [c["ct"] for c in m["calls"] if c["dir"] == "inbound"]
                         + [s["ct"] for s in m["sms_in"] if not is_ack(s["text"]) and not is_stop(s["text"])
                            and (m["phones"].get(s["from"]) or {}).get("status") not in DNC_MARKS]
                         if f"{t:%H:%M}" < SHIFT[1])
        if not inbound or m["notes"] or (m["open_tasks"] or 0) or any(x["by"] == caller and x["to"] not in (None, "prospecting") for x in m["status_changes"]):
            continue
        # A text or call back, or a phone mark (e.g. Wrong Number), from the caller after the last inbound counts as handled.
        replies = ([s["ct"] for s in m["sms_out"] if s["by"] == caller] + [c["ct"] for c in m["calls"] if c["by"] == caller and c["dir"] == "outbound"]
                   + [x["ct"] for x in m["marks"] if x["by"] == caller or x["to"] in DNC_MARKS])
        if any(r > inbound[-1] for r in replies):
            continue
        callbacks.append(f"{m['street']} ({len(inbound)} inbound, last {inbound[-1]:%H:%M})")
    if callbacks:
        out.append(f":warning: Inbound call/text with no reply, note, phone mark or open task: " + "; ".join(callbacks[:10]))
    if wrong_not_dnc:
        out.append(f":warning: Texted in, marked Wrong but not Wrong + DNC: " + "; ".join(wrong_not_dnc[:10]))
    vm_due = {(m["uuid"], c["to"]) for m, c in dials if c["result"] == "answered" and (c["dur"] or 0) >= MIN_VM_SECONDS and final_mark(m, c["to"], caller) == "NO_ANSWER"}
    pm_sent = sum(1 for m in models for s in m["sms_out"] if s["from"] in PM_LINES)
    out.append(f"Afternoon texts: {len(vm_due)} voicemail numbers, {pm_sent} texts sent from afternoon lines.")
    statuses = Counter(s["to"] for m in models for s in m["status_changes"] if s["by"] == caller)
    out.append("Statuses set: " + (", ".join(f"{k}={v}" for k, v in statuses.most_common()) or "none") + f". Notes added: {sum(len(m['notes']) for m in models)}.")
    batch_idle = sorted(m["street"] for m in models if m["assigned_to"] == CALLER_USER_UUID and m["status"] == "prospecting"
                        and any(str(t).startswith("Batch ") for t in m["tags"]) and not any(c["by"] == caller for c in m["calls"])
                        and any(_eligible(p) for p in m["phones"].values()))
    if batch_idle:
        out.append(f":warning: Batch records in Prospecting with an eligible number and no call today: " + "; ".join(batch_idle[:12]))
    legacy = [(m, c) for m, c in dials if c["from"] in LEGACY_LINES]
    legacy_untagged = sorted({m["street"] for m, _ in legacy if not any(str(t).startswith(LEGACY_TAG_PREFIX) for t in m["tags"])})
    out.append(f"Legacy: {len(legacy)} calls from the legacy lines on {len({m['uuid'] for m, _ in legacy})} records"
               + (f"; :warning: not tagged after the call: {'; '.join(legacy_untagged[:10])}" if legacy_untagged else "") + ".")
    # Rule: call, voicemail, then a text to the same number from the same line.
    legacy_no_text = sorted({f"{m['street']} ({c['to'][-4:]})" for m, c in legacy
                             if c["result"] == "answered" and (c["dur"] or 0) >= MIN_VM_SECONDS
                             and final_mark(m, c["to"], caller) == "NO_ANSWER"
                             and not any(s["from"] == c["from"] and s["to"] == c["to"] and s["ct"] >= c["ct"] for s in m["sms_out"])})
    if legacy_no_text:
        out.append(f":warning: Legacy voicemail with no text after it from the same line: {'; '.join(legacy_no_text[:10])}")
    legacy_wrong_line = sorted({m["street"] for m, c in legacy for s in m["sms_out"]
                                if s["manual"] and s["to"] == c["to"] and s["from"] != c["from"]})
    if legacy_wrong_line:
        out.append(f":warning: Legacy text sent from a different line than the call: {'; '.join(legacy_wrong_line[:10])}")
    legacy_sent = Counter(s["from"] for m in models for s in m["sms_out"] if s["from"] in LEGACY_LINES)
    over = [f"{n[-4:]}={k}" for n, k in sorted(legacy_sent.items()) if k > LEGACY_TEXT_CAP]
    if over:
        out.append(f":warning: Legacy line over {LEGACY_TEXT_CAP} texts today: {', '.join(over)}")
    return "\n".join(out)


def _in_window(spec: str) -> bool:
    """spec 'HH-HH' or 'HH:MM-HH:MM' in Central time; True when now is inside [start, end)."""
    a, b = spec.split("-")
    def mins(x):
        h, _, mm = x.partition(":")
        return int(h) * 60 + int(mm or 0)
    now = ct_now()
    return mins(a) <= now.hour * 60 + now.minute < mins(b)


def run_audit(args) -> int:
    """7:15 PM caller audit. Writes audit_posted_<day>.json (read by the next-morning
    audit-check) and exits 1 when the report could not be posted, so the workflow alert fires."""
    if args.only_between and not _in_window(args.only_between):
        print(f"Outside window {args.only_between} Central (now {ct_now():%H:%M}); skipping.")
        # Only the dispatcher's run is meant to land inside the window; the off-season GitHub
        # fallback cron is outside it every day, so that one skips quietly.
        if getattr(args, "alert_skip", False) and args.slack:
            _slack(f":warning: End-of-day audit did not run: dispatched at {ct_now():%H:%M} Central, "
                   f"outside its window {args.only_between}. Re-run it from Actions (mode audit).")
        return 0
    api = Sift(_env("REISIFT_API_KEY") or sys.exit("REISIFT_API_KEY missing"))
    day = args.day or ct_now().date().isoformat()
    stats = {"selected": 0, "skipped": 0, "errors": 0}
    try:
        legacy = load_legacy_presets(api, day)
    except RuntimeError as e:
        print(f"(legacy presets skipped: {e})")
        stats["errors"] += 1
        legacy = None
    models = load_day(api, day, CALLER_EMAIL, stats)
    report = build_audit(models, day, CALLER_EMAIL, legacy)
    try:
        import kpi_scorecard
        report += "\n" + kpi_scorecard.daily_lines(models, CALLER_EMAIL, day=dt.date.fromisoformat(day), stats=stats)
    except Exception as e:
        report += f"\n(KPI scorecard failed: {e})"
    print(report)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"audit_{day}.md").write_text(report, encoding="utf-8")
    posted = _slack(report) if args.slack else False
    (STATE_DIR / f"audit_posted_{day}.json").write_text(json.dumps(
        {"day": day, "posted": posted, "at": ct_now().isoformat(timespec="minutes"), "stats": stats}), encoding="utf-8")
    return 1 if args.slack and not posted else 0


def prev_business_day(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def audit_marker_problem(folder: Path, day: str) -> str | None:
    """None when some audit_posted_<day>.json under `folder` says the report was posted."""
    hits = sorted(folder.rglob(f"audit_posted_{day}.json")) if folder.exists() else []
    if not hits:
        return "no audit run left a marker (it did not run, skipped its window, failed or timed out)"
    for h in hits:
        try:
            if json.loads(h.read_text(encoding="utf-8")).get("posted") is True:
                return None
        except ValueError:
            continue
    return "the audit ran but its report was not posted to Slack"


def run_audit_check(args) -> int:
    """Next-morning check that the previous business day's audit ran and posted its report.
    A skipped or green-but-empty run does not count. `--markers` is a folder of downloaded
    audit artifacts."""
    day = args.day or prev_business_day(ct_now().date()).isoformat()
    problem = audit_marker_problem(Path(args.markers), day)
    if not problem:
        print(f"Audit for {day} completed and posted.")
        return 0
    text = f":warning: End-of-day audit for {day} is missing: {problem}. Re-run it from Actions (mode audit, day {day})."
    print(text)
    if args.slack:
        _slack(text)
    return 1


# ----------------------------------------------------------------- am start
PRESET_01_TITLE = "01. New - Set to Prospecting"
SMS_LINE_TAGS = ("SMS Line A", "SMS Line B", "SMS Line C", "SMS Line D", "SMS Line E")
# Temporary reduced cohort (approved 2026-10-27): at most 9 new records per line per Monday.
# 9 is a ceiling, not a target: line_capacity() lowers it for texts the line already sent today,
# drips still running on it, and the chance that DataSift sends a new record's opener twice.
MAX_NEW_PER_LINE = 9
CAP_MARGIN = 2


def line_capacity(started_today: int, sent_today: int, running_drips: int) -> int:
    """New records a line can still take today without risking MAX_PER_LINE texts.
    Each running drip may send today's text plus a duplicate (2), each new record its opener plus
    a duplicate (2), and CAP_MARGIN texts stay free for a manual reply."""
    room = (MAX_PER_LINE - sent_today - 2 * running_drips - CAP_MARGIN) // 2
    return max(0, min(MAX_NEW_PER_LINE - started_today, room))


def _preset_must(api: Sift, title_prefix: str) -> dict:
    rows = api.call("/api/internal/filter-preset/?limit=500")
    rows = rows.get("results") or rows.get("data") or rows
    for pz in rows:
        if str(pz.get("title", "")).startswith(title_prefix):
            return (pz.get("filters") or {}).get("must") or {}
    raise RuntimeError(f"preset {title_prefix!r} not found")


def _best_primary(phones: dict) -> str | None:
    for want in ("Dial First", "Dial Second"):
        for n, ph in phones.items():
            if ph.get("type") == "MOBILE" and want in (ph.get("tags") or []) and ph.get("status") not in BAD_MARKS:
                return n
    return None


def _primary_num(owner: dict) -> str:
    primary = owner.get("primary_phone")
    return re.sub(r"\D", "", str(primary.get("number") if isinstance(primary, dict) else primary or ""))[-10:]


def load_capacity(api: Sift, started_today: Counter) -> dict[str, dict]:
    """{line tag: {sent, running, started, capacity}} from today's sends and running P1 drips.
    Raises RuntimeError when either can't be read; the caller must then start nothing."""
    now = ct_now()
    sent, _ = count_sent_by_line(api, now.date(), now)
    running = Counter(drip_line(h) for h in drip_histories(api) if h.get("status") == "running")
    out = {}
    for tag in SMS_LINE_TAGS:
        k = {"sent": sent[SMS_LINE_NUMBERS[tag]], "running": running[tag], "started": started_today[tag]}
        out[tag] = {**k, "capacity": line_capacity(k["started"], k["sent"], k["running"])}
    return out


def am_start_gate(det: dict, stop: dict, drips: list[dict] | None, batch_tag: str | None) -> str | None:
    """Why a record must NOT be set to Prospecting, or None when it is a clean, ready Batch record."""
    owner = det.get("owner") or {}
    phones = {ph["number"]: ph for ph in (owner.get("phones") or [])}
    lines = [t for t in (det.get("tags") or []) if t in SMS_LINE_TAGS]
    primary = _primary_num(owner)
    if det.get("status"):
        return f"record already has status {det.get('status')!r} (only clean records start)"
    if batch_tag and batch_tag not in (det.get("tags") or []):
        return f"missing tag {batch_tag!r}"
    if det.get("assigned_to") != CALLER_USER_UUID:
        return "not assigned to the caller"
    if not (owner.get("first_name") or "").strip():
        return "owner has no first name (text would read 'Hey ,')"
    if len(lines) != 1:
        return f"needs exactly one SMS Line tag (has {len(lines)})"
    if not primary or primary not in phones:
        best = _best_primary(phones)
        return "no primary phone" + (f" (best candidate ...{best[-4:]})" if best else " and no Dial First/Second mobile")
    if phones[primary].get("type") != "MOBILE" or phones[primary].get("status") in BAD_MARKS:
        return f"primary phone is {phones[primary].get('type')}/{phones[primary].get('status')}"
    if stop["hold"]:
        return f"STOP policy hold ({stop['why']})"
    if primary in stop["blocked"]:
        return "primary phone is DNC"
    if drips is None:
        return "drip history unreadable"
    if drips:
        return f"already has drip history ({', '.join(sorted({str(x.get('status')) for x in drips}))})"
    return None


def load_ready_file(path: str | None, day: str) -> tuple[dict | None, str | None]:
    """(marker, problem) for the Friday readiness marker written by weekly_topup.py validate."""
    if not path:
        return None, "no readiness marker given"
    f = Path(path)
    if not f.exists():
        return None, f"readiness marker {f.name} not found (Friday prep did not finish)"
    try:
        mk = json.loads(f.read_text(encoding="utf-8"))
    except ValueError as e:
        return None, f"readiness marker unreadable: {e}"
    if mk.get("batch_date") != day:
        return mk, f"readiness marker is for {mk.get('batch_date')}, not {day}"
    if not mk.get("ok"):
        return mk, f"readiness marker says NOT ready: {mk.get('problem') or 'see Friday report'}"
    if not mk.get("uuids"):
        return mk, "readiness marker lists no records"
    return mk, None


def plan_am_start(api: Sift, candidates: list[str], batch_tag: str | None, capacity: dict[str, dict],
                  marker_lines: dict | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """Split candidate records into (ready, held, errors). Records are taken in candidate order
    until each line's capacity is used up; anything unreadable is held, never started."""
    ready, held, errors = [], [], []
    left = {k: v["capacity"] for k, v in capacity.items()}
    for u in candidates:
        try:
            det = api.call(f"/api/internal/property/{u}/")
        except RuntimeError as ex:
            errors.append(f"{u}: {str(ex)[:120]}")
            held.append({"uuid": u, "street": "?", "owner_first": "", "line": None, "primary": "", "why": "record unreadable"})
            continue
        owner = det.get("owner") or {}
        lines = [t for t in (det.get("tags") or []) if t in SMS_LINE_TAGS]
        e = {"uuid": u, "street": (det.get("address") or {}).get("street") or "", "owner_first": (owner.get("first_name") or "").strip(),
             "line": lines[0] if len(lines) == 1 else None, "primary": _primary_num(owner),
             "status": det.get("status"), "assigned": det.get("assigned_to")}
        stop = record_stop(api, u, det)
        try:
            drips = drip_rows(api, u)
        except RuntimeError as ex:
            errors.append(f"{e['street']}: drip history {str(ex)[:100]}")
            drips = None
        why = am_start_gate(det, stop, drips, batch_tag)
        if not why and marker_lines and marker_lines.get(u) not in (None, e["line"]):
            why = f"line changed since Friday ({marker_lines.get(u)} -> {e['line']})"
        if not why and left.get(e["line"], 0) <= 0:
            c = capacity.get(e["line"]) or {}
            why = (f"{e['line']} has no room today (sent {c.get('sent')}, running drips {c.get('running')}, "
                   f"started {c.get('started')}, ceiling {MAX_NEW_PER_LINE})")
        if why:
            held.append({**e, "why": why})
        else:
            left[e["line"]] -= 1
            ready.append(e)
    return ready, held, errors


def _alert(args, text: str) -> int:
    print(text)
    if getattr(args, "slack", False):
        _slack(text)
    return 1


def run_am_start(args) -> int:
    """Monday 9:50 AM: set Friday's validated Batch records to Prospecting, which starts the drip.
    Live mode needs the Friday readiness marker and readable line capacity; otherwise it starts
    nothing and exits 1 with an alert."""
    live = bool(args.send)
    now = ct_now()
    if live and not args.force and args.only_between and not _in_window(args.only_between):
        print(f"Outside window {args.only_between} Central (now {now:%H:%M}); nothing started.")
        return 0
    day = now.date().isoformat()
    batch_date = args.batch_date or day
    if live and now.weekday() != 0:
        print(f"am-start launches new records on Mondays only (today is {now:%A}); nothing started.")
        return 0
    api = Sift(_env("REISIFT_API_KEY") or sys.exit("REISIFT_API_KEY missing"))
    marker, problem = load_ready_file(args.ready_file, batch_date)
    if live and problem:
        return _alert(args, f":x: Morning start {day}: NOT launched. {problem}. No record was changed.")
    state_file = STATE_DIR / f"am_start_{day}.json"
    prior = json.loads(state_file.read_text(encoding="utf-8")).get("results", []) if state_file.exists() else []
    try:
        capacity = load_capacity(api, Counter(r.get("line") for r in prior if r.get("ok") and r.get("line")))
    except RuntimeError as ex:
        return _alert(args, f":x: Morning start {day}: NOT launched. Line capacity unreadable ({str(ex)[:200]}). No record was changed.")
    if marker and not problem:
        candidates, marker_lines = list(marker["uuids"]), marker.get("lines") or {}
        source = f"readiness marker ({len(candidates)} records)"
    else:
        candidates, marker_lines = [r["uuid"] for r in api.search(_preset_must(api, PRESET_01_TITLE))], None
        source = f"preset 01 ({len(candidates)} records; dry run only: {problem})"
    ready, held, errors = plan_am_start(api, candidates, f"Batch {batch_date}", capacity, marker_lines)
    out = [f"Morning start for {day} ({'LIVE' if live else 'DRY RUN'}), Batch {batch_date}, from {source}: "
           f"{len(ready)} to set Prospecting, {len(held)} held back",
           "Line room: " + "; ".join(f"{k[4:]} sent {v['sent']}, running {v['running']}, started {v['started']} -> {v['capacity']}"
                                     for k, v in capacity.items())]
    out += [f"  START {e['street']} | {e['owner_first']} | {e['line']} | primary ...{e['primary'][-4:]}" for e in ready]
    out += [f"  HOLD  {e['street']} | {e['owner_first'] or '(no name)'} | {e['why']}" for e in held]
    if errors:
        out.append(f":warning: {len(errors)} read errors (those records held): " + "; ".join(errors[:5]))
    results, rc = [], 1 if errors else 0
    if live and ready:
        for e in ready:
            try:
                api.call(f"/api/internal/property/{e['uuid']}/status/", "POST", {"status": "prospecting"})
                results.append({**e, "ok": True})
            except RuntimeError as ex:
                results.append({**e, "ok": False, "err": str(ex)[:200]})
        time.sleep(90)
        no_drip = []
        for r in results:
            if not r["ok"]:
                continue
            try:
                running = [x for x in drip_rows(api, r["uuid"]) if x.get("status") == "running"]
            except RuntimeError as ex:
                no_drip.append(f"{r['street']} (unreadable: {str(ex)[:60]})")
                continue
            r["running_drips"] = len(running)
            if len(running) != 1:
                no_drip.append(f"{r['street']} ({len(running)} running)")
        fails = [r for r in results if not r["ok"]]
        out.append(f"Set Prospecting on {len(results) - len(fails)}; failed {len(fails)}" + "".join(f"\n  FAILED {r['street']}: {r['err']}" for r in fails))
        out.append(":white_check_mark: every started record has exactly one running drip" if not no_drip else ":warning: drip check: " + "; ".join(no_drip))
        rc = 1 if (fails or no_drip or errors) else 0
    report = "\n".join(out)
    print(report)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"ready": ready, "blocked": held, "capacity": capacity, "results": prior + results},
                                     indent=1, default=str), encoding="utf-8")
    if args.slack:
        _slack(report)
    return rc


# ----------------------------------------------------------- extra numbers
# A confirmation text to each extra mobile on a prospecting record, plus one follow-up on a later day
# if it never replied. Calls and the drip stay on #1/#2.
EXTRA_TEMPLATE = "Hi, trying to reach {first} about {street}. Is this still a good number? - Britney"
EXTRA_FOLLOWUP_TEMPLATE = "Following up: is this {first}'s number? It's about {street}. - Britney"
SMS_LINE_NUMBERS = {"SMS Line A": "8162545235", "SMS Line B": "8169553316", "SMS Line C": "8167889952",
                    "SMS Line D": "8168971724", "SMS Line E": "8168971637"}
EXTRA_TAGS = ("Dial First", "Dial Second", "Dial Third", "Dial Fourth")
EXTRA_OK_MARKS = {"", "UNKNOWN", "NO_ANSWER"}
EXTRA_LEDGER = STATE_DIR / "extra_texts_sent.json"


def _d10(x) -> str:
    return re.sub(r"\D", "", str(x or ""))[-10:]


def extra_numbers(det: dict, history: dict[str, dict], ledger: dict, today) -> tuple[list[tuple[str, int]], list[tuple[str, str]]]:
    """([(number, stage)], [(number, why skipped)]) for one property detail.
    history = {number: {"sent": [(Central date, from line or None)], "replied": bool}} from its SMS logs.
    Stage 1 = never texted. Stage 2 = exactly one earlier text, from an SMS Line, on a past day, no reply."""
    owner = det.get("owner") or {}
    primary = owner.get("primary_phone")
    primary_num = _d10(primary.get("number") if isinstance(primary, dict) else primary)
    send, skip = [], []
    for ph in owner.get("phones") or []:
        n = _d10(ph.get("number"))
        if not n or n == primary_num:
            continue
        tags = ph.get("tags") or []
        h = history.get(n) or {"sent": [], "replied": False}
        led = ledger.get(n) or {}
        why, stage = None, None
        if not any(t in tags for t in EXTRA_TAGS):
            why = "not Dial First-Fourth"
        elif ph.get("type") != "MOBILE":
            why = f"type {ph.get('type')}"
        elif (ph.get("status") or "").upper() not in EXTRA_OK_MARKS:
            why = f"marked {ph.get('status')}"
        elif h["replied"]:
            why = "replied"
        elif not h["sent"]:
            # DataSift can lag on API sends; the local ledger covers a same-day rerun.
            why, stage = ("already sent a confirmation text", None) if led else (None, 1)
        elif len(h["sent"]) > 1 or led.get("stage") == 2:
            why = "already followed up"
        elif h["sent"][0][1] is None:
            why = "already has SMS history"
        elif h["sent"][0][0] >= today:
            why = "first text was today"
        else:
            stage = 2
        (skip.append((n, why)) if why else send.append((n, stage)))
    return send, skip


def plan_extra_texts(api: Sift, prior_line_counts: Counter | None = None, ledger: dict | None = None) -> tuple[list[dict], list[dict], dict]:
    """Every extra number to text on prospecting records assigned to the caller: (planned, skipped, texts already sent today per line)."""
    ledger = ledger or {}
    today = ct_now().date()
    line_of = {v: k for k, v in SMS_LINE_NUMBERS.items()}
    # Pass 1: every prospecting record's detail + logs, and today's outbound texts per line from
    # every source (drip, manual, earlier extras), so the cap covers the line's whole day.
    rows, sent_today = [], Counter()
    for row in api.search({"any_property_status": ["prospecting"]}):
        u = row["uuid"]
        det = api.call(f"/api/internal/property/{u}/")
        evs = (api.call(f"/api/internal/property/{u}/logs/?limit=250&offset=0") or {}).get("results") or []
        for e in evs:
            if e.get("event_type") == "owner.sms.sent":
                t = to_ct(e.get("timestamp", ""))
                line = line_of.get(_d10(((e.get("payload") or {}).get("sms") or {}).get("origin_number")))
                if line and t and t.date() == today:
                    sent_today[line] += 1
        rows.append((u, det, evs))
    # DataSift can lag a few seconds on API sends, so never count fewer than this job's own record.
    per_line = Counter({k: max(sent_today[k], (prior_line_counts or {}).get(k, 0)) for k in SMS_LINE_NUMBERS})
    prior_totals = dict(per_line)
    planned, skipped = [], []
    for u, det, evs in rows:
        owner = det.get("owner") or {}
        street = (det.get("address") or {}).get("street") or ""
        first = (owner.get("first_name") or "").strip()
        lines = [t for t in (det.get("tags") or []) if t in SMS_LINE_TAGS]
        base = {"uuid": u, "street": street, "owner_first": first, "line": lines[0] if len(lines) == 1 else None}
        why = None
        if det.get("assigned_to") != CALLER_USER_UUID:
            why = "not assigned to the caller"
        elif not first:
            why = "owner has no first name"
        elif len(lines) != 1:
            why = f"needs exactly one SMS Line tag (has {len(lines)})"
        elif (stop := stop_policy(owner, _all_phones(det), dnc_origins(evs)))["hold"]:
            why = f"STOP policy hold ({stop['why']})"
        elif any((ph.get("status") or "").upper() in ("CORRECT", "CORRECT_DNC") for ph in owner.get("phones") or []):
            why = "owner's number already confirmed Correct"
        if why:
            skipped.append({**base, "number": None, "why": why})
            continue
        history, started = {}, None
        for e in evs:
            sms = (e.get("payload") or {}).get("sms") or {}
            if e.get("event_type") == "owner.sms.sent":
                t = to_ct(e.get("timestamp", ""))
                h = history.setdefault(_d10(sms.get("destination_number")), {"sent": [], "replied": False})
                h["sent"].append((t.date() if t else today, line_of.get(_d10(sms.get("origin_number")))))
            elif e.get("event_type") == "owner.sms.received":
                history.setdefault(_d10(sms.get("origin_number")), {"sent": [], "replied": False})["replied"] = True
            if e.get("event_type") == "property.status.updated":
                to = (((e.get("payload") or {}).get("property") or {}).get("status") or [None, None])[-1]
                t = to_ct(e.get("timestamp", ""))
                if to == "prospecting" and t and (started is None or t > started):
                    started = t
        base["started"] = started.date().isoformat() if started else None
        send, skip = extra_numbers(det, history, ledger, today)
        for n, w in skip:
            skipped.append({**base, "number": n, "why": w})
        for n, stage in send:
            if per_line[base["line"]] >= MAX_PER_LINE:
                skipped.append({**base, "number": n, "why": f"{base['line']} hit {MAX_PER_LINE} today"})
                continue
            per_line[base["line"]] += 1
            tpl = EXTRA_TEMPLATE if stage == 1 else EXTRA_FOLLOWUP_TEMPLATE
            planned.append({**base, "number": n, "stage": stage, "from": SMS_LINE_NUMBERS[base["line"]],
                            "message": tpl.format(first=first.title(), street=street.title())})
    return planned, skipped, prior_totals


def run_extra_texts(args) -> int:
    live = bool(args.send)
    if live and not args.force and args.only_between and not _in_window(args.only_between):
        print(f"Outside window {args.only_between} Central (now {ct_now():%H:%M}); nothing sent.")
        return 0
    api = Sift(_env("REISIFT_API_KEY") or sys.exit("REISIFT_API_KEY missing"))
    day = ct_now().date().isoformat()
    state_file = STATE_DIR / f"extra_texts_{day}.json"
    prior = json.loads(state_file.read_text(encoding="utf-8")).get("results", []) if state_file.exists() else []
    ledger = json.loads(EXTRA_LEDGER.read_text(encoding="utf-8")) if EXTRA_LEDGER.exists() else {}
    planned, skipped, already = plan_extra_texts(api, Counter(r["line"] for r in prior if r.get("ok")), ledger)
    by_line = Counter(p["line"] for p in planned)
    n2 = sum(p["stage"] == 2 for p in planned)
    lines = [f"Extra-number texts for {day} ({'LIVE' if live else 'DRY RUN'}): {len(planned)} planned ({len(planned) - n2} first, {n2} follow-up), {len(skipped)} skipped"]
    for k in SMS_LINE_NUMBERS:
        lines.append(f"  {k}: {already.get(k, 0)} already sent today + {by_line[k]} planned = {already.get(k, 0) + by_line[k]}/{MAX_PER_LINE}")
    for p in planned:
        lines.append(f"  DAY {p['stage']} {p['street']} | {p['owner_first']} | ...{p['number'][-4:]} from {p['line']}")
    results = []
    if live and planned:
        token = _env("SMRTPHONE_API_TOKEN") or sys.exit("SMRTPHONE_API_TOKEN missing")
        for p in planned:
            ok, resp = send_sms(token, p["from"], p["number"], p["message"])
            results.append({**p, "ok": ok, "resp": resp})
            if ok:
                ledger[p["number"]] = {"day": day, "uuid": p["uuid"], "line": p["line"], "stage": p["stage"]}
            time.sleep(2)
        fails = [r for r in results if not r["ok"]]
        lines.append(f"Sent {len(results) - len(fails)}; failed {len(fails)}" + "".join(f"\n  FAILED ...{r['number'][-4:]}: {r['resp']}" for r in fails))
        EXTRA_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        EXTRA_LEDGER.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    report = "\n".join(lines)
    print(report)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"planned": planned, "skipped": skipped, "results": prior + results}, indent=1, default=str), encoding="utf-8")
    if args.slack:
        _slack(report)
    return 0


def _slack(text: str, chunk: int = 3500) -> bool:
    """Post to the webhook, splitting long reports into several messages at line boundaries."""
    url = _env("SLACK_WEBHOOK_URL")
    if not url:
        print("(no SLACK_WEBHOOK_URL; not posted)")
        return False
    parts, cur = [], ""
    for line in text.splitlines():
        if cur and len(cur) + len(line) + 1 > chunk:
            parts.append(cur)
            cur = ""
        cur = (cur + "\n" + line) if cur else line
    if cur:
        parts.append(cur)
    for i, part in enumerate(parts, 1):
        body = part if len(parts) == 1 else f"({i}/{len(parts)})\n{part}"
        req = urllib.request.Request(url, data=json.dumps({"text": body}).encode(), headers={"content-type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:  # noqa: BLE001
            print(f"(Slack post failed: {e})")
            return False
        time.sleep(1)
    print(f"(posted to Slack in {len(parts)} message(s))")
    return True


# ----------------------------------------------------------------- line watch
# Every smrtPhone number we text from. line-watch counts today's sends per line and
# alerts once when a line crosses 20 (then again at 25, 30, ...) so nobody keeps
# texting from it and risks the carrier spam flag.
OUR_LINES = {
    "8162545235", "8169553316", "8167889952", "8166084051", "8167100226",
    "8167100554", "8167889485", "8167595422", "8167100448", "8167100146",
    "8169553574", "8167889386", "5054941645", "4057641708", "5055201714",
    "8168971724", "8168971637",
}
LINE_ALERT_AT = 20
LINE_ALERT_STEP = 5


def fmt_phone(n) -> str:
    d = _d10(n)
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else str(n)


def line_crossings(before: int, now: int, threshold: int = LINE_ALERT_AT, step: int = LINE_ALERT_STEP) -> list[int]:
    """Thresholds (20, 25, 30, ...) passed going from `before` to `now`, i.e. in (before, now]."""
    out, t = [], threshold
    while t <= now:
        if t > before:
            out.append(t)
        t += step
    return out


def count_sent_by_line(sift: Sift, day: dt.date, cutoff: dt.datetime) -> tuple[Counter, Counter]:
    """Today's (Central) owner.sms.sent counts per line, overall and up to `cutoff` (Central)."""
    total, upto = Counter(), Counter()
    path = "/api/internal/property/logs/?limit=100"
    for _ in range(500):
        r = sift.call(path)
        rows = r.get("results") or []
        older = False
        for e in rows:
            t = to_ct(e.get("timestamp"))
            if t is None:
                continue
            if t.date() < day:
                older = True
                continue
            if t.date() > day or e.get("event_type") != "owner.sms.sent":
                continue
            line = _d10(((e.get("payload") or {}).get("sms") or {}).get("origin_number"))
            if line not in OUR_LINES:
                continue
            total[line] += 1
            if t.replace(tzinfo=None) <= cutoff.replace(tzinfo=None):
                upto[line] += 1
        cur = r.get("next_cursor")
        if older or not cur or not rows:
            break
        path = "/api/internal/property/logs/?limit=100&cursor=" + urllib.parse.quote(str(cur))
    return total, upto


def _google_chat(text: str) -> None:
    url = _env("GOOGLE_CHAT_WEBHOOK_URL")
    if not url:
        print("(no GOOGLE_CHAT_WEBHOOK_URL; not posted)")
        return
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"content-type": "application/json; charset=UTF-8"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
        print("(posted to Google Chat)")
    except Exception as e:  # noqa: BLE001
        print(f"(Google Chat post failed: {e})")


def run_line_watch(args) -> int:
    now = ct_now()
    if args.day:
        day = dt.date.fromisoformat(args.day)
        now = dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=now.tzinfo)
        cutoff = dt.datetime.combine(day, dt.time(0, 0), tzinfo=now.tzinfo)   # backtest: every crossing that day
    else:
        day = now.date()
        cutoff = now - dt.timedelta(minutes=args.window_min)
    api = Sift(_env("REISIFT_API_KEY") or sys.exit("REISIFT_API_KEY missing"))
    total, upto = count_sent_by_line(api, day, cutoff)
    print(f"Texts sent today ({day}) by line:")
    for line, n in total.most_common():
        print(f"  {fmt_phone(line)}  {n}")
    if not total:
        print("  (none)")
    alerts = []
    for line, n in total.most_common():
        for t in line_crossings(upto[line], n, args.threshold, LINE_ALERT_STEP):
            alerts.append(f"Line {fmt_phone(line)} has sent {t} texts today — stop texting from it.")
    for a in alerts:
        print("ALERT: " + a)
        if not args.no_post:
            _google_chat(a)
            _slack(a)
    return 0


def line_label(n) -> str:
    """Which of our lines a number is, for the call-watch message."""
    d = _d10(n)
    for name, num in SMS_LINE_NUMBERS.items():
        if num == d:
            return name
    if d in PM_LINES:
        return "afternoon line"
    if d in CALL_LINES:
        return "call line"
    if d == CALLBACK_LINE:
        return "callback line"
    return "line"


def group_inbound_calls(events: list[dict]) -> list[dict]:
    """Inbound owner.call.* events to our lines, one entry per call (a call fans out
    to every owner with that number, so events are grouped by the call's external_id)."""
    calls: dict[str, dict] = {}
    for e in events:
        if not str(e.get("event_type") or "").startswith("owner.call."):
            continue
        p = e.get("payload") or {}
        c = p.get("call") or {}
        if c.get("direction") != "inbound":
            continue
        line, caller = _d10(c.get("destination_number")), _d10(c.get("origin_number"))
        if line not in OUR_LINES or caller in OUR_LINES:
            continue
        key = c.get("external_id") or c.get("uuid")
        t = to_ct(e.get("timestamp"))
        if not key or t is None:
            continue
        g = calls.setdefault(key, {"caller": caller, "line": line, "first": t, "last": t, "names": [],
                                   "duration": 0, "recording": None, "missed": False, "final": False})
        g["first"] = min(g["first"], t)
        g["last"] = max(g["last"], t)
        o = (p.get("extra_info") or {}).get("owner") or {}
        name = " ".join(x for x in (o.get("first_name"), o.get("last_name")) if x).strip()
        if name and name not in g["names"]:
            g["names"].append(name)
        g["duration"] = max(g["duration"], int(c.get("duration") or 0))
        g["recording"] = g["recording"] or c.get("recording")
        status = (c.get("extra") or {}).get("status")
        if e.get("event_type") == "owner.call.missed" or status == "missed":
            g["missed"] = True
        # owner.call.received lands first with no outcome; missed/answered follows 9s-3.5min later
        if e.get("event_type") in ("owner.call.missed", "owner.call.answered") or status in ("missed", "answered"):
            g["final"] = True
    return sorted(calls.values(), key=lambda g: g["first"])


def call_message(g: dict) -> str:
    who = ", ".join(g["names"][:3]) + (f" +{len(g['names']) - 3} more" if len(g["names"]) > 3 else "")
    if g["recording"]:
        outcome = "left a voicemail" if g["missed"] else f"talked {g['duration']}s"
    elif g["duration"] > 0:
        outcome = f"answered, {g['duration']}s"
    else:
        outcome = "missed call, no voicemail"
    msg = (f"Incoming call: {fmt_phone(g['caller'])}" + (f" ({who})" if who else " (not in DataSift)")
           + f" called {line_label(g['line'])} {fmt_phone(g['line'])} at {g['first']:%I:%M %p} — {outcome}.")
    if g["recording"]:
        msg += f" Recording: {g['recording']}"
    return msg


def fetch_events_since(api: "Sift", cutoff: dt.datetime) -> list[dict]:
    """Every feed event newer than cutoff (the feed is newest first)."""
    out, cur = [], None
    while True:
        path = "/api/internal/property/logs/?limit=100" + (f"&cursor={urllib.parse.quote(str(cur))}" if cur else "")
        r = api.call(path) or {}
        page = r.get("results") or []
        for e in page:
            t = to_ct(e.get("timestamp"))
            if t is not None and t.replace(tzinfo=None) > cutoff.replace(tzinfo=None):
                out.append(e)
        last = to_ct(page[-1].get("timestamp")) if page else None
        cur = r.get("next_cursor")
        if not page or not cur or (last is not None and last.replace(tzinfo=None) <= cutoff.replace(tzinfo=None)):
            return out


def run_call_watch(args) -> int:
    """Post every inbound call to our lines (missed, answered or voicemail) to Google Chat
    and Slack. No saved state: each run reports calls whose final (missed/answered) event
    landed in the last window, so a call posts once, after DataSift has its outcome."""
    now = ct_now()
    if args.day:
        day = dt.date.fromisoformat(args.day)
        now = dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=now.tzinfo)
        cutoff = dt.datetime.combine(day, dt.time(0, 0), tzinfo=now.tzinfo)
    else:
        cutoff = now - dt.timedelta(minutes=args.window_min)
    api = Sift(_env("REISIFT_API_KEY") or sys.exit("REISIFT_API_KEY missing"))
    calls = [g for g in group_inbound_calls(fetch_events_since(api, cutoff))
             if g["final"] and cutoff.replace(tzinfo=None) < g["last"].replace(tzinfo=None) <= now.replace(tzinfo=None)]
    print(f"call-watch: {len(calls)} inbound call(s) since {cutoff:%Y-%m-%d %H:%M} CT")
    for g in calls:
        msg = call_message(g)
        print("CALL: " + msg)
        if not args.no_post:
            _google_chat(msg)
            _slack(msg)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("pm-texts", help="afternoon voicemail follow-up texts")
    p.add_argument("--send", action="store_true", help="actually send (default is dry run)")
    p.add_argument("--day", help="YYYY-MM-DD to backtest (dry run only)")
    p.add_argument("--force", action="store_true", help="ignore the Central send window")
    p.add_argument("--slack", action="store_true")
    p.add_argument("--only-between", help="Central window like 16:15-17:00; outside it a --send run does nothing")
    p.set_defaults(fn=run_pm_texts)
    a = sub.add_parser("audit", help="end-of-day caller checklist")
    a.add_argument("--day")
    a.add_argument("--slack", action="store_true")
    a.add_argument("--only-between", help="Central window like 19:00-20:00; outside it the run does nothing")
    a.add_argument("--alert-skip", action="store_true", help="post a Slack warning when --only-between skips the run")
    a.set_defaults(fn=run_audit)
    ac = sub.add_parser("audit-check", help="alert when the previous business day's audit did not run and post")
    ac.add_argument("--day", help="audit day to check (default: previous business day)")
    ac.add_argument("--markers", default=str(STATE_DIR / "audit_markers"), help="folder of downloaded audit artifacts")
    ac.add_argument("--slack", action="store_true")
    ac.set_defaults(fn=run_audit_check)
    m = sub.add_parser("am-start", help="set preset-01 records to Prospecting (starts the morning drip)")
    m.add_argument("--send", action="store_true", help="actually change statuses (default is dry run)")
    m.add_argument("--force", action="store_true")
    m.add_argument("--slack", action="store_true")
    m.add_argument("--only-between", help="Central window like 09:50-12:00")
    m.add_argument("--ready-file", help="Friday readiness marker (topup_ready_<batch>.json); required for --send")
    m.add_argument("--batch-date", help="YYYY-MM-DD of the Batch tag (default today)")
    m.set_defaults(fn=run_am_start)
    x = sub.add_parser("extra-texts", help="one confirmation text to each extra mobile number on prospecting records")
    x.add_argument("--send", action="store_true", help="actually send (default is dry run)")
    x.add_argument("--force", action="store_true")
    x.add_argument("--slack", action="store_true")
    x.add_argument("--only-between", help="Central window like 10:30-12:00")
    x.set_defaults(fn=run_extra_texts)
    w = sub.add_parser("line-watch", help="alert Google Chat + Slack when a line sends 20 texts in a day")
    w.add_argument("--day", help="YYYY-MM-DD to backtest (reports every crossing that day)")
    w.add_argument("--window-min", type=int, default=5, help="only alert on crossings in the last N minutes")
    w.add_argument("--threshold", type=int, default=LINE_ALERT_AT)
    w.add_argument("--no-post", action="store_true", help="print only, don't post")
    w.set_defaults(fn=run_line_watch)
    cw = sub.add_parser("call-watch", help="post inbound calls/voicemails on our lines to Google Chat + Slack")
    cw.add_argument("--day", help="YYYY-MM-DD: report every inbound call that day (backtest)")
    cw.add_argument("--window-min", type=int, default=6, help="report calls that started in the last N minutes")
    cw.add_argument("--no-post", action="store_true", help="print only, don't post")
    cw.set_defaults(fn=run_call_watch)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
