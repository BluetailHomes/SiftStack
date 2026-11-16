"""Drip primary swap — keep a P1 morning-text drip going when its primary phone turns out wrong.

Why this exists (Rahaf, 2026-11-12: "texts should move to the next best if the primary is wrong")
--------------------------------------------------------------------------------------------------
A running DataSift drip texts whatever phone is PRIMARY at each send (verified live 2026-11-13: a
mid-drip primary swap sent the next text to the new number). So when the primary is marked Wrong /
Dead (or a wrong-number / not-the-owner reply comes from it), we don't have to stop the drip:

  next best number   = the first Dial First, then Dial Second, MOBILE on the owner that is not
                       Wrong/Dead/DNC and not blocked by the STOP policy (pm_text_sender._best_primary)
  none               -> remove the drip (the old behaviour)
  already texted     -> make it primary and leave the drip running: the remaining days go to it
  never texted       -> remove the drip, make it primary, tag the record "Drip Restart". Monday's
                       am-start re-enrolls it (status cleared, then Prospecting) so the new number
                       gets the full 5 weekday texts. DataSift's drip waits are plain 1-day gaps and
                       can't skip a weekend, so a full 5-text run can only start on a Monday.

"Texted" = an owner.sms.sent to the number or an owner.sms.received from it in the record's logs,
or an entry in the extra-texts ledger (DataSift can lag a few seconds on API sends).

Every swap leaves a message-board note on the record and a Slack line. Nothing changes unless the
repo variable DRIP_SWAP_LIVE is "true"; until then callers keep the old remove-the-drip behaviour
and only log what the swap would have done.
"""

from __future__ import annotations

import json
import logging

import pm_text_sender as pts

LOG = logging.getLogger("drip_swap")

RESTART_TAG = "Drip Restart"
MOVED_ON = "left prospecting"
WRONG_MARKS = {"WRONG", "DEAD", "WRONG_DNC"}


def swap_live() -> bool:
    return pts._env("DRIP_SWAP_LIVE").lower() == "true"


def _titles(tags) -> list[str]:
    return [t.get("title", "") if isinstance(t, dict) else str(t) for t in tags or []]


def owner_phones(det: dict) -> dict[str, dict]:
    """{10-digit number: phone with tags as titles} for the owner's own phones (the primary must be one)."""
    out = {}
    for p in (det.get("owner") or {}).get("phones") or []:
        n = pts._d10(p.get("number"))
        if n:
            out[n] = {**p, "tags": _titles(p.get("tags"))}
    return out


def primary_problem(det: dict) -> str | None:
    """'wrong' when the primary is marked Wrong/Dead, 'cleared' when the owner has no primary (DataSift
    may clear it when the primary is marked Wrong), else None."""
    own = det.get("owner") or {}
    primary = pts._primary_num(own)
    if not primary:
        return "cleared"
    ph = owner_phones(det).get(primary)
    if ph and (ph.get("status") or "").upper() in WRONG_MARKS:
        return "wrong"
    return None


def load_ledger() -> dict:
    try:
        return json.loads(pts.EXTRA_LEDGER.read_text(encoding="utf-8")) if pts.EXTRA_LEDGER.exists() else {}
    except (OSError, ValueError):
        return {}


def texted_numbers(evs: list[dict]) -> set[str]:
    """Numbers we have texted or that have texted us, from a record's logs."""
    out = set()
    for e in evs:
        sms = (e.get("payload") or {}).get("sms") or {}
        if e.get("event_type") == "owner.sms.sent":
            out.add(pts._d10(sms.get("destination_number")))
        elif e.get("event_type") == "owner.sms.received":
            out.add(pts._d10(sms.get("origin_number")))
    out.discard("")
    return out


def never_texted(evs: list[dict], number: str, ledger: dict | None = None) -> bool:
    n = pts._d10(number)
    return n not in texted_numbers(evs) and n not in {pts._d10(k) for k in (ledger or {})}


def swap_target(det: dict, exclude: set[str] | None = None, blocked: set[str] | None = None) -> str | None:
    """The next best primary: Dial First, then Dial Second, MOBILE, not Wrong/Dead/DNC, not excluded."""
    skip = {pts._d10(x) for x in (exclude or set()) | (blocked or set())}
    phones = {n: p for n, p in owner_phones(det).items() if n not in skip}
    return pts._best_primary(phones)


