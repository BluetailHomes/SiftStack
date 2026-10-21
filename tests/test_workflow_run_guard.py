"""Tests for src/workflow_run_guard.py (fail-closed once-per-day guard) and the carried-over line caps."""
import datetime as dt
import io
import json
import sys
import zipfile
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import pm_text_sender  # noqa: E402
import workflow_run_guard as g  # noqa: E402

NOW = dt.datetime(2026, 10, 21, 21, 31)          # 16:31 Central (CDT)
DAY = "2026-10-21"
STEP = "Afternoon voicemail texts"


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, v in files.items():
            z.writestr(n, json.dumps(v))
    return buf.getvalue()


def _job(started="2026-10-21T21:30:05Z", conclusion="success", status="completed", send="success", name="pm-texts"):
    steps = [{"name": "Once-per-day guard", "conclusion": "success"}]
    if send is not None:
        steps.append({"name": f"{STEP} (dry run unless PM_TEXTS_LIVE is \"true\")", "conclusion": send})
    return {"name": name, "status": status, "conclusion": conclusion, "started_at": started, "steps": steps}


@pytest.fixture
def gh(monkeypatch, tmp_path):
    """Fake GitHub: runs = {run_id: (job dict, artifact bytes or None)}."""
    world = {"runs": {}}
    monkeypatch.setattr(g, "STATE_DIR", tmp_path)

    def fake_get(path):
        if path.startswith("/actions/workflows/"):
            return {"workflow_runs": [{"id": rid} for rid in world["runs"]]}
        rid = int(path.split("/")[3])
        job, art = world["runs"][rid]
        if path.endswith("/jobs"):
            return {"jobs": [job, {"name": "audit", "conclusion": "skipped"}]}
        return {"artifacts": [] if art is None else [{"name": f"pm-texts-{rid}", "expired": False, "archive_download_url": f"zip:{rid}"}]}

    monkeypatch.setattr(g, "_get", fake_get)
    monkeypatch.setattr(g, "_download", lambda url: world["runs"][int(url.split(":")[1])][1])
    return world


def check(world_run_id=999):
    return g.check("pm-texts", "16:15-17:00", STEP, str(world_run_id), NOW)


def test_no_earlier_run_runs(gh):
    assert check()[0] == "run"


def test_finished_run_in_window_skips(gh):
    gh["runs"][1] = (_job(), _zip({f"sent_{DAY}.json": []}))
    verdict, why = check()
    assert verdict == "skip" and "run 1" in why


def test_own_run_is_ignored(gh):
    gh["runs"][999] = (_job(), None)
    assert check(999)[0] == "run"


def test_gated_noop_outside_window_does_not_count(gh):
    gh["runs"][1] = (_job(started="2026-10-21T19:00:00Z"), None)   # 14:00 Central, outside the window
    assert check()[0] == "run"


def test_partial_failure_with_log_runs_and_carries_sends_over(gh, tmp_path):
    sent = [{"to": "8165550001", "line": pm_text_sender.PM_LINES[0], "ok": True, "sent_at": "t1"},
            {"to": "8165550002", "line": pm_text_sender.PM_LINES[0], "ok": True, "sent_at": "t2"}]
    gh["runs"][1] = (_job(conclusion="failure", send="failure"), _zip({f"sent_{DAY}.json": sent}))
    verdict, why = check()
    assert verdict == "run" and "will not repeat" in why
    assert json.loads((tmp_path / f"sent_{DAY}.json").read_text()) == sent


def test_partial_failure_without_log_blocks(gh):
    gh["runs"][1] = (_job(conclusion="failure", send="failure"), None)
    verdict, why = check()
    assert verdict == "block" and "unknown" in why


def test_failure_before_send_step_runs(gh):
    gh["runs"][1] = (_job(conclusion="failure", send="skipped"), None)
    assert check()[0] == "run"


def test_other_run_in_progress_blocks(gh):
    gh["runs"][1] = (_job(status="in_progress", conclusion=None), None)
    assert check()[0] == "block"


def test_queued_run_behind_us_does_not_block(gh):
    gh["runs"][1] = (_job(status="queued", conclusion=None, started=None, send=None), None)
    assert check()[0] == "run"


