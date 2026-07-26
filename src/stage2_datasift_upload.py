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
purpose of a 2am scheduled run meant to beat Rahaf's 4am start — nobody
is awake at 2am to do the manual upload.

This script closes the loop without touching the Apify container at all:
it runs in a *different* unattended environment (GitHub Actions, scheduled
~1 hour after the Apify run), fetches the already-DataSift-formatted CSVs
straight out of the Apify KVS, and calls the exact same upload_to_datasift/
upload_datasift_split Playwright automation used successfully by the local
CLI path (`--upload-datasift`). No re-enrichment happens here — the CSVs
pulled from KVS are already fully enriched (Smarty, Zillow, tax, obituary/
DM, Tracerfy, Trestle all ran inside the Actor); this script's only job is
the upload that the Actor intentionally skipped.

Notification policy
--------------------
Every exit path posts to Slack (when SLACK_WEBHOOK_URL is set) — not just
the success path. The whole point of this script is to catch the case where
the 2am Apify run ran long, failed, or produced nothing; if THAT case is
also the one case that stays silent, the safety net has a hole in exactly
the spot it exists to cover. So: missing secrets, unreachable Apify API,
no KVS store, a stale/not-yet-finished run, zero CSVs, a download failure,
and an unexpected crash all notify, in addition to the upload result itself.

Required environment variables
-------------------------------
APIFY_TOKEN         Apify API token (same account as the scheduled Actor)
DATASIFT_EMAIL      DataSift login email
DATASIFT_PASSWORD   DataSift login password
APIFY_ACTOR_ID      Optional. Defaults to DLE4KmqvBSxrxGUab
                     (neat_honeyeater/tn-public-notice-scraper)
SLACK_WEBHOOK_URL   Optional. Posts a status message on every exit path.

Usage
-----
    cd src
    python stage2_datasift_upload.py

Intended to run on a schedule (e.g. GitHub Actions, ~3:00-3:15am MST) —
NOT inside the Apify Actor. See .github/workflows/stage2-datasift-upload.yml.
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
DEFAULT_ACTOR_ID = "DLE4KmqvBSxrxGUab"  # neat_honeyeater/tn-public-notice-scraper

# Maps the KVS key names written by main.py's Actor path (see
# "DataSift CSVs -> KVS (manual upload)" section) back to the label used
# by write_datasift_split_csvs(). Falls back to a title-cased guess for any
# key that doesn't match, so a future third CSV type doesn't silently drop.
LABEL_MAP = {
    "datasift_dms.csv": "DMs",
    "datasift_heirs.csv": "Heirs",
}

# If the most recent SUCCEEDED run is older than this, refuse to upload —
# it's almost certainly a stale/previous day's run (e.g. tonight's 2am scrape
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


def _get_last_succeeded_run(token: str, actor_id: str) -> dict:
    url = f"{API_BASE}/acts/{actor_id}/runs/last"
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


def main() -> int:
    token = os.environ.get("APIFY_TOKEN", "")
    actor_id = os.environ.get("APIFY_ACTOR_ID", DEFAULT_ACTOR_ID)
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

    try:
        run = _get_last_succeeded_run(token, actor_id)
    except Exception as e:
        msg = f"⚠️ Stage 2 DataSift upload: couldn't reach the Apify API ({e}). Nothing uploaded — check manually before 4am."
        log.error(msg)
        _notify(msg, ok=False)
        return 1

    store_id = run.get("defaultKeyValueStoreId")
    finished_at = run.get("finishedAt", "")

    if not store_id:
        msg = f"⚠️ Stage 2 DataSift upload: last SUCCEEDED run `{run.get('id')}` has no KVS store — nothing to upload. Check manually before 4am."
        log.error(msg)
        _notify(msg, ok=False)
        return 1

    finished_dt = None
    if finished_at:
        finished_dt = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - finished_dt).total_seconds() / 3600
        if age_hours > MAX_RUN_AGE_HOURS:
            msg = (
                f"⚠️ Stage 2 DataSift upload: no fresh Actor run to upload. Last SUCCEEDED "
                f"run finished {age_hours:.1f}h ago ({finished_at}) — older than the "
                f"{MAX_RUN_AGE_HOURS}h freshness window, likely means tonight's 2am scrape "
                f"hasn't finished or failed. Nothing uploaded — check the Apify Actor before 4am."
            )
            log.error(msg)
            _notify(msg, ok=False)
            return 1

    try:
        keys = _list_kvs_keys(token, store_id)
    except Exception as e:
        msg = f"⚠️ Stage 2 DataSift upload: couldn't list KVS records for run `{run.get('id')}` ({e}). Nothing uploaded — check manually before 4am."
        log.error(msg)
        _notify(msg, ok=False)
        return 1

    if not keys:
        msg = (
            f"ℹ️ Stage 2 DataSift upload: run `{run.get('id')}` succeeded but produced zero "
            f"datasift_*.csv records — likely zero notices survived filtering last night. "
            f"Nothing to upload (not a failure, just an FYI)."
        )
        log.warning(msg)
        _notify(msg, ok=True)
        return 0

    date_str = finished_dt.strftime("%Y-%m-%d") if finished_dt else datetime.now().strftime("%Y-%m-%d")

    tmp_dir = Path("/tmp/stage2_datasift")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    csv_infos = []
    try:
        for key in sorted(keys):
            label = LABEL_MAP.get(key, key.removeprefix("datasift_").removesuffix(".csv").replace("_", " ").title())
            dest = tmp_dir / key
            _download_kvs_record(token, store_id, key, dest)
            csv_infos.append({
                "path": dest,
                "label": label,
                "list_name": f"SiftStack {date_str} - {label}",
            })
            log.info("Downloaded %s (%d bytes) -> label=%s", key, dest.stat().st_size, label)
    except Exception as e:
        msg = f"⚠️ Stage 2 DataSift upload: failed downloading CSVs from KVS for run `{run.get('id')}` ({e}). Nothing uploaded — check manually before 4am."
        log.error(msg)
        _notify(msg, ok=False)
        return 1

    # Import here (not at module level) so the early-return paths above don't
    # require Playwright + the rest of src/ to already be importable.
    sys.path.insert(0, str(Path(__file__).resolve().parent))

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
        msg = f"⚠️ Stage 2 DataSift upload: upload automation crashed for run `{run.get('id')}` ({e}). CSVs were downloaded but NOT uploaded — check manually before 4am."
        log.error(msg, exc_info=True)
        _notify(msg, ok=False)
        return 1

    log.info("Upload result: %s", result)

    status_emoji = "✅" if result.get("success") else "⚠️"
    _notify(
        f"{status_emoji} Stage 2 DataSift upload for run `{run.get('id')}`: "
        f"{result.get('message', 'no message')}",
        ok=bool(result.get("success")),
    )

    return 0 if result.get("success") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # last-resort catch-all so a crash is never fully silent
        log.error("Stage 2 uploader crashed unexpectedly: %s", e, exc_info=True)
        _notify(f"⚠️ Stage 2 DataSift upload crashed unexpectedly: {e}. Check GitHub Actions logs before 4am.", ok=False)
        sys.exit(1)
