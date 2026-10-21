"""Drip exit guard — stop DataSift drip-campaign texts the moment a record leaves prospecting.

Why this exists
---------------
DataSift drip campaigns keep running until every step is done or someone
removes the record from the campaign by hand. There is no sequence action
for "remove from drip campaign" (confirmed live 2026-10-16: the sequence
builder's action palette has Run Drip Campaign only), and a status change
does NOT stop a running drip (tested the same day: a record moved from
`prospecting` to `not_interested` stayed `running` with the next SMS queued).

For the Jackson Priority 1 niche flow that is a real problem: the morning
text goes out automatically for five days once Rahaf sets a record to
`prospecting`. If the owner answers and becomes a New Lead or a Not
Interested, the drip would keep texting them. This guard closes that gap.

What it does
------------
1. Search records that are (a) in one of the drip-triggering tags and
   (b) in any status other than `prospecting` / no status, plus (c) records
   flagged DNC/opt-out and (d) prospecting records whose PRIMARY phone is
   marked Wrong/Dead (the drip texts the primary phone; a not-the-owner
   reply leaves status at prospecting, so (a)/(b) never catch it).
2. For each, read its drip history (`/drip-campaign-history/by-property/`).
3. DELETE every history row that is still `running`. Deleting the row is
   what the UI's "Remove from campaign" does; verified live 2026-10-16.

Everything is idempotent and read-before-write; a record with no running
drip costs one GET. It runs as the second step of the SMS auto-responder
job (.github/workflows/sms-auto-responder.yml), which the Cloudflare Worker in
dispatcher/ starts every 5 minutes, 7:00 AM-10:55 PM Central. Its own GitHub
cron (drip-exit-guard.yml, every 15 minutes) stays as a fallback; GitHub often
runs that hours late, and running twice is harmless.

Usage
-----
    python src/drip_exit_guard.py            # live
    python src/drip_exit_guard.py --dry-run  # report only

Env: REISIFT_API_KEY (required). Optional: DRIP_GUARD_TAGS (comma list of tag
titles whose records are guarded; default "Priority 1").
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import requests

API = "https://apiv2.reisift.io"
LOG = logging.getLogger("drip_exit_guard")

# Any status that means "a human is now handling this, or has closed it".
# Records with NO status or with `prospecting` are the only ones allowed to
# keep receiving automated texts.
ALLOWED_STATUSES = {None, "", "prospecting"}


def _headers(key: str, override_get: bool = False) -> dict:
    h = {
        "authorization": f"Api-Key {key}",
        "accept": "application/json",
        "content-type": "application/json",
    }
    if override_get:
        # A bare POST to /property/ CREATES a record; the override turns it
        # into a search (same trick the DataSift web app uses).
        h["x-http-method-override"] = "GET"
    return h


def _get(key: str, path: str, retries: int = 6) -> dict | list:
    # 429s got more common once the guard started running every 5 minutes next to
    # the responder (2026-10-21): back off longer and honor Retry-After when sent.
    for attempt in range(retries):
        r = requests.get(API + path, headers=_headers(key), timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("Retry-After", ""))
            except ValueError:
                wait = 5 * (attempt + 1)
            time.sleep(min(max(wait, 1), 60))
            continue
        r.raise_for_status()
    raise RuntimeError(f"GET {path} kept failing")


def tag_uuids(key: str, titles: list[str]) -> list[str]:
    rows = _get(key, "/api/internal/tag/?offset=0&limit=10000&ordering=title")
    rows = rows.get("results", rows)
    by_title = {t["title"]: t["uuid"] for t in rows}
    missing = [t for t in titles if t not in by_title]
    if missing:
        LOG.warning("Tags not found in account, ignoring: %s", missing)
    return [by_title[t] for t in titles if t in by_title]


def search_records(key: str, must: dict, limit: int = 200) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        body = {"limit": limit, "offset": offset, "query": {"must": must}}
        for attempt in range(4):
            r = requests.post(
                API + "/api/internal/property/",
                headers=_headers(key, override_get=True),
                json=body,
                timeout=60,
            )
            if r.status_code == 200:
                break
            time.sleep(3 * (attempt + 1))
        r.raise_for_status()
        d = r.json()
        rows = d.get("data") or d.get("results") or []
        out += rows
        if not rows or len(out) >= int(d.get("count") or 0):
            return out
        offset += limit


WRONG_PHONE_STATUSES = {"WRONG", "DEAD", "WRONG_DNC"}


def _digits(n: str | None) -> str:
    return "".join(ch for ch in (n or "") if ch.isdigit())[-10:]


def primary_phone_is_wrong(key: str, property_uuid: str) -> bool:
    d = _get(key, f"/api/internal/property/{property_uuid}/")
    own = d.get("owner") or {}
    primary = _digits(own.get("primary_phone"))
    if not primary:
        return False
    for p in own.get("phones") or []:
        if _digits(p.get("number")) == primary:
            return (p.get("status") or "") in WRONG_PHONE_STATUSES
    return False


def running_drips(key: str, property_uuid: str) -> list[dict]:
    d = _get(key, f"/api/internal/drip-campaign-history/by-property/?property_uuid={property_uuid}")
    return [h for h in (d.get("results") or []) if h.get("status") == "running"]


def remove_history(key: str, history_uuid: str) -> bool:
    r = requests.delete(
        API + f"/api/internal/drip-campaign-history/{history_uuid}/",
        headers=_headers(key),
        timeout=30,
    )
    return r.status_code in (200, 204)


def run(key: str, tags: list[str], dry_run: bool) -> dict:
    stats = {"candidates": 0, "with_running_drip": 0, "removed": 0, "failed": 0, "unread": 0}
    tuuids = tag_uuids(key, tags)
    if not tuuids:
        LOG.error("No guard tags resolved; nothing to do.")
        return stats

    # Records in the guarded tags whose status is anything but prospecting/none.
    # DataSift's search has no "status is not null" filter, so ask for every
    # active status explicitly via must_not on the allowed ones, then filter
    # client-side on the row's own status field as a belt-and-suspenders.
    must = {"any_tags": tuuids, "must_not": {"any_property_status": ["prospecting"]}}
    rows = search_records(key, must)
    rows = [r for r in rows if (r.get("status") or None) not in ALLOWED_STATUSES]
    LOG.info("%d guarded records in a non-prospecting status", len(rows))

    # STOP / opt-out safety net, regardless of status: an owner flagged DNC or
    # opt-out, or any phone on the record carrying a DNC status (a STOP reply
    # synced from smrtPhone, or a litigator number marked DNC by hand), must
    # never keep receiving automated texts.
    seen = {r["uuid"] for r in rows}
    for extra in (
        {"any_tags": tuuids, "dnc": 1},
        {"any_tags": tuuids, "opt_out": 1},
        {"any_tags": tuuids, "any_phone_status": ["DNC", "CORRECT_DNC", "WRONG_DNC"]},
    ):
        try:
            for r in search_records(key, extra):
                if r["uuid"] not in seen:
                    seen.add(r["uuid"])
                    r["_reason"] = "dnc/opt-out"
                    rows.append(r)
        except Exception as e:  # noqa: BLE001
            LOG.warning("opt-out search failed for %s: %s", extra, e)
    # Primary phone marked WRONG/DEAD while the record is still prospecting: the drip texts the
    # primary phone, so it would keep texting a number a human (or the auto-responder's draft)
    # already said is not the owner. Search rows do not carry phone statuses reliably, so each
    # candidate's full record is read over GET; only prospecting records are checked, so this is
    # the active batch, not the whole tag. Added 2026-10-21.
    try:
        wrong = search_records(key, {"any_tags": tuuids, "any_property_status": ["prospecting"],
                                     "any_phone_status": sorted(WRONG_PHONE_STATUSES)})
    except Exception as e:  # noqa: BLE001
        LOG.warning("wrong-phone search failed: %s", e)
        wrong = []
    for r in wrong:
        if r["uuid"] in seen:
            continue
        try:
            if primary_phone_is_wrong(key, r["uuid"]):
                seen.add(r["uuid"])
                r["_reason"] = "primary phone wrong/dead"
                rows.append(r)
        except Exception as e:  # noqa: BLE001
            LOG.warning("primary-phone check failed for %s: %s", r["uuid"], e)
        time.sleep(0.2)
    stats["candidates"] = len(rows)
    LOG.info("%d candidates after adding DNC/opt-out and wrong-primary-phone records", len(rows))

    for r in rows:
        pu = r["uuid"]
        try:
            drips = running_drips(key, pu)
        except Exception as e:  # noqa: BLE001
            # Not a failure: nothing was changed, and the next run (5 minutes later)
            # checks this record again. Only a failed removal fails the job.
            LOG.warning("history read failed for %s (retried next run): %s", pu, e)
            stats["unread"] += 1
            continue
        if not drips:
            continue
        stats["with_running_drip"] += 1
        for h in drips:
            label = f"{r.get('_reason') or r.get('status')} / {h.get('name')} / next={h.get('next_step')} in {h.get('next_in')}"
            if dry_run:
                LOG.info("DRY RUN would remove %s from drip (%s)", pu, label)
                continue
            if remove_history(key, h["history_uuid"]):
                stats["removed"] += 1
                LOG.info("Removed %s from drip (%s)", pu, label)
            else:
                stats["failed"] += 1
                LOG.error("Failed to remove %s (%s)", pu, label)
        time.sleep(0.3)
    return stats


def mark_phone_wrong(key: str, property_uuid: str, number: str) -> bool:
    """Set one phone on the record's owner to WRONG via the UI's own upsert call
    (PATCH /owner/ ignores status; see memory note datasift-owner-phone-write-api)."""
    d = _get(key, f"/api/internal/property/{property_uuid}/")
    own = d.get("owner") or {}
    ph = next((p for p in own.get("phones") or [] if _digits(p.get("number")) == _digits(number)), None)
    if not ph:
        LOG.error("phone %s not on record %s", number, property_uuid)
        return False
    tags = [t.get("title", t) if isinstance(t, dict) else t for t in ph.get("tags") or []]
    body = {"phones": [{"number": ph["number"], "type": ph.get("type") or "UNKNOWN", "tags": tags, "status": "WRONG",
                        "is_connected": ph.get("is_connected", True), "verified": ph.get("verified", False)}]}
    r = requests.post(API + f"/api/internal/owner/{own['uuid']}/upsert-phones/", headers=_headers(key), json=body, timeout=30)
    LOG.info("upsert-phones %s -> WRONG: HTTP %s", number, r.status_code)
    return r.status_code in (200, 201)


def run_one(key: str, property_uuid: str, mark_wrong: str | None, note: str | None, dry_run: bool) -> dict:
    """Pull one record from every running drip, optionally marking a phone WRONG and leaving a note."""
    stats = {"candidates": 1, "with_running_drip": 0, "removed": 0, "failed": 0}
    if mark_wrong and not dry_run and not mark_phone_wrong(key, property_uuid, mark_wrong):
        stats["failed"] += 1
    drips = running_drips(key, property_uuid)
    stats["with_running_drip"] = 1 if drips else 0
    for h in drips:
        label = f"{h.get('name')} / next={h.get('next_step')} in {h.get('next_in')}"
        if dry_run:
            LOG.info("DRY RUN would remove %s from drip (%s)", property_uuid, label)
        elif remove_history(key, h["history_uuid"]):
            stats["removed"] += 1
            LOG.info("Removed %s from drip (%s)", property_uuid, label)
        else:
            stats["failed"] += 1
            LOG.error("Failed to remove %s (%s)", property_uuid, label)
    if note and not dry_run:
        r = requests.post(API + f"/api/internal/property/{property_uuid}/message/", headers=_headers(key),
                          json={"message": note}, timeout=30)
        LOG.info("note posted: HTTP %s", r.status_code)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="report only, delete nothing")
    ap.add_argument("--property", help="one record uuid: remove it from every running drip, ignoring status")
    ap.add_argument("--mark-wrong", help="with --property: also set this phone number to WRONG")
    ap.add_argument("--note", help="with --property: message-board note to leave on the record")
    ap.add_argument("--tags", default=os.environ.get("DRIP_GUARD_TAGS", "Priority 1"),
                    help="comma-separated tag titles to guard (default: Priority 1)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    key = os.environ.get("REISIFT_API_KEY", "")
    if not key:
        LOG.error("REISIFT_API_KEY is not set")
        return 2
    if a.property:
        stats = run_one(key, a.property, a.mark_wrong, a.note, a.dry_run)
        LOG.info("Done: %s", stats)
        return 1 if stats["failed"] else 0
    tags = [t.strip() for t in a.tags.split(",") if t.strip()]
    stats = run(key, tags, a.dry_run)
    LOG.info("Done: %s", stats)
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
