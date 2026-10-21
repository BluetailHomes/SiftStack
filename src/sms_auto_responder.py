"""SMS auto-responder for the Jackson Priority 1 drip (draft mode first).

Watches DataSift's account-wide activity feed for inbound texts, classifies each one
against docs/sms_reply_rules.md with Claude, and either:

  draft mode (default): writes the suggested reply + proposed phone/status changes to the
      record's message board and pings Slack. Nothing is sent to the seller.
  auto mode: (not enabled yet) sends the reply in-thread for the safe categories and applies
      the bookkeeping; hand-raise categories get a holding reply and a human ping.

Idempotent across runs: every handled inbound gets a "[auto-reply <sms-id>]" marker on the
record's message board, so re-polling the same window never double-handles a text.

Usage:
  python src/sms_auto_responder.py --since-minutes 20            # draft mode
  python src/sms_auto_responder.py --since-minutes 1500 --dry-run  # classify only, write nothing
  python src/sms_auto_responder.py --mode auto                     # refuses until SEND is wired

Env: REISIFT_API_KEY (required), ANTHROPIC_API_KEY (required), SLACK_WEBHOOK_URL / GOOGLE_CHAT_WEBHOOK_URL (optional),
     SMS_RESPONDER_MODEL (optional, default claude-sonnet-5-5), OUR_LINES (optional, comma list).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_client  # noqa: E402

log = logging.getLogger("sms_auto_responder")

BASE = "https://apiv2.reisift.io/api/internal"
RULES_PATH = Path(__file__).resolve().parent.parent / "docs" / "sms_reply_rules.md"
OUT_DIR = Path(__file__).resolve().parent.parent / "output" / "auto_responder"

# Lines Bluetail owns. Inbound texts that originate from one of these are our own tests.
DEFAULT_OUR_LINES = {
    "8162545235", "8169553316", "8167889952",            # morning-text lines A/B/C
    "8166084051", "8167100226", "8167100554",            # afternoon-text lines
    "8167889485", "8167595422", "8167100448", "8167100146",  # call lines
    "8169553574", "8167889386", "4054941645",
}

CATEGORIES_SAFE = {
    "who_is_this", "pushback", "not_owner", "wrong_number", "text_only",
    "owner_not_selling", "tenant", "rental_owner", "has_realtor", "stop", "scam_question",
}
CATEGORIES_HUMAN = {"call_me", "price_question", "interest", "probate_death", "already_sold", "unclear"}
ALL_CATEGORIES = CATEGORIES_SAFE | CATEGORIES_HUMAN

# Drip stop (stop_drip_if_needed): any reply from the dripped primary phone, or a STOP from any
# number, pulls the record from its running drip. Applied even in draft mode: removing a record
# from a drip is bookkeeping, not a reply. Added 2026-10-21 for stop-type categories after a
# non-owner said "quit texting me" and stayed queued; widened the same day to every category,
# because a seller who has replied should hear from Rahaf, not from tomorrow's automated text.

STATUS_MAP = {  # rulebook wording -> DataSift status key
    "Not Interested": "not_interested",
    "New Lead": "new_lead",
    "Exhausted": "Exhausted",
    "Prospecting": "prospecting",
}


class DataSift:
    def __init__(self, key: str):
        self.h = {"authorization": f"Api-Key {key}", "content-type": "application/json"}
        self.s = {**self.h, "x-http-method-override": "GET"}

    def _req(self, method: str, url: str, **kw) -> requests.Response:
        for i in range(8):
            r = requests.request(method, url, headers=kw.pop("headers", self.h), timeout=40, **kw)
            if r.status_code == 429 or "throttled" in r.text[:120]:
                time.sleep(3 * (i + 1))
                continue
            return r
        return r

    def get(self, path: str, **params):
        r = self._req("GET", BASE + path, params=params)
        return r.json() if r.status_code == 200 else {}

    def search(self, query: dict, limit: int = 20) -> list[dict]:
        r = self._req("POST", BASE + "/property/", headers=self.s,
                      json={"limit": limit, "offset": 0, "query": query})
        return r.json().get("results", []) if r.status_code == 200 else []

    def property(self, uuid: str) -> dict:
        return self.get(f"/property/{uuid}/")

    def logs(self, uuid: str) -> list[dict]:
        return self.get(f"/property/{uuid}/logs/").get("data", [])

    def messages(self, uuid: str) -> list[dict]:
        return self.get(f"/property/{uuid}/message/").get("results", [])

    def post_message(self, uuid: str, text: str) -> bool:
        return self._req("POST", f"{BASE}/property/{uuid}/message/", json={"message": text}).status_code in (200, 201)

    def running_drips(self, uuid: str) -> list[dict]:
        d = self.get("/drip-campaign-history/by-property/", property_uuid=uuid)
        return [h for h in (d.get("results") or []) if h.get("status") == "running"]

    def remove_drip(self, history_uuid: str) -> bool:
        """Same DELETE the UI's "Remove from campaign" sends (verified live 2026-10-16)."""
        return self._req("DELETE", f"{BASE}/drip-campaign-history/{history_uuid}/").status_code in (200, 204)

    def feed(self, since: dt.datetime, max_pages: int = 30) -> list[dict]:
        """Account-wide log feed, newest first, cursor-paginated. Returns inbound SMS events since `since`."""
        out, cursor = [], None
        for _ in range(max_pages):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = self.get("/property/logs/", **params)
            res = page.get("results", [])
            if not res:
                break
            for e in res:
                ts = dt.datetime.strptime(e["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
                if ts < since:
                    return out
                if e.get("event_type") == "owner.sms.received":
                    out.append(e)
            cursor = page.get("next_cursor")
            if not cursor:
                break
        return out


def load_rules() -> str:
    return RULES_PATH.read_text(encoding="utf-8")


def digits(n: str | None) -> str:
    return re.sub(r"\D", "", n or "")[-10:]


def pick_property(ds: DataSift, owner_uuid: str, texted_address_hint: str | None) -> dict | None:
    props = ds.search({"must": {"owners": [owner_uuid]}}, limit=20)
    if not props:
        return None
    def rank(p):
        tags = p.get("tags") or []
        st = p.get("status"); st = st.get("title") if isinstance(st, dict) else st
        street = (p.get("address") or {}).get("street") or ""
        hint = 2 if texted_address_hint and street.lower() in texted_address_hint.lower() else 0
        return (hint, 1 if st == "prospecting" else 0, 1 if any(str(t).startswith("Batch") for t in tags) else 0)
    return sorted(props, key=rank, reverse=True)[0]


def build_thread(logs: list[dict], inbound_number: str) -> list[dict]:
    """Chronological SMS thread with the inbound number (last 12 messages)."""
    thread = []
    for e in reversed(logs):
        if e.get("event_type") not in ("owner.sms.sent", "owner.sms.received"):
            continue
        sms = (e.get("payload") or {}).get("sms") or {}
        other = sms.get("destination_number") if sms.get("direction") == "outbound" else sms.get("origin_number")
        if digits(other) != inbound_number:
            continue
        thread.append({"when": e["timestamp"], "from": "us" if sms.get("direction") == "outbound" else "owner",
                       "text": sms.get("message") or ""})
    return thread[-12:]


def already_handled(ds: DataSift, prop_uuid: str, sms_uuid: str) -> tuple[bool, bool]:
    """(this sms handled, any auto-reply already today)"""
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    msgs = ds.messages(prop_uuid)
    this = any(f"[auto-reply {sms_uuid[:8]}]" in (m.get("message") or "") for m in msgs)
    today_any = any("[auto-reply " in (m.get("message") or "") and (m.get("created") or "")[:10] == today for m in msgs)
    return this, today_any


SYSTEM = """You classify an inbound text from a property owner for a small Kansas City real-estate team and
draft the reply, following the rulebook exactly. The seller-facing name is Britney. Output JSON only:
{"category": <one of the rulebook ids>, "reply": <the rulebook reply with [address] and [first name] filled in,
 or "" if the rulebook says none>, "phone_status": "leave"|"CORRECT"|"WRONG"|"DNC",
 "property_status": "leave"|"Not Interested"|"New Lead"|"Exhausted", "note": <short note for the record or "">,
 "needs_human": true|false, "confidence": 0.0-1.0, "reason": <one sentence>}
Rules you must not break: never include a dollar amount in a reply; never mark Not Interested unless the owner
speaks in first person from the number we texted; a question is never Not Interested; if the message is from
someone who is not the owner, category is not_owner; if unsure, category unclear with needs_human true and an
empty reply. Keep the reply under 300 characters and in the rulebook's words."""


def _ask(prompt: str, model: str) -> dict | None:
    """One Claude call with a tolerant JSON parse (allows newlines inside strings)."""
    import anthropic
    tool = {
        "name": "classify_reply",
        "description": "Record the classification and drafted reply for one inbound text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": sorted(ALL_CATEGORIES)},
                "reply": {"type": "string"},
                "phone_status": {"type": "string", "enum": ["leave", "CORRECT", "WRONG", "DNC"]},
                "property_status": {"type": "string", "enum": ["leave", "Not Interested", "New Lead", "Exhausted"]},
                "note": {"type": "string"},
                "needs_human": {"type": "boolean"},
                "confidence": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["category", "reply", "phone_status", "property_status", "note", "needs_human", "confidence", "reason"],
        },
    }
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        resp = client.messages.create(model=model, max_tokens=700,
                                      system=SYSTEM + "\nAlways answer by calling the classify_reply tool.",
                                      tools=[tool], messages=[{"role": "user", "content": prompt}])
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use":
                return dict(block.input)
        text = llm_client._extract_response_text(resp.content).strip()
    except Exception as e:  # noqa: BLE001
        log.warning("Anthropic call failed: %s", e)
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1]):
        try:
            return json.loads(candidate, strict=False)
        except (json.JSONDecodeError, ValueError):
            continue
    log.warning("unparseable classifier output: %.300s", text)
    return None