def decide(det: dict, evs: list[dict] | None, stop: dict, wrong: str | None = None, ledger: dict | None = None) -> dict:
    """{action: remove|swap|restart, target, why}. `wrong` = a number known to be wrong that may not be
    marked yet (the responder decides before DataSift shows the mark). Unreadable logs -> plain swap
    (never a restart on a guess)."""
    if stop.get("hold"):
        return {"action": "remove", "target": None, "why": f"STOP policy hold ({stop.get('why')})"}
    exclude = {pts._primary_num(det.get("owner") or {})} | ({wrong} if wrong else set())
    target = swap_target(det, exclude - {""}, set(stop.get("blocked") or ()))
    if not target:
        return {"action": "remove", "target": None, "why": "no other Dial First/Second mobile that isn't Wrong/Dead/DNC"}
    if evs is None:
        return {"action": "swap", "target": target, "why": "record logs unreadable; treated as already texted"}
    if never_texted(evs, target, ledger):
        return {"action": "restart", "target": target, "why": "next number never texted: full 5-day drip restarts Monday"}
    return {"action": "swap", "target": target, "why": "next number already texted: remaining drip days go to it"}


def _ensure_tag(api: pts.Sift, title: str) -> None:
    resp = api.call("/api/internal/tag/?offset=0&limit=10000&ordering=title")
    rows = (resp.get("results") or resp.get("data") or []) if isinstance(resp, dict) else (resp or [])
    if not any(pts._tag_key(t.get("title")) == pts._tag_key(title) for t in rows):
        api.call("/api/internal/tag/", "POST", {"title": title})


def set_primary(api: pts.Sift, det: dict, number: str) -> bool:
    own = det.get("owner") or {}
    api.call(f"/api/internal/owner/{own['uuid']}/", "PATCH", {"primary_phone": pts._d10(number)})
    back = api.call(f"/api/internal/property/{det['uuid']}/") if det.get("uuid") else {}
    return pts._primary_num((back or {}).get("owner") or {}) == pts._d10(number)


def remove_drips(api: pts.Sift, drips: list[dict]) -> list[str]:
    """DELETE each running history row; returns the names that failed."""
    failed = []
    for h in drips:
        try:
            api.call(f"/api/internal/drip-campaign-history/{h['history_uuid']}/", "DELETE")
        except RuntimeError as e:
            LOG.error("drip removal failed %s: %s", h.get("history_uuid"), e)
            failed.append(h.get("name") or h["history_uuid"])
    return failed


def apply(api: pts.Sift, pu: str, det: dict, drips: list[dict], d: dict, old: str, reason: str,
          slack: bool = True, note: bool = True) -> dict:
    """Carry out decide()'s result (live). Returns {ok, line} where line is the note/Slack summary."""
    det = {**det, "uuid": det.get("uuid") or pu}
    street = (det.get("address") or {}).get("street") or pu
    old_s = f"...{old[-4:]}" if old else "(none)"
    new_s = f"...{(d['target'] or '')[-4:]}"
    ok, failed = True, []
    if d["action"] in ("remove", "restart"):
        failed = remove_drips(api, drips)
        ok = not failed
    if d["action"] in ("swap", "restart"):
        try:
            if not set_primary(api, det, d["target"]):
                ok = False
                failed.append("primary did not read back")
        except RuntimeError as e:
            ok = False
            failed.append(f"primary write failed: {str(e)[:80]}")
    if d["action"] == "restart" and ok:
        try:
            _ensure_tag(api, RESTART_TAG)
            api.call(f"/api/internal/property/{pu}/", "PATCH", {"tags": [RESTART_TAG]})
        except RuntimeError as e:
            ok = False
            failed.append(f"tag write failed: {str(e)[:80]}")
    if d["action"] == "remove":
        line = f"Drip: primary {old_s} {reason}; {d['why']}, so the drip was removed."
    elif d["action"] == "swap":
        line = f"Drip: primary {old_s} {reason}; primary moved to {new_s} (already texted), drip keeps running to it."
    else:
        line = (f"Drip: primary {old_s} {reason}; primary moved to {new_s}, which was never texted. Drip removed and "
                f"tagged {RESTART_TAG!r}: Monday's morning start gives {new_s} the full 5-day drip.")
    if failed:
        line += " PROBLEM: " + "; ".join(failed) + ". Check by hand."
    if note:
        try:
            api.call(f"/api/internal/property/{pu}/message/", "POST", {"message": line})
        except RuntimeError as e:
            LOG.warning("note failed for %s: %s", pu, e)
    if slack:
        pts._slack(f"{':warning: ' if failed else ''}*Drip swap* {street}: {line}\nhttps://app.reisift.io/records/properties/{pu}/details")
    LOG.info("%s %s: %s", d["action"], pu, line)
    return {"ok": ok, "line": line}


