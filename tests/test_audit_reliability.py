"""Tests for the end-of-day audit's KPI integration and reliability: load_day skip/stats
accounting, call ids and the deduped dial header, the KPI hand-off (day + stats), the
audit_posted marker and exit code, the off-window alert, and the next-morning audit-check."""
import datetime as dt
import json
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pm_text_sender as pts  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DAY = "2026-10-26"
CALLER = pts.CALLER_EMAIL
LINE, OWNER_A, OWNER_B = "8165550100", "8165550001", "8165550002"


def _call_ev(uuid, ext_id, to, ts="2026-10-26T15:05:00Z", kind="owner.call.made"):
    return {"event_type": kind, "timestamp": ts,
            "payload": {"call": {"uuid": uuid, "external_id": ext_id, "origin_number": LINE, "destination_number": to,
                                 "direction": "outbound", "external_user": {"email": CALLER}}}}


def _det(street="1 MAIN ST"):
    return {"status": None, "assigned_to": pts.CALLER_USER_UUID, "tags": [], "address": {"street": street},
            "owner": {"first_name": "JANE", "phones": [{"number": OWNER_A, "status": "UNKNOWN"}]}}


class FakeSift:
    """search() returns `rows`; call() serves logs/detail from dicts, raising RuntimeError for
    any uuid listed in `fail_logs` / `fail_detail`."""

    def __init__(self, logs, details, fail_logs=(), fail_detail=()):
        self.logs, self.details = logs, details
        self.fail_logs, self.fail_detail = set(fail_logs), set(fail_detail)

    def search(self, _filters):
        return [{"uuid": u} for u in self.logs]

    def call(self, path, method="GET", body=None, override=None):
        u = path.split("/property/")[1].split("/")[0]
        if path.endswith("/logs/?limit=250&offset=0"):
            if u in self.fail_logs:
                raise RuntimeError("logs 500")
            return {"results": [dict(e) for e in self.logs[u]]}
        if u in self.fail_detail:
            raise RuntimeError("detail 500")
        return self.details[u]


# ---- load_day ----------------------------------------------------------------------------

def test_load_day_skips_unreadable_records_and_counts_them():
    logs = {"ok": [_call_ev("c1", "x1", OWNER_A)], "badlogs": [], "baddet": [_call_ev("c2", "x2", OWNER_A)],
            "quiet": [_call_ev("c3", "x3", OWNER_A, ts="2026-10-20T15:00:00Z")]}
    api = FakeSift(logs, {"ok": _det(), "baddet": _det(), "quiet": _det()}, fail_logs={"badlogs"}, fail_detail={"baddet"})
    stats = {}
    models = pts.load_day(api, DAY, CALLER, stats)
    assert [m["uuid"] for m in models] == ["ok"]
    # "quiet" had no activity on the day: not selected for the report, but not an error either.
    assert stats == {"selected": 4, "skipped": 2, "errors": 0}


def test_load_day_skips_malformed_record_instead_of_crashing():
    bad = {"event_type": "owner.call.made", "timestamp": "2026-10-26T15:00:00Z", "payload": {}}  # no "call"
    api = FakeSift({"ok": [_call_ev("c1", "x1", OWNER_A)], "bad": [bad]}, {"ok": _det(), "bad": _det()})
    stats = {}
    models = pts.load_day(api, DAY, CALLER, stats)
    assert [m["uuid"] for m in models] == ["ok"]
    assert stats["skipped"] == 1


def test_load_day_works_without_stats_and_keeps_call_id():
    api = FakeSift({"ok": [_call_ev("c1", "smrt-77", OWNER_A)]}, {"ok": _det()})
    (m,) = pts.load_day(api, DAY, CALLER)
    assert m["calls"][0]["id"] == "smrt-77"


# ---- audit header dedupe -----------------------------------------------------------------

def _model(uuid, calls):
    return {"uuid": uuid, "street": f"{uuid} ST", "status": None, "assigned_to": pts.CALLER_USER_UUID, "tags": [],
            "attempts": 0, "owner_first": "JANE", "owner_dnc": False, "owner_opt_out": False, "phones": {},
            "stop": {}, "primary": None, "open_tasks": None, "calls": calls, "call_days": {DAY},
            "sms_out": [], "sms_in": [], "marks": [], "status_changes": [], "attempt_bumps": [], "notes": [],
            "actions": [c["ct"] for c in calls], "task_events": 0, "tags_added": []}