def classify(rules: str, ctx: dict, model: str) -> dict | None:
    prompt = (
        "RULEBOOK:\n" + rules + "\n\nRECORD:\n" + json.dumps(ctx["record"], indent=1)
        + "\n\nTHREAD (oldest first):\n" + "\n".join(f"[{m['when']}] {m['from']}: {m['text']}" for m in ctx["thread"])
        + f"\n\nINBOUND TO CLASSIFY (from {ctx['inbound_number']}): {ctx['inbound_text']!r}\n"
    )
    out = _ask(prompt, model)
    if not out or out.get("category") not in ALL_CATEGORIES:
        log.warning("classifier returned unusable output; retrying once")
        out = _ask(prompt + "\nReturn ONLY the JSON object, no prose, no line breaks inside strings.", model)
    if not out or out.get("category") not in ALL_CATEGORIES:
        return None
    if re.search(r"\$\s?\d", out.get("reply") or ""):
        out["reply"], out["needs_human"], out["category"] = "", True, "unclear"
    if out["category"] in CATEGORIES_HUMAN:
        out["needs_human"] = True
    return out


def slack(text: str, needs_human: bool = False) -> None:
    """Post to the chat webhooks. Slack gets every draft; Google Chat (Rahaf's space) gets only the
    drafts flagged needs-a-human, unless GOOGLE_CHAT_ALL=true."""
    for name, env in (("Slack", "SLACK_WEBHOOK_URL"), ("Google Chat", "GOOGLE_CHAT_WEBHOOK_URL")):
        url = os.environ.get(env)
        if not url:
            continue
        if env == "GOOGLE_CHAT_WEBHOOK_URL" and not needs_human and os.environ.get("GOOGLE_CHAT_ALL", "").lower() != "true":
            continue
        try:
            requests.post(url, json={"text": text}, timeout=15)
        except Exception as e:  # noqa: BLE001
            log.warning("%s post failed: %s", name, e)


