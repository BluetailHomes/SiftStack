"""Friday deep prospecting for Jackson obituary records: who died, who the heirs are, their phones,
written back to DataSift for review.

Why this exists
  Ty (Oct 13 workshop, 1:49:57) deep prospects every obituary record: an owner skip trace reaches
  ~5% of these, the heirs reach 60-70%. DataSift's Obituary list matches owners NAMED in an
  obituary, so most records are living owners whose relative died (the 2026-11-07 pilot: 2-3 heir
  cases in 10). This job screens each record first and only pays for heir research when the owner
  is the one who died.

What it does, per record (Obit 3-6mo / Obit 6mo+, Jackson MO, not yet researched, unassigned,
no lead/active/decline status, suppression-clean, obituary at least 90 days old, freshest first)
  1. Screen (Claude + web search): whose obituary is it, date of death, the survivors paragraph.
     The 3-year rule: a death more than 3 years before DataSift's obituary date is a stale match.
  2. Owner deceased: one SmartSkip bulk trace for the run (relatives WITH phones), SmartSkip
     relatives matched to the obituary's survivors (the obituary's relationship wins; the LLM
     never adds a person), Trestle scores, Drop and litigator numbers left out.
  3. Writes: heir phones merged onto the owner (tagged tier + "DP Heir" / "DP Owner Line" +
     "SmartSkip", never a person's name), one message-board note with the call order, and the tags
     "DP Review" + "DP Researched". A living owner gets a warning note (don't ask for the deceased)
     and the same tags. Not found / stale: "DP Researched" only.

Review gate (first 2-3 weeks)
  Nothing reaches Rahaf until Kaycie swaps "DP Review" for "Deep Prospecting" (owner deceased) or
  removes "DP Review" (living owner). DP Review blocks every automated text (outreach_block).

Modes
  python src/obituary_dp.py                   # plan: who would be screened; free, no writes
  python src/obituary_dp.py --screen          # + Claude screen (~$0.50/record); no SmartSkip, no writes
  python src/obituary_dp.py --live --slack    # screen + SmartSkip + Trestle + DataSift writes

Env: REISIFT_API_KEY, ANTHROPIC_API_KEY; --live also SMARTSKIP_EMAIL, SMARTSKIP_PASSWORD,
TRESTLE_API_KEY; SLACK_WEBHOOK_URL for --slack.

Safety rules
  - Paid calls are capped per run: --max-records screens, --max-skip SmartSkip rows, --max-score phones.
  - Existing owner phones are never removed or retagged (one PATCH, re-read right before it,
    readback checked). Every record's before/after goes to
    output/account_backups/obit_dp_write_{date}.json, even on a crash.
  - A paid SmartSkip order that can't be downloaded tags its records "DP Researched" (so they are
    never paid for twice) and the run pack in output/pm_texts/ keeps the order id for --resume.
  - Status, assignment, owner name, and campaign tags are never touched.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import smartskip  # noqa: E402
from llm_client import _extract_response_text  # noqa: E402
from p1_enrich import LITIGATOR_TAG, score_all, tier_for  # noqa: E402
from pm_text_sender import DEEP_PROSPECTING_TAG, DP_REVIEW_TAG, STATE_DIR, Sift, _env, _slack, _tag_key, ct_now  # noqa: E402
from weekly_topup import (ACTIVE_STATUSES, BACKUP_DIR, DECLINE_STATUSES, _rows, add_tags, digits,  # noqa: E402
                          ensure_tag, suppress_reason, tag_uuids)

OBIT_TAGS = {"390ebcc0-277c-42da-938f-7ebd7cc01f1a": "Obit 3-6mo", "f91785a5-e043-4972-842e-3ae48054870e": "Obit 6mo+"}
# Tag on the "Jackson - Obituary - P1 Neighborhoods" SiftMap auto-add preset. Its records may never get
# the Obit age tags above, so plan() searches it too (once the preset has created it).
PULL_TAG = "Obituary P1"
RESEARCHED_TAG = "DP Researched"
HEIR_PHONE_TAG, OWNER_LINE_TAG, SOURCE_TAG = "DP Heir", "DP Owner Line", "SmartSkip"
MIN_OBIT_AGE_DAYS = 90       # Ty: give the family 3 months
STALE_DOD_YEARS = 3          # death this long before the obituary date = wrong person
MAX_RECORDS, MAX_SKIP, MAX_SCORE = 10, 10, 120
SCREEN_COST, SCORE_COST = 0.50, 0.02
CALL_TIERS = ("Dial First", "Dial Second")
TIER_ORDER = {"Dial First": 1, "Dial Second": 2, "Dial Third": 3, "Dial Fourth": 4}
CLOSE_RELS = {"Child", "Spouse", "Sibling"}
LINE_TYPES = {"mobile": "MOBILE", "landline": "LANDLINE"}
MODEL = "claude-opus-5-5"
# The 25 neighborhoods on every "Jackson - Priority 1" SiftMap preset (config.TARGET_NEIGHBORHOODS).
# Paid research stays inside them, like P1. A record with no neighborhood on file passes (never drop
# on missing data).
P1_NEIGHBORHOODS = config.TARGET_NEIGHBORHOODS


def _title(t) -> str:
    return t if isinstance(t, str) else (t.get("title") or "")


def _date(v) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def owner_name(d: dict) -> str:
    o = d.get("owner") or {}
    return f"{(o.get('first_name') or '').strip()} {(o.get('last_name') or '').strip()}".strip()


# ----------------------------------------------------------------- select
def closed_out_on(api: Sift, uuid: str) -> dt.date | None:
    """Date the record last moved to close_out, from its activity log, or None if not found."""
    try:
        evs = (api.call(f"/api/internal/property/{uuid}/logs/?limit=250&offset=0") or {}).get("results") or []
    except RuntimeError:
        return None
    days = [_date(e.get("timestamp")) for e in evs if e.get("event_type") == "property.status.updated"
            and ((((e.get("payload") or {}).get("property") or {}).get("status") or [None])[-1] == "close_out")]
    return max((x for x in days if x), default=None)


def eligible(d: dict, today: dt.date, county: str = "Jackson", state: str = "MO",
             closed_on: dt.date | None = None) -> str | None:
    """Why this property detail can't be researched this run, or None.
    A close_out record (worked and closed earlier) is only researched when its obituary is dated
    after the close-out, i.e. someone died after we stopped working it. `closed_on` is that date."""
    keys = {_tag_key(t) for t in d.get("tags") or []}
    for t in (RESEARCHED_TAG, DP_REVIEW_TAG[1], DEEP_PROSPECTING_TAG[1]):
        if _tag_key(t) in keys:
            return f"tagged {t!r}"
    a = d.get("address") or {}
    if county.lower() not in str(a.get("county") or "").lower() or str(a.get("state") or "").upper() != state:
        return "other county"
    hood = re.sub(r",\s*mo$", "", str(d.get("neighborhood") or "").strip().lower())
    if hood and county in P1_NEIGHBORHOODS and hood not in P1_NEIGHBORHOODS[county]:
        return "outside P1 neighborhoods"
    o = d.get("owner") or {}
    if (o.get("type") or "person").lower() not in ("person", "individual") or not (o.get("first_name") or "").strip():
        return "not a person"
    status = str(d.get("status") or "")
    if status == "close_out":
        od = _date(d.get("last_obituary_date"))
        if not (closed_on and od and od > closed_on):
            return f"status close_out (closed {closed_on or 'date unknown'}, obituary {od or 'none'})"
    elif status in ACTIVE_STATUSES or status in DECLINE_STATUSES or re.search(r"lead|contract|closed|sold", status, re.I):
        return f"status {status}"
    if d.get("assigned_to"):
        return "assigned"
    # suppress_reason() also refuses a deceased owner, which here is the point.
    if (why := suppress_reason(d, today)) and why != "owner flagged deceased":
        return why
    od = _date(d.get("last_obituary_date"))
    if od and (today - od).days < MIN_OBIT_AGE_DAYS:
        return f"obituary {od} (under {MIN_OBIT_AGE_DAYS} days)"
    return None


def plan(api: Sift, county: str, today: dt.date) -> dict:
    tags = tag_uuids(api)
    skip = [tags[t] for t in (RESEARCHED_TAG, DP_REVIEW_TAG[1], DEEP_PROSPECTING_TAG[1]) if t in tags]
    must = {"any_tags": list(OBIT_TAGS) + ([tags[PULL_TAG]] if PULL_TAG in tags else [])}
    if skip:
        must["must_not"] = {"any_tags": skip}
    picks, counts = [], Counter()
    for row in api.search(must):
        d = api.call(f"/api/internal/property/{row['uuid']}/")
        closed_on = closed_out_on(api, d["uuid"]) if d.get("status") == "close_out" else None
        why = eligible(d, today, county, closed_on=closed_on)
        counts[why.split(" ")[0] if why else "eligible"] += 1
        if not why:
            picks.append(d)
    # Freshest obituary first; a missing date is kept, last.
    picks.sort(key=lambda d: str(d.get("last_obituary_date") or ""), reverse=True)
    return {"records": picks, "counts": dict(counts)}


# ----------------------------------------------------------------- screen
SCREEN_PROMPT = """You are screening a real-estate lead whose owner appears in an obituary. Use web search.