def handle(api: pts.Sift, pu: str, drips: list[dict], reason: str, wrong: str | None = None,
           det: dict | None = None, slack: bool = True, note: bool = True) -> dict:
    """Read the record, decide, and apply (live). Callers check swap_live() first."""
    det = det or api.call(f"/api/internal/property/{pu}/")
    try:
        evs = (api.call(f"/api/internal/property/{pu}/logs/?limit=250&offset=0") or {}).get("results") or []
    except RuntimeError:
        evs = None
    stop = pts.record_stop(api, pu, det)
    d = decide(det, evs, stop, wrong, load_ledger())
    old = pts._d10(wrong) or pts._primary_num(det.get("owner") or {})
    return {**d, **apply(api, pu, det, drips, d, old, reason, slack, note)}


def preview(api: pts.Sift, pu: str, wrong: str | None = None, det: dict | None = None) -> dict:
    """decide() without writing anything (dry runs and the not-yet-live log line)."""
    det = det or api.call(f"/api/internal/property/{pu}/")
    try:
        evs = (api.call(f"/api/internal/property/{pu}/logs/?limit=250&offset=0") or {}).get("results") or []
    except RuntimeError:
        evs = None
    return decide(det, evs, pts.record_stop(api, pu, det), wrong, load_ledger())


# ------------------------------------------------------------ Monday restart pass (am-start)
def restart_gate(det: dict, stop: dict, drips: list[dict] | None) -> str | None:
    """Why a 'Drip Restart' record must not be re-enrolled today, or None."""
    owner = det.get("owner") or {}
    phones = owner_phones(det)
    lines = [t for t in _titles(det.get("tags")) if t in pts.SMS_LINE_TAGS]
    primary = pts._primary_num(owner)
    if why := pts.enroll_block(det):
        return why
    if det.get("status") not in (None, "", "prospecting"):
        return f"{MOVED_ON}: status is {det.get('status')!r} (someone has taken it from here)"
    if det.get("assigned_to") != pts.CALLER_USER_UUID:
        return "not assigned to the caller"
    if not (owner.get("first_name") or "").strip():
        return "owner has no first name"
    if len(lines) != 1:
        return f"needs exactly one SMS Line tag (has {len(lines)})"
    if not primary or primary not in phones:
        return "no primary phone"
    if phones[primary].get("type") != "MOBILE" or (phones[primary].get("status") or "") in pts.BAD_MARKS:
        return f"primary phone is {phones[primary].get('type')}/{phones[primary].get('status')}"
    if stop["hold"]:
        return f"STOP policy hold ({stop['why']})"
    if primary in stop["blocked"]:
        return "primary phone is DNC"
    if drips is None:
        return "drip history unreadable"
    if any(x.get("status") == "running" for x in drips):
        return "already has a running drip"
    return None


def plan_restarts(api: pts.Sift, left: dict[str, int]) -> tuple[list[dict], list[dict], list[str]]:
    """(ready, held, errors) for every record tagged Drip Restart, within each line's remaining room.
    `left` is decremented for each ready record."""
    ready, held, errors = [], [], []
    tag = next((t for t in (api.call("/api/internal/tag/?offset=0&limit=10000&ordering=title") or {}).get("results") or []
                if pts._tag_key(t.get("title")) == pts._tag_key(RESTART_TAG)), None)
    if not tag:
        return ready, held, errors
    for row in api.search({"any_tags": [tag["uuid"]]}):
        u = row["uuid"]
        try:
            det = api.call(f"/api/internal/property/{u}/")
        except RuntimeError as ex:
            errors.append(f"{u}: {str(ex)[:120]}")
            continue
        owner = det.get("owner") or {}
        lines = [t for t in _titles(det.get("tags")) if t in pts.SMS_LINE_TAGS]
        e = {"uuid": u, "street": (det.get("address") or {}).get("street") or "", "owner_first": (owner.get("first_name") or "").strip(),
             "line": lines[0] if len(lines) == 1 else None, "primary": pts._primary_num(owner), "status": det.get("status"),
             "restart": True}
        try:
            drips = pts.drip_rows(api, u)
        except RuntimeError:
            drips = None
        why = restart_gate(det, pts.record_stop(api, u, det), drips)
        if not why and left.get(e["line"], 0) <= 0:
            why = f"{e['line']} has no room left today after the new batch"
        if why:
            held.append({**e, "why": why})
        else:
            left[e["line"]] -= 1
            ready.append(e)
    return ready, held, errors


def restart(api: pts.Sift, u: str) -> None:
    """Re-fire the line's sequence: clear the status, then set Prospecting (re-enrollment verified 2026-10-19)."""
    api.call(f"/api/internal/property/{u}/status/", "POST", {"status": None})
    api.call(f"/api/internal/property/{u}/status/", "POST", {"status": "prospecting"})


def untag(api: pts.Sift, u: str) -> None:
    # Per-record remove only; never the bulk /property/remove-tags/.
    api.call(f"/api/internal/property/{u}/remove-tags/", "POST", {"tags": [RESTART_TAG]})
