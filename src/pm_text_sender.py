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

Env: REISIFT_API_KEY, SMRTPHONE_API_TOKEN (pm-texts --send), SLACK_WEBHOOK_URL
(optional). Caller/lines are constants below; change them there when the
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
DRIP_LINES = {"8162545235", "8169553316", "8167889952"}
CALLBACK_LINE = "8169553574"
# Legacy follow-up lines: KC (816) 788-9386, OK (405) 764-1708, NM (505) 520-1714.
# Each lead is called, voicemailed and texted from its own market's line.
LEGACY_LINES = {"8167889386", "4057641708", "5055201714"}
LEGACY_TEXT_CAP = 20
LEGACY_TAG_PREFIX = "Legacy Recall"
MAX_PER_LINE = 25
MIN_VM_SECONDS = 20
SHIFT = ("10:00", "19:00")   # Rahaf's agreed shift, Central (Kaycie, 2026-10-21)
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
        u = dt.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
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
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    t = r.read().decode()
                    return json.loads(t) if t.strip().startswith(("{", "[")) else t
            except urllib.error.HTTPError as e:
                txt = e.read().decode(errors="replace")
                if e.code == 429 and attempt < 5:
                    m = re.search(r"available in (\d+)\s*second", txt, re.I)
                    time.sleep(min((int(m.group(1)) + 1) if m else 5 * (attempt + 1), 300))
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


def load_day(api: Sift, day: str, caller: str) -> list[dict]:
    """Every record with activity on `day` (Central), modelled for the rules below."""
    d0 = dt.date.fromisoformat(day)
    rows = api.search({"property_type": "clean", "updated": [(d0 - dt.timedelta(days=1)).isoformat(), (d0 + dt.timedelta(days=1)).isoformat()]})
    models = []
    for rec in rows:
        u = rec.get("uuid")
        try:
            evs = (api.call(f"/api/internal/property/{u}/logs/?limit=250&offset=0") or {}).get("results") or []
        except RuntimeError:
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
        det = api.call(f"/api/internal/property/{u}/")
        owner = det.get("owner") or {}
        phones = {p["number"]: p for p in (owner.get("phones") or [])}
        for so in det.get("secondary_owners") or []:
            for p in so.get("phones") or []:
                phones.setdefault(p["number"], p)
        calls: dict[str, dict] = {}
        for e in today:
            et = e["event_type"]
            if et.startswith("owner.call."):
                c = e["payload"]["call"]
                cc = calls.setdefault(c["uuid"], {"ct": e["_ct"], "from": c.get("origin_number") or "", "to": c.get("destination_number") or "",
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
            "phones": phones, "open_tasks": rec.get("tasks") if isinstance(rec.get("tasks"), int) else None,
            "calls": sorted(calls.values(), key=lambda c: c["ct"]), "call_days": all_call_days,
            "sms_out": [{"ct": e["_ct"], "from": e["payload"]["sms"].get("origin_number") or "", "to": e["payload"]["sms"].get("destination_number") or "",
                         "manual": bool((e["payload"]["sms"].get("external_user") or {}).get("email")),
                         "by": (e["payload"]["sms"].get("external_user") or {}).get("email")} for e in today if e["event_type"] == "owner.sms.sent"],
            "sms_in": [{"ct": e["_ct"], "from": e["payload"]["sms"].get("origin_number") or "", "text": e["payload"]["sms"].get("message") or ""}
                       for e in today if e["event_type"] == "owner.sms.received"],
            "marks": [{"ct": e["_ct"], "phone": e["payload"]["owner"].get("phone"), "to": (e["payload"]["owner"].get("status") or [None, None])[-1], "by": _who(e)}
                      for e in today if e["event_type"] == "owner.phone.status.updated"],
            "status_changes": [{"ct": e["_ct"], "to": (e["payload"]["property"].get("status") or [None, None])[-1], "by": _who(e)} for e in today if e["event_type"] == "property.status.updated"],
            "attempt_bumps": [e["_ct"] for e in today if e["event_type"] == "property.predictivecall.attempts.increased" and _who(e) == caller],
            "notes": [e["_ct"] for e in today if e["event_type"] in ("property.message.added", "property.message.updated") and _who(e) == caller],
            "actions": [e["_ct"] for e in today if _actor(e) == caller],
            "task_events": sum(1 for e in today if "task" in e["event_type"] and _actor(e) == caller),
            "tags_added": [(t, _who(e)) for e in today if e["event_type"] == "property.tags.added" for t in ((e.get("payload") or {}).get("tags") or [])],
        }
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
            elif m["owner_dnc"] or m["owner_opt_out"]:
                why = "owner flagged DNC/opt-out"
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
    by_line = Counter(c["from"] for _, c in dials)
    by_hour = Counter(c["ct"].hour for _, c in dials)
    first, last = min(c["ct"] for _, c in dials), max(c["ct"] for _, c in dials)
    out.append(f"Dials {len(dials)} (floor {DIAL_FLOOR}) on {len({m['uuid'] for m, _ in dials})} records, {first:%H:%M} to {last:%H:%M}. "
               f"Answered {sum(1 for _, c in dials if c['result'] == 'answered')}, no answer {sum(1 for _, c in dials if c['result'] == 'noanswer')}.")
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
    if args.only_between and not _in_window(args.only_between):
        print(f"Outside window {args.only_between} Central (now {ct_now():%H:%M}); skipping.")
        return 0
    api = Sift(_env("REISIFT_API_KEY") or sys.exit("REISIFT_API_KEY missing"))
    day = args.day or ct_now().date().isoformat()
    try:
        legacy = load_legacy_presets(api, day)
    except RuntimeError as e:
        print(f"(legacy presets skipped: {e})")
        legacy = None
    report = build_audit(load_day(api, day, CALLER_EMAIL), day, CALLER_EMAIL, legacy)
    print(report)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"audit_{day}.md").write_text(report, encoding="utf-8")
    if args.slack:
        _slack(report)
    return 0