Property: {address}
Owner of record: {owner}{age}
Other owners on title: {co_owners}
Personal representative on file: {pr}
DataSift obituary date (publication): {obit_date}

Steps, in order:
1. Find the obituary DataSift matched (Kansas City area, near the publication date). Decide WHOSE
   obituary it is. If the owner of record is alive and a relative died (spouse, parent, child),
   that is "relative_deceased" - not an heir case. If another owner on title died, "co_owner_deceased".
   Only "owner_deceased" when the decedent is the owner of record (match name, age, city, address).
2. Copy the survivors paragraph verbatim ("survived by ...", "preceded in death by" is NOT survivors).
3. List each survivor with the relationship the obituary gives (son, daughter, wife, brother, ...)
   and city if given. Married daughters appear under their married surname; give the name as printed.
4. Date of death with its source. If sources conflict, say so in notes; never silently pick one.
Never invent a person or a relationship. If you cannot find the obituary, say "not_found".

Reply with ONLY one JSON object, no prose:
{{"classification": "owner_deceased|relative_deceased|co_owner_deceased|not_found",
  "decedent_name": "", "relationship_to_owner": "self|wife|husband|mother|father|son|daughter|other",
  "date_of_death": "YYYY-MM-DD or empty", "dod_source": "",
  "obituary_links": [""], "survivors_verbatim": "",
  "survivors": [{{"name": "", "relationship": "", "city": ""}}],
  "confidence": "high|medium|low", "notes": ""}}"""


def screen_prompt(d: dict) -> str:
    a, o = d.get("address") or {}, d.get("owner") or {}
    co = ", ".join(f"{s.get('first_name') or ''} {s.get('last_name') or ''}".strip()
                   for s in d.get("secondary_owners") or []) or "none"
    return SCREEN_PROMPT.format(address=f"{a.get('street') or ''}, {a.get('city') or ''}, {a.get('state') or ''} {a.get('postal_code') or ''}",
                                owner=owner_name(d), age=f" (age {o['age']})" if o.get("age") else "",
                                co_owners=co, pr=d.get("personal_representative") or "none",
                                obit_date=d.get("last_obituary_date") or "unknown")


def parse_json(text: str) -> dict:
    """The last top-level JSON object in the model's reply."""
    end = text.rfind("}")
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            depth += {"}": 1, "{": -1}.get(text[i], 0)
            if depth == 0:
                try:
                    return json.loads(text[i:end + 1])
                except ValueError:
                    break
        end = text.rfind("}", 0, end)
    raise ValueError("no JSON object in the reply")