def _c(ext_id, to, minute, result="noanswer"):
    ct = dt.datetime(2026, 10, 26, 10, minute, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    return {"ct": ct, "id": ext_id, "from": LINE, "to": to, "dir": "outbound", "by": CALLER,
            "dur": None, "result": result, "rec": None}


def _header(report):
    return next(l for l in report.splitlines() if l.startswith("Dials "))


def test_header_counts_shared_number_call_once():
    # One smrtPhone call to a number two owners share is logged on both records.
    models = [_model("a", [_c("x1", OWNER_A, 5), _c("x2", OWNER_B, 9, "answered")]), _model("b", [_c("x1", OWNER_A, 5)])]
    h = _header(pts.build_audit(models, DAY, CALLER))
    assert h.startswith("Dials 2 ") and "on 2 records" in h
    assert "Answered 1, no answer 1." in h
    assert "(1 more log rows are the same calls on other records.)" in h


def test_header_without_call_id_falls_back_to_line_number_time():
    models = [_model("a", [_c(None, OWNER_A, 5)]), _model("b", [_c(None, OWNER_A, 5)]), _model("c", [_c(None, OWNER_A, 6)])]
    assert _header(pts.build_audit(models, DAY, CALLER)).startswith("Dials 2 ")


def test_header_has_no_duplicate_note_when_none():
    h = _header(pts.build_audit([_model("a", [_c("x1", OWNER_A, 5)])], DAY, CALLER))
    assert h.startswith("Dials 1 ") and "more log rows" not in h


# ---- run_audit ---------------------------------------------------------------------------

def _audit_args(**kw):
    return Namespace(**{"only_between": None, "day": DAY, "slack": True, "alert_skip": False, **kw})


def _run_audit(tmp_path, posted=True, legacy_fails=False, **kw):
    seen = {}

    def fake_daily_lines(models, caller, day=None, stats=None, today=None):
        seen.update(day=day, stats=dict(stats))
        return "KPI"

    def fake_load_day(api, day, caller, stats=None):
        stats["selected"] += 3
        stats["skipped"] += 1
        return []

    def fake_legacy(api, day):
        if legacy_fails:
            raise RuntimeError("preset 500")
        return None

    import kpi_scorecard
    with patch.object(pts, "STATE_DIR", tmp_path), patch.object(pts, "_env", return_value="k"), \
            patch.object(pts, "Sift"), patch.object(pts, "load_day", fake_load_day), \
            patch.object(pts, "load_legacy_presets", fake_legacy), \
            patch.object(pts, "_slack", return_value=posted) as slack, \
            patch.object(kpi_scorecard, "daily_lines", fake_daily_lines):
        rc = pts.run_audit(_audit_args(**kw))
    marker = tmp_path / f"audit_posted_{DAY}.json"
    return rc, (json.loads(marker.read_text()) if marker.exists() else None), seen, slack


def test_run_audit_passes_day_and_stats_to_kpi(tmp_path):
    rc, marker, seen, _ = _run_audit(tmp_path, legacy_fails=True)
    assert rc == 0
    assert seen["day"] == dt.date(2026, 10, 26)
    assert seen["stats"] == {"selected": 3, "skipped": 1, "errors": 1}
    assert marker["posted"] is True and marker["stats"]["errors"] == 1


def test_run_audit_failed_post_marks_not_posted_and_exits_1(tmp_path):
    rc, marker, _, _ = _run_audit(tmp_path, posted=False)
    assert rc == 1 and marker["posted"] is False


def test_run_audit_without_slack_is_a_dry_run(tmp_path):
    rc, marker, _, slack = _run_audit(tmp_path, slack=False)
    assert rc == 0 and marker["posted"] is False and not slack.called


def test_dispatched_run_outside_window_alerts(tmp_path):
    with patch.object(pts, "_in_window", return_value=False), patch.object(pts, "_slack") as slack, \
            patch.object(pts, "STATE_DIR", tmp_path):
        assert pts.run_audit(_audit_args(only_between="19:00-20:00", alert_skip=True)) == 0
    assert "did not run" in slack.call_args[0][0]
    assert not list(tmp_path.iterdir())  # no posted marker, so the next-morning check alerts too


def test_fallback_run_outside_window_stays_quiet(tmp_path):
    with patch.object(pts, "_in_window", return_value=False), patch.object(pts, "_slack") as slack:
        assert pts.run_audit(_audit_args(only_between="19:00-20:00")) == 0
    assert not slack.called


# ---- next-morning audit-check ------------------------------------------------------------

def test_prev_business_day():
    assert pts.prev_business_day(dt.date(2026, 11, 2)) == dt.date(2026, 10, 30)   # Mon -> Fri
    assert pts.prev_business_day(dt.date(2026, 10, 27)) == dt.date(2026, 10, 26)  # Tue -> Mon
    assert pts.prev_business_day(dt.date(2026, 11, 1)) == dt.date(2026, 10, 30)   # Sun -> Fri


def _marker(folder, run, posted, day=DAY):
    p = folder / run
    p.mkdir(parents=True)
    (p / f"audit_posted_{day}.json").write_text(json.dumps({"day": day, "posted": posted}))


def test_marker_missing(tmp_path):
    assert "did not run" in pts.audit_marker_problem(tmp_path, DAY)
    assert "did not run" in pts.audit_marker_problem(tmp_path / "nope", DAY)


def test_marker_not_posted(tmp_path):
    _marker(tmp_path, "111", False)
    assert "not posted" in pts.audit_marker_problem(tmp_path, DAY)


def test_marker_posted_on_any_run_counts(tmp_path):
    _marker(tmp_path, "111", False)
    _marker(tmp_path, "222", True)
    assert pts.audit_marker_problem(tmp_path, DAY) is None


def test_marker_for_another_day_does_not_count(tmp_path):
    _marker(tmp_path, "111", True, day="2026-10-23")
    assert pts.audit_marker_problem(tmp_path, DAY) is not None


def test_corrupt_marker_is_not_success(tmp_path):
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / f"audit_posted_{DAY}.json").write_text("{not json")
    assert pts.audit_marker_problem(tmp_path, DAY) is not None