# ----------------------------------------------------------------- am start
PRESET_01_TITLE = "01. New - Set to Prospecting"
SMS_LINE_TAGS = ("SMS Line A", "SMS Line B", "SMS Line C")
MAX_NEW_PER_LINE = 25


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


def plan_am_start(api: Sift, prior_line_counts: Counter | None = None) -> tuple[list[dict], list[dict]]:
    """Records preset 01 would show right now, split into (ready, blocked) with the reason for each block.
    prior_line_counts = records already started per line earlier today, so the daily cap holds across runs."""
    must = _preset_must(api, PRESET_01_TITLE)
    rows = api.search(must)
    ready, blocked, per_line = [], [], Counter(prior_line_counts or {})
    for row in rows:
        u = row["uuid"]
        det = api.call(f"/api/internal/property/{u}/")
        owner = det.get("owner") or {}
        phones = {ph["number"]: ph for ph in (owner.get("phones") or [])}
        lines = [t for t in (det.get("tags") or []) if t in SMS_LINE_TAGS]
        first = (owner.get("first_name") or "").strip()
        primary = owner.get("primary_phone")
        primary_num = str(primary.get("number") if isinstance(primary, dict) else primary or "")
        primary_num = re.sub(r"\D", "", primary_num)[-10:]
        e = {"uuid": u, "street": (det.get("address") or {}).get("street") or "", "owner_first": first, "line": lines[0] if len(lines) == 1 else None,
             "primary": primary_num, "status": det.get("status"), "assigned": det.get("assigned_to")}
        why = None
        if det.get("status") == "prospecting":
            why = "already prospecting"
        elif not first:
            why = "owner has no first name (text would read 'Hey ,')"
        elif len(lines) != 1:
            why = f"needs exactly one SMS Line tag (has {len(lines)})"
        elif not primary_num or primary_num not in phones:
            why = "no primary phone" + (f" (best candidate ...{_best_primary(phones)[-4:]})" if _best_primary(phones) else " and no Dial First/Second mobile")
        elif phones[primary_num].get("type") != "MOBILE" or phones[primary_num].get("status") in BAD_MARKS:
            why = f"primary phone is {phones[primary_num].get('type')}/{phones[primary_num].get('status')}"
        elif det.get("assigned_to") != CALLER_USER_UUID:
            why = "not assigned to the caller"
        elif per_line[lines[0]] >= MAX_NEW_PER_LINE:
            why = f"{lines[0]} already has {MAX_NEW_PER_LINE} new records today"
        if why:
            blocked.append({**e, "why": why})
        else:
            per_line[lines[0]] += 1
            ready.append(e)
    return ready, blocked