def handle(ds: DataSift, rules: str, ev: dict, mode: str, dry_run: bool, model: str, our_lines: set[str]) -> dict:
    sms = (ev.get("payload") or {}).get("sms") or {}
    inbound_number = digits(sms.get("origin_number"))
    result = {"ts": ev["timestamp"], "from": inbound_number, "text": sms.get("message"), "action": "skip"}
    if inbound_number in our_lines:
        result["why"] = "from our own line"
        return result
    owner_uuid = ev.get("resource") or sms.get("owner")
    prop = pick_property(ds, owner_uuid, None)
    if not prop:
        result["why"] = "no property for owner"
        return result
    pu = prop["uuid"]
    d = ds.property(pu)
    st = d.get("status"); st = st.get("title") if isinstance(st, dict) else st
    logs = ds.logs(pu)
    thread = build_thread(logs, inbound_number)
    hint = next((m["text"] for m in reversed(thread) if m["from"] == "us"), "")
    if len(ds.search({"must": {"owners": [owner_uuid]}}, limit=5)) > 1 and hint:
        prop = pick_property(ds, owner_uuid, hint) or prop
        if prop["uuid"] != pu:
            pu = prop["uuid"]; d = ds.property(pu); logs = ds.logs(pu); thread = build_thread(logs, inbound_number)
            st = d.get("status"); st = st.get("title") if isinstance(st, dict) else st
    own = d.get("owner") or {}
    phones = {digits(p["number"]): p.get("status") for p in own.get("phones", [])}
    ctx = {
        "record": {
            "address": d['address'].get('street'), "city": d['address'].get('city'),
            "owner_first_name": own.get("first_name"), "owner_last_name": own.get("last_name"),
            "property_status": st, "inbound_number_status": phones.get(inbound_number),
            "inbound_is_primary": digits(own.get("primary_phone")) == inbound_number,
            "tags": [t for t in d.get("tags", []) if str(t).startswith(("Batch", "SMS Line", "Priority"))],
        },
        "thread": thread, "inbound_number": inbound_number, "inbound_text": sms.get("message") or "",
    }
    result.update(property=pu, street=d["address"].get("street"), status=st)
    handled, today_any = (False, False) if dry_run else already_handled(ds, pu, sms.get("uuid", ""))
    if handled:
        result["why"] = "already handled"
        return result
    out = classify(rules, ctx, model)
    if not out:
        result["why"] = "classifier failed"
        return result
    if today_any:
        out["needs_human"] = True
        out["reason"] = "second inbound today; " + out.get("reason", "")
    result.update(classification=out, action="draft" if mode == "draft" else "auto")
    if dry_run:
        return result
    marker = f"[auto-reply {sms.get('uuid', '')[:8]}]"
    note = (f"{marker} DRAFT — {out['category']} (confidence {out.get('confidence', 0):.2f})"
            f"{' — NEEDS A HUMAN' if out.get('needs_human') else ''}\n"
            f"Owner texted from {inbound_number}: {ctx['inbound_text']!r}\n"
            f"Suggested reply: {out.get('reply') or '(none)'}\n"
            f"Proposed: phone {inbound_number[-4:]} -> {out.get('phone_status')}; status -> {out.get('property_status')}"
            f"{'; note: ' + out['note'] if out.get('note') else ''}\nWhy: {out.get('reason', '')}")
    if mode == "auto":
        raise NotImplementedError("auto mode needs the send-SMS call wired; run in draft mode")
    drip_line = stop_drip_if_needed(ds, pu, out, ctx["record"]["inbound_is_primary"])
    if drip_line:
        note += "\n" + drip_line
        result["drip"] = drip_line
    ds.post_message(pu, note)
    slack(f"*Auto-reply draft* {d['address'].get('street')} — {out['category']}"
          f"{' — needs a human' if out.get('needs_human') else ''}\n"
          f"> {ctx['inbound_text']}\nSuggested: {out.get('reply') or '(none)'}\n"
          f"{drip_line + chr(10) if drip_line else ''}"
          f"https://app.reisift.io/records/properties/{pu}/details", needs_human=bool(out.get("needs_human")))
    return result