def screen(client, d: dict) -> dict:
    msgs = [{"role": "user", "content": screen_prompt(d)}]
    for _ in range(5):
        resp = client.beta.messages.create(
            model=MODEL, max_tokens=16000, thinking={"type": "adaptive"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}],
            messages=msgs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        if resp.stop_reason == "pause_turn":
            msgs = msgs[:1] + [{"role": "assistant", "content": resp.content}]
            continue
        if resp.stop_reason == "refusal":
            return {"classification": "not_found", "confidence": "low", "notes": "model refused"}
        return parse_json(_extract_response_text(resp.content))
    return {"classification": "not_found", "confidence": "low", "notes": "search did not finish"}


def sanity(s: dict, obit_date: dt.date | None, today: dt.date) -> dict:
    """Downgrade a match whose date of death can't be this obituary's."""
    s = dict(s)
    dod = _date(s.get("date_of_death"))
    if dod and dod > today:
        s.update(classification="not_found", notes=f"date of death {dod} is in the future; " + (s.get("notes") or ""))
    elif dod and obit_date and (obit_date - dod).days > STALE_DOD_YEARS * 365:
        s.update(classification="not_found",
                 notes=f"stale match: died {dod}, over {STALE_DOD_YEARS} years before the obituary date {obit_date}; " + (s.get("notes") or ""))
    return s


