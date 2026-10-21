"""Fail-closed guard for the two live seller-outreach jobs in pm-text-sender.yml (am-start, pm-texts).

Runs before the job's send step. It looks at every earlier run of the same job today and:

1. Downloads each one's output artifact and merges its state files into output/pm_texts/
   (sent_<day>.json = every text already sent, am_start_<day>.json = every record already
   started). pm_text_sender.py reads those, so a retry never re-texts a number that was
   already texted and the 25-per-line caps count everything sent today, not just this run.
2. Skips this run (skip=true, exit 0) when an earlier run already succeeded inside the
   job's Central window: the day's batch is done.
3. BLOCKS this run (exit 1 + Slack alert) whenever it cannot be sure a send would be safe:
   the GitHub API can't be read, an artifact can't be downloaded, another run of the job is
   still in progress, or an earlier run reached its send step but left no state file.
   For seller texts a missed batch is better than a duplicate, so every doubt stops the run.

The job's `concurrency:` group makes overlapping starts queue, so a second start always
sees the first one's finished result here.

Usage (workflow step, needs permissions: actions: read):
    python src/workflow_run_guard.py <job> <HH:MM-HH:MM window> "<send step name prefix>"
Env: GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_OUTPUT, SLACK_WEBHOOK_URL (optional)
"""
import datetime as dt
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from pm_text_sender import _central_offset  # same Central-time rule the jobs use (no tzdata needed)

WORKFLOW = "pm-text-sender.yml"
STATE_DIR = Path(__file__).resolve().parent.parent / "output" / "pm_texts"
API = "https://api.github.com"
ACTIVE = ("in_progress",)  # queued runs wait behind this one in the concurrency group, so only a running one is a conflict


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{API}/repos/{os.environ['GITHUB_REPOSITORY']}{path}",
                                 headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                                          "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _download(url: str) -> bytes:
    """Artifact zips answer with a redirect to a signed storage URL that must be fetched WITHOUT the token."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"})
    try:
        urllib.request.build_opener(_NoRedirect).open(req, timeout=60)
        raise RuntimeError(f"artifact download did not redirect: {url}")
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        with urllib.request.urlopen(e.headers["Location"], timeout=120) as r:
            return r.read()


def _central(ts: str) -> dt.datetime:
    u = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    return u + _central_offset(u)


def _hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def _merge_state(files: dict[str, bytes], day: str) -> list[str]:
    """Merge state files from an earlier run into STATE_DIR. Returns the names merged."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    merged = []
    for name, raw in files.items():
        base = name.rsplit("/", 1)[-1]
        target = STATE_DIR / base
        if base == f"sent_{day}.json":
            old = json.loads(target.read_text(encoding="utf-8")) if target.exists() else []
            seen = {(s.get("to"), s.get("sent_at")) for s in old}
            old += [s for s in json.loads(raw) if (s.get("to"), s.get("sent_at")) not in seen]
            target.write_text(json.dumps(old, indent=1), encoding="utf-8")
            merged.append(base)
        elif base == f"am_start_{day}.json":
            old = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"results": []}
            have = {(r.get("uuid"), r.get("ok")) for r in old.get("results", [])}
            old["results"] = old.get("results", []) + [
                r for r in json.loads(raw).get("results", []) if (r.get("uuid"), r.get("ok")) not in have]
            target.write_text(json.dumps(old, indent=1), encoding="utf-8")
            merged.append(base)
    return merged


def check(job: str, window: str, step_prefix: str, run_id: str, now_utc: dt.datetime) -> tuple[str, str]:
    """Return ("run" | "skip" | "block", reason). Raises on any API problem (the caller blocks)."""
    lo, hi = (_hm(x) for x in window.split("-"))
    now_utc = now_utc.replace(tzinfo=None)
    today = (now_utc + _central_offset(now_utc)).date()
    day = today.isoformat()
    state_name = f"sent_{day}.json" if job == "pm-texts" else f"am_start_{day}.json"
    since = dt.datetime.combine(today, dt.time(0)) - dt.timedelta(hours=1)
    runs = _get(f"/actions/workflows/{WORKFLOW}/runs?per_page=100&created=>={since:%Y-%m-%dT%H:%M:%SZ}").get("workflow_runs", [])
    done_by = None
    for run in runs:
        if str(run["id"]) == str(run_id):
            continue
        j = next((x for x in _get(f"/actions/runs/{run['id']}/jobs").get("jobs", []) if x["name"] == job), None)
        if j is None or j.get("conclusion") == "skipped":
            continue
        if j.get("status") in ACTIVE:
            return "block", f"another {job} run ({run['id']}) is still {j['status']}"
        if not j.get("started_at"):
            continue
        started = _central(j["started_at"])
        if started.date() != today:
            continue
        send = next((s for s in j.get("steps", []) if s["name"].startswith(step_prefix)), None)
        reached_send = bool(send) and send.get("conclusion") not in (None, "skipped")
        merged = []
        for a in _get(f"/actions/runs/{run['id']}/artifacts").get("artifacts", []):
            if a.get("expired"):
                return "block", f"artifact {a['name']} of run {run['id']} has expired"
            z = zipfile.ZipFile(io.BytesIO(_download(a["archive_download_url"])))
            merged += _merge_state({n: z.read(n) for n in z.namelist()}, day)
        in_window = lo <= started.time() <= hi
        if reached_send and in_window and j.get("conclusion") != "success" and state_name not in merged:
            return "block", (f"run {run['id']} (started {started:%H:%M}) reached its send step and failed, "
                             f"but left no {state_name}, so what it sent is unknown")
        if reached_send and in_window and j.get("conclusion") == "success":
            done_by = f"run {run['id']} (started {started:%H:%M} Central)"
    if done_by:
        return "skip", f"{job} already ran today: {done_by}"
    carried = (STATE_DIR / state_name).exists()
    return "run", f"no completed {job} run today" + (" (earlier sends merged in, so they will not repeat)" if carried else "")


def _slack(text: str) -> None:
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                                      headers={"Content-Type": "application/json"}), timeout=15)
    except Exception as e:  # noqa: BLE001 - the exit code still fails the job visibly
        print(f"(Slack alert failed: {e})")


def main() -> int:
    job, window, step_prefix = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        verdict, why = check(job, window, step_prefix, os.environ.get("GITHUB_RUN_ID", ""), dt.datetime.now(dt.timezone.utc))
    except Exception as e:  # noqa: BLE001 - fail closed
        verdict, why = "block", f"could not check earlier runs: {type(e).__name__}: {e}"
    print(f"{verdict}: {why}")
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a", encoding="utf-8") as f:
        f.write(f"skip={'false' if verdict == 'run' else 'true'}\n")
    if verdict == "block":
        _slack(f":octagonal_sign: *{job}* did NOT run (safety guard): {why}. Nothing was sent. "
               f"Check GitHub Actions run {os.environ.get('GITHUB_RUN_ID', '?')} and decide whether to send by hand.")
        return 1
    if verdict == "skip":
        _slack(f":information_source: A second *{job}* start was skipped: {why}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