def stop_drip_if_needed(ds: DataSift, prop_uuid: str, out: dict, inbound_is_primary: bool) -> str:
    """Remove the record from any running drip when the inbound comes from the number we drip.
    The drip texts the primary phone, so ANY reply from it (whatever the category: a question, an
    interested owner, a not-the-owner) ends the automated morning texts; a human takes it from
    there. A non-primary inbound only counts for a STOP (a legal must-stop pulls every automation).
    Returns a one-line summary, or "" if untouched."""
    cat = out.get("category")
    wants_stop = cat == "stop" or inbound_is_primary
    if not wants_stop:
        return ""
    try:
        drips = ds.running_drips(prop_uuid)
    except Exception as e:  # noqa: BLE001
        log.warning("drip history read failed for %s: %s", prop_uuid, e)
        return "Drip: could not read drip history, check by hand"
    if not drips:
        return ""
    names = []
    for h in drips:
        ok = ds.remove_drip(h["history_uuid"])
        names.append(f"{h.get('name')}{'' if ok else ' (REMOVE FAILED)'}")
        log.info("%s drip %s for %s (%s)", "removed" if ok else "FAILED to remove", h.get("name"), prop_uuid, cat)
    return f"Drip: removed from {', '.join(names)} ({cat})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-minutes", type=int, default=20)
    ap.add_argument("--mode", choices=["draft", "auto"], default="draft")
    ap.add_argument("--dry-run", action="store_true", help="classify only; write nothing")
    ap.add_argument("--model", default=os.environ.get("SMS_RESPONDER_MODEL", "claude-sonnet-5-5"))
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO, format="%(levelname)s %(message)s")
    key = os.environ.get("REISIFT_API_KEY")
    if not key:
        log.error("REISIFT_API_KEY not set"); return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY not set"); return 2
    our_lines = set(DEFAULT_OUR_LINES) | {digits(x) for x in os.environ.get("OUR_LINES", "").split(",") if x.strip()}
    ds = DataSift(key); rules = load_rules()
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=a.since_minutes)
    events = ds.feed(since)
    log.info("inbound texts since %s: %d", since.strftime("%H:%M UTC"), len(events))
    results = []
    for ev in sorted(events, key=lambda e: e["timestamp"]):
        try:
            r = handle(ds, rules, ev, a.mode, a.dry_run, a.model, our_lines)
        except NotImplementedError as e:
            log.error(str(e)); return 3
        except Exception as e:  # noqa: BLE001
            log.exception("failed on event %s: %s", ev.get("id"), e)
            r = {"ts": ev["timestamp"], "action": "error", "why": str(e)[:200]}
        results.append(r)
        c = r.get("classification") or {}
        log.info("%s | %s | %s | %s%s | %s", r.get("ts"), r.get("street"), (r.get("text") or "")[:60],
                 c.get("category", r.get("why")), " [HUMAN]" if c.get("needs_human") else "", (c.get("reply") or "")[:80])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    (OUT_DIR / f"run_{stamp}.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