def test_run_audit_check_alerts_and_fails(tmp_path):
    with patch.object(pts, "_slack") as slack:
        assert pts.run_audit_check(Namespace(day=DAY, markers=str(tmp_path), slack=True)) == 1
    assert DAY in slack.call_args[0][0]


def test_run_audit_check_ok_is_silent(tmp_path):
    _marker(tmp_path, "1", True)
    with patch.object(pts, "_slack") as slack:
        assert pts.run_audit_check(Namespace(day=DAY, markers=str(tmp_path), slack=True)) == 0
    assert not slack.called


def test_run_audit_check_defaults_to_previous_business_day(tmp_path):
    _marker(tmp_path, "1", True, day="2026-10-30")
    mon = dt.datetime(2026, 11, 2, 9, 50, tzinfo=dt.timezone(dt.timedelta(hours=-6)))
    with patch.object(pts, "ct_now", return_value=mon), patch.object(pts, "_slack"):
        assert pts.run_audit_check(Namespace(day=None, markers=str(tmp_path), slack=True)) == 0


def test_slack_reports_failure():
    with patch.object(pts, "_env", return_value=None):
        assert pts._slack("x") is False


def test_cli_routes_audit_check_and_alert_skip():
    seen = []
    with patch.object(pts, "run_audit_check", lambda a: seen.append(a) or 0),             patch.object(pts, "run_audit", lambda a: seen.append(a) or 0):
        with patch.object(sys, "argv", ["pm_text_sender.py", "audit-check", "--day", DAY]):
            assert pts.main() == 0
        with patch.object(sys, "argv", ["pm_text_sender.py", "audit", "--alert-skip"]):
            assert pts.main() == 0
    assert seen[0].day == DAY and seen[0].markers.endswith("audit_markers") and seen[0].slack is False
    assert seen[1].alert_skip is True


# ---- workflow wiring ---------------------------------------------------------------------

def test_workflow_audit_reliability_wiring():
    wf = yaml.safe_load((ROOT / ".github/workflows/pm-text-sender.yml").read_text(encoding="utf-8"))
    on = wf.get("on", wf.get(True))
    crons = [c["cron"] for c in on["schedule"]]
    assert "15 0 * * 2-6" in crons and "15 1 * * 2-6" in crons  # 7:15 PM schedule unchanged
    audit = wf["jobs"]["audit"]
    assert audit["timeout-minutes"] == 30
    run = next(s["run"] for s in audit["steps"] if s.get("name") == "End-of-day caller audit")
    assert '--alert-skip' in run and 'inputs.gate }}" = "true"' in run
    alert = next(s for s in audit["steps"] if s.get("name") == "Alert on audit failure or timeout")
    assert alert["if"] == "failure() || cancelled()"
    check = wf["jobs"]["audit-check"]
    assert check["if"] == "github.event_name == 'workflow_dispatch' && inputs.mode == 'am-start' && inputs.gate"
    assert "schedule" not in check["if"]  # GitHub fallback crons must not alert a second/third time
    assert check["permissions"]["actions"] == "read"
    assert "audit-check --slack" in check["steps"][-1]["run"]
    assert "pm-texts" not in str(check) and "schedule" not in str(wf["jobs"]["pm-texts"].get("if", "")).replace("github.event_name == 'workflow_dispatch'", "")