# ----------------------------------------------------------------- heirs
def _toks(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z]+", (name or "").lower()) if len(t) > 1]


def matches(survivor: str, rel: dict) -> bool:
    """SmartSkip relative = obituary survivor: same first name, and the relative's surname appears
    in the survivor's printed name (catches "Jane (Doe) Smith" and married names printed in full)."""
    st = _toks(survivor)
    first, last = _toks(rel.get("first"))[:1], _toks(rel.get("last"))[-1:]
    return bool(st and first and last and st[0] == first[0] and last[0] in st)


def heirs(s: dict, rec: dict) -> tuple[list[dict], list[str]]:
    """(people to call, survivors with no SmartSkip match). People come only from SmartSkip; the
    obituary confirms them and supplies the relationship."""
    live = [r for r in rec.get("relatives") or [] if not r.get("deceased")]
    people, missing, used = [], [], set()
    for sv in s.get("survivors") or []:
        hit = next((i for i, r in enumerate(live) if i not in used and matches(sv.get("name", ""), r)), None)
        if hit is None:
            missing.append(f"{sv.get('name')} ({sv.get('relationship') or '?'})")
            continue
        used.add(hit)
        people.append({"name": live[hit]["name"], "relationship": (sv.get("relationship") or live[hit]["rel"]).title(),
                       "confirmed": True, "phones": live[hit]["phones"], "owner_line": False})
    if not s.get("survivors"):
        people += [{"name": r["name"], "relationship": f"{r['rel']} (unconfirmed)", "confirmed": False,
                    "phones": r["phones"], "owner_line": False} for r in live if r["rel"] in CLOSE_RELS]
    if rec.get("subject_phones"):
        people.append({"name": "Decedent's own lines", "relationship": "owner", "confirmed": True,
                       "phones": rec["subject_phones"], "owner_line": True})
    return people, missing


def numbers_to_score(people: list[dict], existing: set[str]) -> list[str]:
    """Heirs: mobiles and untyped numbers (landlines rarely reach an heir). Owner lines: all."""
    out = []
    for p in people:
        for ph in p["phones"]:
            n = ph["number"]
            if n in existing or n in out:
                continue
            if p["owner_line"] or not ph.get("type") or ph["type"][0].upper() in "MO":
                out.append(n)
    return out


def merge_phones(before: list[dict], people: list[dict], scores: dict) -> tuple[list[dict], list[dict]]:
    """(full new phone list, added). Existing phones untouched; Drop / litigator / unscored left out."""
    out, have, added = [dict(p) for p in before], {digits(p["number"]) for p in before}, []
    for p in people:
        for ph in p["phones"]:
            n, sc = ph["number"], scores.get(ph["number"]) or {}
            if n in have or sc.get("error") or sc.get("litigator") or "score" not in sc:
                continue
            tier = tier_for(sc["score"])
            if not tier or tier == "Drop":
                continue
            typ = LINE_TYPES.get(str(sc.get("line_type") or "").lower()) or LINE_TYPES.get(ph.get("type", "").lower(), "UNKNOWN")
            out.append({"number": n, "type": typ, "status": "UNKNOWN", "is_connected": True,
                        "tags": [tier, OWNER_LINE_TAG if p["owner_line"] else HEIR_PHONE_TAG, SOURCE_TAG]})
            added.append({"number": n, "tier": tier, "line_type": typ, "who": p["name"], "relationship": p["relationship"]})
            have.add(n)
    return out, added