def test_tomorrow_runs(gh):
    gh["runs"][1] = (_job(), _zip({f"sent_{DAY}.json": []}))
    assert g.check("pm-texts", "16:15-17:00", STEP, "999", NOW + dt.timedelta(days=1))[0] == "run"


def test_api_error_fails_closed_with_alert(monkeypatch, tmp_path):
    def boom(path):
        raise OSError("network down")
    alerts = []
    out = tmp_path / "out"
    monkeypatch.setattr(g, "_get", boom)
    monkeypatch.setattr(g, "_slack", alerts.append)
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(sys, "argv", ["x", "pm-texts", "16:15-17:00", STEP])
    assert g.main() == 1
    assert "skip=true" in out.read_text()
    assert alerts and "did NOT run" in alerts[0]


def test_am_start_results_merge(gh, tmp_path):
    a = {"results": [{"uuid": "u1", "line": "SMS Line A", "ok": True}]}
    b = {"results": [{"uuid": "u1", "line": "SMS Line A", "ok": True}, {"uuid": "u2", "line": "SMS Line B", "ok": True}]}
    g._merge_state({f"am_start_{DAY}.json": json.dumps(a).encode()}, DAY)
    g._merge_state({f"x/am_start_{DAY}.json": json.dumps(b).encode()}, DAY)
    got = json.loads((tmp_path / f"am_start_{DAY}.json").read_text())["results"]
    assert [r["uuid"] for r in got] == ["u1", "u2"]


def _model(n: int) -> dict:
    num = f"81655500{n:02d}"
    return {"uuid": f"u{n}", "street": f"{n} Main St", "owner_first": "Pat", "status": "prospecting",
            "attempts": 1, "attempt_bumps": 1, "owner_dnc": False, "owner_opt_out": False,
            "phones": {num: {"status": "NO_ANSWER", "tags": ["Dial First"]}}, "marks": [], "sms_in": [], "sms_out": [],
            "calls": [{"by": "rahaf", "dir": "outbound", "result": "answered", "to": num, "dur": 35,
                       "ct": dt.datetime(2026, 10, 21, 11, 0)}]}


def test_pm_text_line_cap_counts_earlier_sends():
    """Earlier today: line 0 sent 24, line 1 sent 25, line 2 sent 20. Ten more voicemails:
    1 fits on line 0, none on line 1, 5 on line 2, the other 4 are overflow (reported, never sent)."""
    lines = pm_text_sender.PM_LINES
    prior = Counter({lines[0]: 24, lines[1]: 25, lines[2]: 20})
    to_send, skipped = pm_text_sender.plan_pm_texts([_model(i) for i in range(10)], DAY, "rahaf", set(), prior)
    assert not skipped and len(to_send) == 10
    used = Counter(e["line"] for e in to_send if not e["overflow"])
    assert used == {lines[0]: 1, lines[2]: 5}
    assert sum(e["overflow"] for e in to_send) == 4
    assert all(prior[ln] + used[ln] <= pm_text_sender.MAX_PER_LINE for ln in lines)


def test_pm_text_already_sent_number_is_skipped():
    to_send, skipped = pm_text_sender.plan_pm_texts([_model(1)], DAY, "rahaf", {"8165550001"})
    assert not to_send and skipped[0]["why"] == "afternoon text already sent today"


def test_pm_text_rotation_unchanged_without_prior():
    to_send, _ = pm_text_sender.plan_pm_texts([_model(i) for i in range(6)], DAY, "rahaf", set())
    assert [e["line"] for e in to_send] == list(pm_text_sender.PM_LINES) * 2


def test_pm_text_landline_number_is_skipped():
    m = _model(1)
    m["phones"]["8165550001"]["type"] = "LANDLINE"
    to_send, skipped = pm_text_sender.plan_pm_texts([m], DAY, "rahaf", set())
    assert not to_send and skipped[0]["why"].startswith("number is LANDLINE, not mobile")


def test_pm_text_mobile_or_untyped_number_is_sent():
    a, b = _model(1), _model(2)
    a["phones"]["8165550001"]["type"] = "MOBILE"
    to_send, skipped = pm_text_sender.plan_pm_texts([a, b], DAY, "rahaf", set())
    assert not skipped and len(to_send) == 2