def run_am_start(args) -> int:
    live = bool(args.send)
    if live and not args.force and args.only_between and not _in_window(args.only_between):
        print(f"Outside window {args.only_between} Central (now {ct_now():%H:%M}); nothing started.")
        return 0
    api = Sift(_env("REISIFT_API_KEY") or sys.exit("REISIFT_API_KEY missing"))
    day = ct_now().date().isoformat()
    state_file = STATE_DIR / f"am_start_{day}.json"
    prior = json.loads(state_file.read_text(encoding="utf-8")).get("results", []) if state_file.exists() else []
    ready, blocked = plan_am_start(api, Counter(r.get("line") for r in prior if r.get("ok") and r.get("line")))
    lines = [f"Morning start for {day} ({'LIVE' if live else 'DRY RUN'}): {len(ready)} to set Prospecting, {len(blocked)} held back"]
    for e in ready:
        lines.append(f"  START {e['street']} | {e['owner_first']} | {e['line']} | primary ...{e['primary'][-4:]}")
    for e in blocked:
        lines.append(f"  HOLD  {e['street']} | {e['owner_first'] or '(no name)'} | {e['why']}")
    results = []
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
            h = api.call(f"/api/internal/drip-campaign-history/by-property/?property_uuid={r['uuid']}")
            running = [x for x in (h.get("results") or []) if x.get("status") == "running"]
            r["running_drips"] = len(running)
            if len(running) != 1:
                no_drip.append(f"{r['street']} ({len(running)} running)")
        fails = [r for r in results if not r["ok"]]
        lines.append(f"Set Prospecting on {len(results) - len(fails)}; failed {len(fails)}" + "".join(f"\n  FAILED {r['street']}: {r['err']}" for r in fails))
        lines.append(":white_check_mark: every started record has exactly one running drip" if not no_drip else ":warning: drip check: " + "; ".join(no_drip))
    report = "\n".join(lines)
    print(report)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"ready": ready, "blocked": blocked, "results": prior + results}, indent=1, default=str), encoding="utf-8")
    if args.slack:
        _slack(report)
    return 0


# ----------------------------------------------------------- extra numbers
# One confirmation text, ever, to each extra mobile on a prospecting record. Calls and the drip stay on #1/#2.
EXTRA_TEMPLATE = "Hi, trying to reach {first} about {street}. Is this still a good number? - Britney"
SMS_LINE_NUMBERS = {"SMS Line A": "8162545235", "SMS Line B": "8169553316", "SMS Line C": "8167889952"}
EXTRA_TAGS = ("Dial First", "Dial Second", "Dial Third")
EXTRA_OK_MARKS = {"", "UNKNOWN", "NO_ANSWER"}
EXTRA_LEDGER = STATE_DIR / "extra_texts_sent.json"


def _d10(x) -> str:
    return re.sub(r"\D", "", str(x or ""))[-10:]