# ----------------------------------------------------------------- notes
def heir_note(s: dict, added: list[dict], missing: list[str], today: dt.date) -> str:
    lines = [f"DEEP PROSPECTING {today} ({s.get('confidence') or '?'} confidence) - PENDING REVIEW",
             f"Deceased: {s.get('decedent_name') or '?'} (owner of record)",
             f"Date of death: {s.get('date_of_death') or 'unknown'}" + (f" ({s['dod_source']})" if s.get("dod_source") else ""), "",
             "Call order (last 4 digits, Dial First/Second only; others are on the record with tier tags):"]
    by_who: dict[str, list[dict]] = {}
    for a in added:
        if a["tier"] in CALL_TIERS:
            by_who.setdefault(f"{a['who']} - {a['relationship']}", []).append(a)
    for k, (who, ph) in enumerate(sorted(by_who.items(), key=lambda kv: min(TIER_ORDER[p["tier"]] for p in kv[1])), 1):
        ph.sort(key=lambda p: TIER_ORDER[p["tier"]])
        lines.append(f"{k}. {who}: " + ", ".join(f"x{p['number'][-4:]} ({p['tier'].replace('Dial ', '')}, {p['line_type'].lower()})" for p in ph))
    if not by_who:
        lines.append("(no Dial First/Second numbers found)")
    if missing:
        lines += ["", "Survivors with no phone found: " + "; ".join(missing)]
    if s.get("survivors_verbatim"):
        lines += ["", "Obituary survivors: " + s["survivors_verbatim"]]
    if s.get("notes"):
        lines += ["", "Notes: " + s["notes"]]
    if s.get("obituary_links"):
        lines += ["", "Obituary: " + " | ".join(l for l in s["obituary_links"] if l)]
    lines += ["", f"Heir phones are on this owner tagged '{HEIR_PHONE_TAG}' (decedent's own lines: '{OWNER_LINE_TAG}'). "
                  "No automated texts on this record - text by hand."]
    return "\n".join(lines)


def living_note(s: dict, today: dt.date) -> str:
    who = s.get("decedent_name") or "a family member"
    rel = s.get("relationship_to_owner") or "relative"
    lines = [f"OBITUARY CHECK {today}: THE OWNER IS LIVING ({s.get('confidence') or '?'} confidence).",
             f"The obituary is for {who} ({rel} of the owner{', co-owner on title' if s.get('classification') == 'co_owner_deceased' else ''}), "
             f"died {s.get('date_of_death') or 'date unknown'}.",
             "Call the owner directly. Do not ask for the deceased; if it comes up, offer condolences."]
    if s.get("notes"):
        lines.append("Notes: " + s["notes"])
    if s.get("obituary_links"):
        lines.append("Obituary: " + " | ".join(l for l in s["obituary_links"] if l))
    return "\n".join(lines)


# ----------------------------------------------------------------- run
def skip_row(d: dict) -> dict:
    a, o = d.get("address") or {}, d.get("owner") or {}
    m = o.get("address") if isinstance(o.get("address"), dict) and (o.get("address") or {}).get("street") else a
    return {"First Name": (o.get("first_name") or "").strip(), "Last Name": (o.get("last_name") or "").strip(),
            "Mailing Address": m.get("street"), "Mailing City": m.get("city"), "Mailing State": m.get("state"),
            "Mailing Zip": m.get("postal_code") or m.get("zip5"), "Property Address": a.get("street"),
            "Property City": a.get("city"), "Property State": a.get("state"), "Property Zip": a.get("postal_code")}


def match_record(d: dict, recs: list[dict]) -> dict | None:
    """The SmartSkip result for this property: same street number + first street word, else same owner name."""
    st = _toks((d.get("address") or {}).get("street"))
    num = re.match(r"\d+", (d.get("address") or {}).get("street") or "")
    for r in recs:
        rt = _toks(r.get("property_address"))
        rn = re.match(r"\d+", r.get("property_address") or "")
        if num and rn and num.group() == rn.group() and st[:1] == rt[:1]:
            return r
    o = d.get("owner") or {}
    key = (_toks(o.get("first_name"))[:1], _toks(o.get("last_name"))[-1:])
    return next((r for r in recs if (_toks(r["first"])[:1], _toks(r["last"])[-1:]) == key), None)


def write_record(api: Sift, d: dict, s: dict, people: list[dict], missing: list[str], scores: dict,
                 tag_ids: dict, today: dt.date, log: dict) -> bool:
    u = d["uuid"]
    entry = log[u] = {"address": (d.get("address") or {}).get("street"), "owner": owner_name(d),
                      "classification": s.get("classification"), "screen": s}
    ok = True
    if s["classification"] == "owner_deceased":
        ou = (d.get("owner") or {}).get("uuid")
        before = api.call(f"/api/internal/owner/{ou}/").get("phones") or []
        phones, added = merge_phones(before, people, scores)
        entry.update(before=before, added=added)
        if added:
            after = api.call(f"/api/internal/owner/{ou}/", "PATCH", {"phones": phones})
            got = {digits(p["number"]) for p in (after or {}).get("phones") or []}
            ok = got >= {digits(p["number"]) for p in phones}
            entry["phones"] = "ok" if ok else f"readback mismatch: {str(after)[:200]}"
        note = heir_note(s, added, missing, today)
    elif s["classification"] in ("relative_deceased", "co_owner_deceased"):
        note = living_note(s, today)
    else:
        note = None
    if note:
        api.call(f"/api/internal/property/{u}/message/", "POST", {"message": note})
        entry["note"] = note
    titles = [RESEARCHED_TAG] + ([DP_REVIEW_TAG[1]] if note else [])
    add_tags(api, [u], titles)
    entry["tags"] = titles
    return ok


