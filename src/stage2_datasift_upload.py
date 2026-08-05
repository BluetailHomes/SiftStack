"""Stage 2 of the two-stage daily pipeline: upload to DataSift.

Why this exists
----------------
The Apify Actor (tn-public-notice-scraper) deliberately does NOT run the
Playwright-based DataSift upload itself — see upstream commit e5e2d509
("Replace automated DataSift upload with manual CSV download links"):
headless Playwright inside Apify's Actor container was found unreliable
(SPA wizard timing issues), so the Actor path only generates DataSift-
formatted CSVs and saves them to Apify's Key-Value Store (KVS) for manual
download + upload.

That's fine for occasional runs a human can babysit, but it defeats the
purpose of a scheduled early-morning run meant to beat Rahaf's 4am start —
nobody is awake at 2-3am to do the manual upload.

This script closes the loop without touching the Apify container at all:
it runs in a *different* unattended environment (GitHub Actions, scheduled
after the Apify run(s)), fetches the already-DataSift-formatted CSVs
straight out of the Apify KVS, and calls the exact same upload_to_datasift/
upload_datasift_split Playwright automation used successfully by the local
CLI path (`--upload-datasift`). No re-enrichment happens here — the CSVs
pulled from KVS are already fully enriched (Smarty, Zillow, tax, obituary/
DM, Tracerfy, Trestle all ran inside the Actor); this script's only job is
the upload that the Actor intentionally skipped.

Multi-platform support (added 2026-08-05)
-------------------------------------------
MO, NM, and KS each run as their own scheduled Apify TASK against the same
underlying Actor (tn-public-notice-scraper) — one platform per Task, per
main.py's separate-run model. The original version of this script found
"the last SUCCEEDED run of the ACTOR", which was fine with exactly one
scheduled thing hitting that Actor, but silently wrong the moment a second
Task started running on a schedule too: it would grab whichever of N
platforms' runs happened to finish last, and upload THAT one under
whatever list-name math ran for it — not necessarily the platform this
script's caller actually wanted, and never all of them.

Fixed by scoping every lookup to a TASK ID instead of the Actor ID —
`GET /v2/actor-tasks/{taskId}/runs/last` instead of `GET /v2/acts/{actorId}/
runs/last` — via the new APIFY_TASKS env var (see below), and looping over
every configured platform independently: one platform's failure (e.g. a
brand-new NM Task with no runs yet) is caught and notified without blocking
the others. List names now include the platform label
("SiftStack 2026-08-05 NM - DMs") so same-day lists from different
platforms never collide in DataSift.

Backward compatible: if APIFY_TASKS isn't set, falls back to the original
single-actor behavior (APIFY_ACTOR_ID, defaulting to MO's Actor ID) so an
existing deployment's secrets keep working with zero changes required.

Rollout note: adding a platform to APIFY_TASKS turns ON its auto-upload for
that platform. Per the 2026-08-04 decision to verify each new market's
scheduled Task runs clean, unattended, at least once before trusting
auto-upload, NM and KS should stay OUT of APIFY_TASKS until that's
confirmed — see CLAUDE.md's Kansas/New Mexico sections for status.

Notification policy
--------------------
Every exit path posts to Slack (when SLACK_WEBHOOK_URL is set) — not just
the success path, and now per-platform. The whole point of this script is
to catch the case where a scheduled Apify run ran long, failed, or produced
nothing; if THAT case is also the one case that stays silent, the safety
net has a hole in exactly the spot it exists to cover. So: missing secrets,
unreachable Apify API, no KVS store, a stale/not-yet-finished run, zero
CSVs, a download failure, and an unexpected crash all notify, in addition
to the upload result itself — independently for each configured platform.

Required environment variables
-------------------------------
APIFY_TOKEN         Apify API token (same account as the scheduled Actor/Tasks)
DATASIFT_EMAIL      DataSift login email
DATASIFT_PASSWORD   DataSift login password
APIFY_TASKS         Optional. "LABEL:taskId,LABEL:taskId,..." — one entry per
                     platform to auto-upload, e.g. "MO:aBc123,NM:dEf456".
                     Find each Task's ID in the Apify Console (Tasks tab ->
                     open the Task -> ID is in the URL / Settings). Omit a
                     platform to leave it on manual/Slack-links-only upload.
APIFY_ACTOR_ID      Optional. Legacy single-platform fallback used only when
                     APIFY_TASKS is unset. Defaults to DLE4KmqvBSxrxGUab
                     (neat_honeyeater/tn-public-notice-scraper).
SLACK_WEBHOOK_URL   Optional. Posts a status message on every exit path.

Usage
-----
    cd src
    python stage2_datasift_upload.py

Intended to run on a schedule (e.g. GitHub Actions) — NOT inside the Apify
Actor. See .github/workflows/stage2-datasift-upload.yml.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("stage2_datasift_upload")

API_BASE = "https://api.apify.com/v2"
DEFAULT_ACTOR_ID = "DLE4KmqvBSxrxGUab"  # neat_honeyeater/tn-public-notice-scraper — legacy MO-only fallback

# Maps the KVS key names written by main.py's Actor path (see
# "DataSift CSVs -> KVS (manual upload)" section) back to the label used
# by write_datasift_split_csvs(). Falls back to a title-cased guess for any
# key that doesn't match, so a future third CSV type doesn't silently drop.
LABEL_MAP = {
    "datasift_dms.csv": "DMs",
    "datasift_heirs.csv": "Heirs",
}

# If the most recent SUCCEEDED run is older than this, refuse to upload —
# it's almost certainly a stale/previous day's run (e.g. tonight's scrape
# failed), not tonight's data. Re-uploading a stale run would silently
# duplicate leads in DataSift under a wrong-dated list name.
MAX_RUN_AGE_HOURS = 6


def _notify(message: str, *, ok: bool = True) -> None:
    """Best-effort Slack post — called on EVERY exit path, not just success.

    A safety net that only fires when things go right isn't a safety net.
    Silently does nothing if SLACK_WEBHOOK_URL isn't set; never raises.
    """
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook:
        return
    try:
        from slack_notifier import _send_webhook

        _send_webhook(message, webhook_url=webhook)
    except Exception as e:  # pragma: no cover - best-effort notification
        log.warning("Slack notification failed: %s", e)


def _parse_platform_targets() -> list[dict]:
    """Parse APIFY_TASKS into per-platform lookup targets.

    Format: "LABEL:taskId,LABEL:taskId,..." (comma-separated LABEL:taskId
    pairs). Falls back to a single legacy target scoped to APIFY_ACTOR_ID
    (today's original single-platform behavior, labeled "MO") if APIFY_TASKS
    isn't set at all — so an existing MO-only deployment's secrets keep
    working unchanged.
    """
    raw = os.environ.get("APIFY_TASKS", "").strip()
    if not raw:
        # GitHub Actions sets optional secrets to an empty string (not
        # absent) when unconfigured — `.get(key, default)` only falls back
        # on a missing key, not an empty value, so use `or` instead.
        actor_id = os.environ.get("APIFY_ACTOR_ID") or DEFAULT_ACTOR_ID
        return [{"label": "MO", "task_id": None, "actor_id": actor_id}]

    targets = []
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            log.warning("Skipping malformed APIFY_TASKS entry (expected LABEL:taskId): %r", pair)
            continue
        label, task_id = pair.split(":", 1)
        label, task_id = label.strip(), task_id.strip()
        if not label or not task_id:
            log.warning("Skipping malformed APIFY_TASKS entry (expected LABEL:taskId): %r", pair)
            continue
        targets.append({"label": label, "task_id": task_id, "actor_id": None})
    return targets


def _get_last_succeeded_run(token: str, *, task_id: str | None = None, actor_id: str | None = None) -> dict:
    """Look up the last SUCCEEDED run, scoped to a Task if given, else an Actor.

    Task-scoped lookup (`/actor-tasks/{taskId}/runs/last`) is what makes
    multi-platform safe — each scheduled Task's run history is independent
    even though MO/NM/KS all share one underlying Actor. The Actor-scoped
    lookup (`/acts/{actorId}/runs/last`) is kept only for the legacy
    single-platform fallback path.
    """
    if task_id:
        url = f"{API_BASE}/actor-tasks/{task_id}/runs/last"
    elif actor_id:
        url = f"{API_BASE}/acts/{actor_id}/runs/last"
    else:
        raise ValueError("_get_last_succeeded_run requires task_id or actor_id")
    resp = requests.get(url, params={"token": token, "status": "SUCCEEDED"}, timeout=30)
    resp.raise_for_status()
    return resp.json()["data"]


def _list_kvs_keys(token: str, store_id: str, prefix: str = "datasift_") -> list[str]:
    url = f"{API_BASE}/key-value-stores/{store_id}/keys"
    resp = requests.get(url, params={"token": token}, timeout=30)
    resp.raise_for_status()
    items = resp.json()["data"]["items"]
    return [item["key"] for item in items if item["key"].startswith(prefix) and item["key"].endswith(".csv")]


def _download_kvs_record(token: str, store_id: str, key: str, dest: Path) -> None:
    url = f"{API_BASE}/key-value-stores/{store_id}/records/{key}"
    resp = requests.get(url, params={"token": token}, timeout=60)
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def _process_target(token: str, target: dict, email: str, password: str) -> bool:
    """Run the full fetch -> download -> upload -> notify flow for one platform.

    Returns True on success (including the "zero CSVs, nothing to do" case,
    which isn't a failure). Every failure path notifies and returns False;
    the caller loops over platforms independently so one bad platform
    doesn't block the others' uploads or notifications.
    """
    label = target["label"]
    tag = f"[{label}] "

    try:
        run = _get_last_succeeded_run(token, task_id=target.get("task_id"), actor_id=target.get("actor_id"))
    except Exception as e:
        msg = f"⚠️ {tag}Stage 2 DataSift upload: couldn't reach the Apify API ({e}). Nothing uploaded — check manually before 4am."
        log.error(msg)
        _notify(msg, ok=False)
        return False

    store_id = run.get("defaultKeyValueStoreId")
    finished_at = run.get("finishedAt", "")

    if not store_id:
        msg = f"⚠️ {tag}Stage 2 DataSift upload: last SUCCEEDED run `{run.get('id')}` has no KVS store — nothing to upload. Check manually before 4am."
        log.error(msg)
        _notify(msg, ok=False)
        return False

    finished_dt = None
    if finished_at:
        finished_dt = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - finished_dt).total_seconds() / 3600
        if age_hours > MAX_RUN_AGE_HOURS:
            msg = (
                f"⚠️ {tag}Stage 2 DataSift upload: no fresh run to upload. Last SUCCEEDED "
                f"run finished {age_hours:.1f}h ago ({finished_at}) — older than the "
                f"{MAX_RUN_AGE_HOURS}h freshness window, likely means tonight's scrape "
                f"hasn't finished or failed. Nothing uploaded — check the Apify Task before 4am."
            )
            log.error(msg)
            _notify(msg, ok=False)
            return False

    try:
        keys = _list_kvs_keys(token, store_id)
    except Exception as e:
        msg = f"⚠️ {tag}Stage 2 DataSift upload: couldn't list KVS records for run `{run.get('id')}` ({e}). Nothing uploaded — check manually before 4am."
        log.error(msg)
        _notify(msg, ok=False)
        return False

    if not keys:
        msg = (
            f"ℹ️ {tag}Stage 2 DataSift upload: run `{run.get('id')}` succeeded but produced zero "
            f"datasift_*.csv records — likely zero notices survived filtering last night. "
            f"Nothing to upload (not a failure, just an FYI)."
        )
        log.warning(msg)
        _notify(msg, ok=True)
        return True

    date_str = finished_dt.strftime("%Y-%m-%d") if finished_dt else datetime.now().strftime("%Y-%m-%d")

    tmp_dir = Path(f"/tmp/stage2_datasift/{label}")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    csv_infos = []
    try:
        for key in sorted(keys):
            csv_label = LABEL_MAP.get(key, key.removeprefix("datasift_").removesuffix(".csv").replace("_", " ").title())
            dest = tmp_dir / key
            _download_kvs_record(token, store_id, key, dest)
            csv_infos.append({
                "path": dest,
                "label": csv_label,
                # Platform label included so same-day MO/NM/KS lists never
                # collide under an identical DataSift list name.
                "list_name": f"SiftStack {date_str} {label} - {csv_label}",
            })
            log.info("%sDownloaded %s (%d bytes) -> label=%s", tag, key, dest.stat().st_size, csv_label)
    except Exception as e:
        msg = f"⚠️ {tag}Stage 2 DataSift upload: failed downloading CSVs from KVS for run `{run.get('id')}` ({e}). Nothing uploaded — check manually before 4am."
        log.error(msg)
        _notify(msg, ok=False)
        return False

    try:
        from datasift_uploader import upload_datasift_split, upload_to_datasift

        if len(csv_infos) > 1:
            result = asyncio.run(
                upload_datasift_split(csv_infos, email=email, password=password, headless=True)
            )
        else:
            result = asyncio.run(
                upload_to_datasift(csv_infos[0]["path"], email=email, password=password, headless=True)
            )
    except Exception as e:
        msg = f"⚠️ {tag}Stage 2 DataSift upload: upload automation crashed for run `{run.get('id')}` ({e}). CSVs were downloaded but NOT uploaded — check manually before 4am."
        log.error(msg, exc_info=True)
        _notify(msg, ok=False)
        return False

    log.info("%sUpload result: %s", tag, result)

    status_emoji = "✅" if result.get("success") else "⚠️"
    _notify(
        f"{status_emoji} {tag}Stage 2 DataSift upload for run `{run.get('id')}`: "
        f"{result.get('message', 'no message')}",
        ok=bool(result.get("success")),
    )

    return bool(result.get("success"))


def main() -> int:
    token = os.environ.get("APIFY_TOKEN", "")
    email = os.environ.get("DATASIFT_EMAIL", "")
    password = os.environ.get("DATASIFT_PASSWORD", "")

    if not token:
        msg = "⚠️ Stage 2 DataSift upload: `APIFY_TOKEN` not set — cannot run at all. Check GitHub Actions secrets."
        log.error(msg)
        _notify(msg, ok=False)
        return 1
    if not email or not password:
        msg = "⚠️ Stage 2 DataSift upload: `DATASIFT_EMAIL`/`DATASIFT_PASSWORD` not set — cannot upload. Check GitHub Actions secrets."
        log.error(msg)
        _notify(msg, ok=False)
        return 1

    targets = _parse_platform_targets()
    log.info("Stage 2 running for %d platform(s): %s", len(targets), ", ".join(t["label"] for t in targets))

    # Import here (not at module level) so the early-return credential-check
    # paths above don't require Playwright + the rest of src/ to already be
    # importable.
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    overall_ok = True
    for target in targets:
        ok = _process_target(token, target, email, password)
        overall_ok = overall_ok and ok

    return 0 if overall_ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # last-resort catch-all so a crash is never fully silent
        log.error("Stage 2 uploader crashed unexpectedly: %s", e, exc_info=True)
        _notify(f"⚠️ Stage 2 DataSift upload crashed unexpectedly: {e}. Check GitHub Actions logs before 4am.", ok=False)
        sys.exit(1)