def extra_numbers(det: dict, texted: set[str], ledger: set[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """(numbers to text, [(number, why skipped)]) for one property detail. texted = numbers seen in its SMS logs."""
    owner = det.get("owner") or {}
    primary = owner.get("primary_phone")
    primary_num = _d10(primary.get("number") if isinstance(primary, dict) else primary)
    send, skip = [], []
    for ph in owner.get("phones") or []:
        n = _d10(ph.get("number"))
        if not n or n == primary_num:
            continue
        tags = ph.get("tags") or []
        why = None
        if not any(t in tags for t in EXTRA_TAGS):
            why = "not Dial First/Second/Third"
        elif ph.get("type") != "MOBILE":
            why = f"type {ph.get('type')}"
        elif (ph.get("status") or "").upper() not in EXTRA_OK_MARKS:
            why = f"marked {ph.get('status')}"
        elif n in texted:
            why = "already has SMS history"
        elif n in ledger:
            why = "already sent a confirmation text"
        (skip.append((n, why)) if why else send.append(n))
    return send, skip


def plan_extra_texts(api: Sift, prior_line_counts: Counter | None = None, ledger: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Every extra number to text on prospecting records assigned to the caller, split into (planned, skipped)."""
    ledger = ledger or set()
    per_line = Counter(prior_line_counts or {})
    planned, skipped = [], []
    for row in api.search({"any_property_status": ["prospecting"]}):
        u = row["uuid"]
        det = api.call(f"/api/internal/property/{u}/")
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
        elif owner.get("dnc") or owner.get("opt_out"):
            why = "owner DNC/opt-out"
        if why:
            skipped.append({**base, "number": None, "why": why})
            continue
        evs = (api.call(f"/api/internal/property/{u}/logs/?limit=250&offset=0") or {}).get("results") or []
        texted, started = set(), None
        for e in evs:
            sms = (e.get("payload") or {}).get("sms") or {}
            if e.get("event_type") in ("owner.sms.sent", "owner.sms.received"):
                texted |= {_d10(sms.get("origin_number")), _d10(sms.get("destination_number"))}
            if e.get("event_type") == "property.status.updated":
                to = (((e.get("payload") or {}).get("property") or {}).get("status") or [None, None])[-1]
                t = to_ct(e.get("timestamp", ""))
                if to == "prospecting" and t and (started is None or t > started):
                    started = t
        base["started"] = started.date().isoformat() if started else None
        send, skip = extra_numbers(det, texted, ledger)
        for n, w in skip:
            skipped.append({**base, "number": n, "why": w})
        for n in send:
            if per_line[base["line"]] >= MAX_PER_LINE:
                skipped.append({**base, "number": n, "why": f"{base['line']} hit {MAX_PER_LINE} today"})
                continue
            per_line[base["line"]] += 1
            planned.append({**base, "number": n, "from": SMS_LINE_NUMBERS[base["line"]],
                            "message": EXTRA_TEMPLATE.format(first=first.title(), street=street.title())})
    return planned, skipped


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
    planned, skipped = plan_extra_texts(api, Counter(r["line"] for r in prior if r.get("ok")), set(ledger))
    by_line = Counter(p["line"] for p in planned)
    lines = [f"Extra-number texts for {day} ({'LIVE' if live else 'DRY RUN'}): {len(planned)} planned "
             f"({', '.join(f'{k} {v}' for k, v in sorted(by_line.items())) or 'none'}), {len(skipped)} skipped"]
    for p in planned:
        lines.append(f"  TEXT {p['street']} | {p['owner_first']} | ...{p['number'][-4:]} from {p['line']}")
    results = []
    if live and planned:
        token = _env("SMRTPHONE_API_TOKEN") or sys.exit("SMRTPHONE_API_TOKEN missing")
        for p in planned:
            ok, resp = send_sms(token, p["from"], p["number"], p["message"])
            results.append({**p, "ok": ok, "resp": resp})
            if ok:
                ledger[p["number"]] = {"day": day, "uuid": p["uuid"], "line": p["line"]}
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


def _slack(text: str, chunk: int = 3500) -> None:
    """Post to the webhook, splitting long reports into several messages at line boundaries."""
    url = _env("SLACK_WEBHOOK_URL")
    if not url:
        print("(no SLACK_WEBHOOK_URL; not posted)")
        return
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
            return
        time.sleep(1)
    print(f"(posted to Slack in {len(parts)} message(s))")


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
    a.set_defaults(fn=run_audit)
    m = sub.add_parser("am-start", help="set preset-01 records to Prospecting (starts the morning drip)")
    m.add_argument("--send", action="store_true", help="actually change statuses (default is dry run)")
    m.add_argument("--force", action="store_true")
    m.add_argument("--slack", action="store_true")
    m.add_argument("--only-between", help="Central window like 09:50-12:00")
    m.set_defaults(fn=run_am_start)
    x = sub.add_parser("extra-texts", help="one confirmation text to each extra mobile number on prospecting records")
    x.add_argument("--send", action="store_true", help="actually send (default is dry run)")
    x.add_argument("--force", action="store_true")
    x.add_argument("--slack", action="store_true")
    x.add_argument("--only-between", help="Central window like 10:30-12:00")
    x.set_defaults(fn=run_extra_texts)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