def run(api: Sift, records: list[dict], args, today: dt.date, log_path, pack_path) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=_env("ANTHROPIC_API_KEY"))
    pack = {"date": str(today), "records": {}, "order": None}
    res = {"screened": 0, "classes": Counter(), "written": 0, "failed": [], "phones_added": 0, "skip_rows": 0,
           "scored": 0, "order": None, "error": None}

    def save():
        pack_path.write_text(json.dumps(pack, indent=1, default=str), encoding="utf-8")

    for d in records[:args.max_records]:
        try:
            s = sanity(screen(client, d), _date(d.get("last_obituary_date")), today)
        except Exception as e:  # one bad search shouldn't stop the batch
            s = {"classification": "error", "notes": f"{type(e).__name__}: {e}"}
        pack["records"][d["uuid"]] = {"detail": d, "screen": s}
        res["screened"] += 1
        res["classes"][s["classification"]] += 1
        save()
    if not args.live:
        return res
    return write_all(api, pack, args, today, log_path, pack_path, res)


def write_all(api: Sift, pack: dict, args, today: dt.date, log_path, pack_path, res: dict) -> dict:
    tag_ids = {t: ensure_tag(api, t) for t in (RESEARCHED_TAG, DP_REVIEW_TAG[1])}
    items = [v for v in pack["records"].values() if v["screen"].get("classification") != "error"]
    heir_cases = [v for v in items if v["screen"]["classification"] == "owner_deceased"][:args.max_skip]
    recs, log = [], {}
    if heir_cases and not pack.get("export"):
        sess = smartskip.Session()
        try:
            if not pack.get("order"):
                order = smartskip.submit(sess, smartskip.build_csv([skip_row(v["detail"]) for v in heir_cases]))
                pack["order"] = order["bulkSkipId"]
                pack_path.write_text(json.dumps(pack, indent=1, default=str), encoding="utf-8")
                smartskip.pay(sess, order["bulkSkipId"])
                pack["paid"] = True
                res["skip_rows"] = order.get("entities") or len(heir_cases)
            pack["export"] = smartskip.wait_and_download(sess, pack["order"])
        except smartskip.SmartSkipError as e:
            res["error"] = f"SmartSkip: {e}"
            if pack.get("paid"):  # never pay twice for the same records
                add_tags(api, [v["detail"]["uuid"] for v in heir_cases], [RESEARCHED_TAG])
            pack_path.write_text(json.dumps(pack, indent=1, default=str), encoding="utf-8")
            return res
        pack_path.write_text(json.dumps(pack, indent=1, default=str), encoding="utf-8")
    res["order"] = pack.get("order")
    if pack.get("export"):
        recs = smartskip.parse(pack["export"])
    plans, numbers = {}, []
    for v in heir_cases:
        rec = match_record(v["detail"], recs) or {}
        people, missing = heirs(v["screen"], rec)
        existing = {digits(p.get("number")) for p in ((v["detail"].get("owner") or {}).get("phones") or [])}
        plans[v["detail"]["uuid"]] = (people, missing)
        numbers += [n for n in numbers_to_score(people, existing) if n not in numbers]
    scores = score_all(numbers[:args.max_score], _env("TRESTLE_API_KEY")) if numbers else {}
    res["scored"] = len(scores)
    try:
        for v in items:
            d, s = v["detail"], v["screen"]
            if s["classification"] == "owner_deceased" and d["uuid"] not in plans:
                continue  # over --max-skip; researched next run
            people, missing = plans.get(d["uuid"], ([], []))
            try:
                if write_record(api, d, s, people, missing, scores, tag_ids, today, log):
                    res["written"] += 1
                else:
                    res["failed"].append(owner_name(d))
            except RuntimeError as e:
                log.setdefault(d["uuid"], {})["error"] = str(e)
                res["failed"].append(f"{owner_name(d)}: {e}")
            res["phones_added"] += len(log.get(d["uuid"], {}).get("added") or [])
    finally:
        log_path.write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
    return res


def report(p: dict, res: dict | None, args) -> str:
    n = min(len(p["records"]), args.max_records)
    lines = [f"Obituary deep prospecting ({'LIVE' if args.live else 'screen only' if args.screen else 'plan only'}): "
             f"{len(p['records'])} eligible, {n} this run"]
    lines.append("Pool: " + ", ".join(f"{k} {v}" for k, v in sorted(p["counts"].items())))
    for d in p["records"][:n]:
        lines.append(f"  - {owner_name(d)}, {(d.get('address') or {}).get('street')} (obit {d.get('last_obituary_date') or '?'})")
    if res is None:
        lines.append(f"Est. cost if live: screen ~${n * SCREEN_COST:.2f} + SmartSkip up to ${min(n, args.max_skip) * smartskip.COST_PER_ROW:.2f}"
                     f" + Trestle up to ${(args.max_score if n else 0) * SCORE_COST:.2f}")
        return "\n".join(lines)
    lines.append("Screened: " + ", ".join(f"{k} {v}" for k, v in res["classes"].items()))
    if args.live:
        lines.append(f"SmartSkip rows {res['skip_rows']} (order {res['order'] or '-'}), phones scored {res['scored']}, "
                     f"heir phones added {res['phones_added']}, records written {res['written']}")
        lines.append("Waiting for review in DataSift: tag 'DP Review'. Approve heir cases by swapping it for 'Deep Prospecting'.")
    if res.get("error"):
        lines.append("STOPPED: " + res["error"])
    if res["failed"]:
        lines.append("Failed: " + "; ".join(res["failed"]))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--screen", action="store_true", help="run the Claude screen (paid); no writes")
    ap.add_argument("--live", action="store_true", help="screen + SmartSkip + Trestle + DataSift writes")
    ap.add_argument("--resume", help="run pack JSON from an earlier run: download its paid order and write")
    ap.add_argument("--county", default="Jackson")
    ap.add_argument("--max-records", type=int, default=MAX_RECORDS)
    ap.add_argument("--max-skip", type=int, default=MAX_SKIP)
    ap.add_argument("--max-score", type=int, default=MAX_SCORE)
    ap.add_argument("--slack", action="store_true")
    args = ap.parse_args()
    api = Sift(_env("REISIFT_API_KEY"))
    today = ct_now().date()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    log_path = BACKUP_DIR / f"obit_dp_write_{today}.json"
    if args.resume:
        args.live = True
        pack = json.loads(open(args.resume, encoding="utf-8").read())
        res = write_all(api, pack, args, today, log_path, STATE_DIR / os.path.basename(args.resume),
                        {"screened": 0, "classes": Counter(), "written": 0, "failed": [], "phones_added": 0,
                         "skip_rows": 0, "scored": 0, "order": None, "error": None})
        text = report({"records": [], "counts": {}}, res, args)
    else:
        p = plan(api, args.county, today)
        (STATE_DIR / f"obit_dp_plan_{today}.json").write_text(json.dumps(
            {"counts": p["counts"], "records": [{"uuid": d["uuid"], "owner": owner_name(d), "street": (d.get("address") or {}).get("street"),
                                                 "obit": d.get("last_obituary_date")} for d in p["records"]]}, indent=1), encoding="utf-8")
        res = None
        if args.screen or args.live:
            missing = [k for k in ("ANTHROPIC_API_KEY",) + (("SMARTSKIP_EMAIL", "SMARTSKIP_PASSWORD", "TRESTLE_API_KEY") if args.live else ())
                       if not _env(k)]
            if missing:
                text = f"Obituary DP: {', '.join(missing)} not set, nothing done"
                print(text)
                if args.slack:
                    _slack(text)
                return 1
            res = run(api, p["records"], args, today, log_path, STATE_DIR / f"obit_dp_pack_{today}.json")
        text = report(p, res, args)
    print(text)
    if args.slack:
        _slack(text)
    return 1 if res and (res["failed"] or res.get("error")) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # the workflow step is continue-on-error, so say it out loud
        if "--slack" in sys.argv:
            _slack(f"Obituary DP FAILED ({type(e).__name__}: {e}); Friday top-up continues without it")
        raise
