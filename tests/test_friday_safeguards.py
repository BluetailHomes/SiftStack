"""Tests for the pre-Tulum safeguards: STOP policy, line capacity, am-start interlocks, Friday drip
classification, the Friday readiness marker (weekly_topup.validate), and the drip guard's STOP check."""
import datetime as dt
import json
import sys
from argparse import Namespace
from collections import Counter
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import drip_exit_guard as guard  # noqa: E402
import pm_text_sender as pts  # noqa: E402
import weekly_topup as wt  # noqa: E402

CDT, CST = dt.timezone(dt.timedelta(hours=-5)), dt.timezone(dt.timedelta(hours=-6))
FRI_6PM = dt.datetime(2026, 10, 30, 18, 0, tzinfo=CDT)
TUE_NOON = dt.datetime(2026, 10, 27, 12, 0, tzinfo=CDT)
MON_950 = dt.datetime(2026, 11, 2, 9, 50, tzinfo=CST)
PRIMARY, OTHER = "8165550001", "8165550002"
BATCH = "Batch 2026-11-02"


def _phone(number, status="UNKNOWN", type_="MOBILE", tags=("Dial First",)):
    return {"number": number, "status": status, "type": type_, "tags": list(tags)}


def _det(phones=None, tags=(BATCH, "SMS Line A", "Priority 1"), status=None, owner=None, primary=PRIMARY):
    return {"status": status, "assigned_to": pts.CALLER_USER_UUID, "tags": list(tags),
            "address": {"street": "1 MAIN ST"},
            "owner": {"first_name": "JANE", "primary_phone": {"number": primary},
                      "phones": phones if phones is not None else [_phone(PRIMARY)], **(owner or {})}}


def _log(phone, old, new, ts="2026-10-20T15:00:00"):
    return {"event_type": "owner.phone.status.updated", "timestamp": ts,
            "payload": {"owner": {"phone": phone, "status": [old, new]}}}


class FakeApi:
    """GET-by-path fake of pm_text_sender.Sift. A POST of status starts a drip, like DataSift."""

    def __init__(self, props, drips=None, logs=None, fail=()):
        self.props, self.drips, self.logs, self.fail = props, drips or {}, logs or {}, set(fail)
        self.writes = []

    def call(self, path, method="GET", body=None):
        if method != "GET":
            self.writes.append((method, path, body))
            u = path.split("/")[4]
            self.drips.setdefault(u, []).append({"status": "running"})
            return {}
        u = path.split("=")[-1] if "by-property" in path else path.split("/")[4]
        if u in self.fail:
            raise RuntimeError("HTTP 500")
        if "by-property" in path:
            return {"results": self.drips.get(u, [])}
        if "/logs/" in path:
            return {"results": self.logs.get(u, [])}
        return self.props[u]


# ------------------------------------------------------------ STOP policy

def test_wrong_number_stop_blocks_only_that_number():
    s = pts.stop_policy({}, {OTHER: _phone(OTHER, "WRONG_DNC")}, {})
    assert s["blocked"] == {OTHER} and not s["owner_stop"] and not s["hold"]


def test_correct_dnc_without_prior_correct_holds_for_review():
    for origins in ({}, {OTHER: "UNKNOWN"}, {OTHER: None}, None):
        s = pts.stop_policy({}, {OTHER: _phone(OTHER, "CORRECT_DNC")}, origins)
        assert not s["owner_stop"] and s["review"] == {OTHER} and s["hold"] and OTHER in s["blocked"]


def test_correct_dnc_on_a_confirmed_correct_number_is_owner_stop():
    s = pts.stop_policy({}, {OTHER: _phone(OTHER, "CORRECT_DNC")}, {OTHER: "CORRECT"})
    assert s["owner_stop"] and s["hold"] and not s["review"]


def test_dnc_on_a_confirmed_correct_number_is_owner_stop():
    s = pts.stop_policy({}, {OTHER: _phone(OTHER, "DNC")}, {OTHER: "CORRECT"})
    assert s["owner_stop"] and s["hold"] and not s["review"]


def test_ambiguous_dnc_blocks_number_and_holds_for_review():
    s = pts.stop_policy({}, {OTHER: _phone(OTHER, "DNC")}, {OTHER: "UNKNOWN"})
    assert not s["owner_stop"] and s["review"] == {OTHER} and s["hold"] and OTHER in s["blocked"]


def test_unreadable_logs_treat_dnc_as_ambiguous():
    s = pts.stop_policy({}, {OTHER: _phone(OTHER, "DNC")}, None)
    assert s["hold"] and s["review"] == {OTHER}


def test_owner_opt_out_is_owner_stop():
    assert pts.stop_policy({"opt_out": True}, {}, {})["owner_stop"]


def test_dnc_origins_uses_status_before_latest_dnc_move():
    evs = [_log(OTHER, "UNKNOWN", "CORRECT", "2026-10-01T10:00:00"), _log(OTHER, "CORRECT", "DNC", "2026-10-02T10:00:00")]
    assert pts.dnc_origins(evs) == {OTHER: "CORRECT"}


def test_record_stop_unreadable_logs_holds():
    api = FakeApi({}, fail={"u1"})
    assert pts.record_stop(api, "u1", _det([_phone(PRIMARY), _phone(OTHER, "DNC")]))["hold"]


# ------------------------------------------------------------ capacity

def test_line_capacity_ceiling_is_nine():
    assert pts.line_capacity(0, 0, 0) == 9


def test_line_capacity_counts_sent_running_and_started():
    assert pts.line_capacity(0, 6, 0) == 6        # (20-6-0-2)//2
    assert pts.line_capacity(0, 0, 5) == 4        # (20-0-10-2)//2
    assert pts.line_capacity(7, 0, 0) == 2        # ceiling 9 minus 7 already started
    assert pts.line_capacity(0, 19, 3) == 0       # never negative


# ------------------------------------------------------------ am-start gate

def test_gate_passes_clean_batch_record():
    d = _det()
    assert pts.am_start_gate(d, pts.stop_policy(d["owner"], {PRIMARY: _phone(PRIMARY)}, {}), [], BATCH) is None


def test_gate_refuses_status_missing_tag_and_drips():
    ok = pts.stop_policy({}, {}, {})
    assert "already has status" in pts.am_start_gate(_det(status="prospecting"), ok, [], BATCH)
    assert "missing tag" in pts.am_start_gate(_det(tags=("SMS Line A",)), ok, [], BATCH)
    assert "drip history unreadable" in pts.am_start_gate(_det(), ok, None, BATCH)
    assert "already has drip history" in pts.am_start_gate(_det(), ok, [{"status": "failed"}], BATCH)


def test_gate_wrong_number_stop_on_other_phone_does_not_hold_owner():
    phones = [_phone(PRIMARY), _phone(OTHER, "WRONG_DNC", tags=())]
    d = _det(phones)
    stop = pts.stop_policy(d["owner"], {p["number"]: p for p in phones}, {})
    assert pts.am_start_gate(d, stop, [], BATCH) is None


def test_gate_ambiguous_stop_holds():
    phones = [_phone(PRIMARY), _phone(OTHER, "DNC", tags=())]
    d = _det(phones)
    stop = pts.stop_policy(d["owner"], {p["number"]: p for p in phones}, {OTHER: "UNKNOWN"})
    assert "STOP policy hold" in pts.am_start_gate(d, stop, [], BATCH)


def test_plan_am_start_stops_at_line_capacity_and_holds_unreadable():
    api = FakeApi({"u1": _det(), "u2": _det(), "u3": _det()}, fail={"u3"})
    cap = {"SMS Line A": {"capacity": 1, "sent": 0, "running": 0, "started": 0}}
    ready, held, errors = pts.plan_am_start(api, ["u1", "u2", "u3"], BATCH, cap)
    assert [e["uuid"] for e in ready] == ["u1"]
    assert {e["uuid"]: e["why"][:10] for e in held} == {"u2": "SMS Line A", "u3": "record unr"}
    assert errors


def test_plan_am_start_holds_line_changed_since_friday():
    api = FakeApi({"u1": _det()})
    cap = {"SMS Line A": {"capacity": 9, "sent": 0, "running": 0, "started": 0}}
    ready, held, _ = pts.plan_am_start(api, ["u1"], BATCH, cap, {"u1": "SMS Line B"})
    assert not ready and "line changed" in held[0]["why"]


# ------------------------------------------------------------ drip classification

def _drip(status="running", sent=0, next_utc=None):
    return {"uuid": "h", "status": status, "property": 1,
            "details": {"log": [{"action": "send_sms", "status": "ok"}] * sent, "next_run_at": next_utc,
                        "event_extra_info": {"source": {"title": "P1 Niche - Morning Texts Line A"}}}}


def test_classify_completed_failed_unknown():
    assert pts.classify_drip(_drip("completed", 5), FRI_6PM)["state"] == "completed"
    assert pts.classify_drip(_drip("running", 5, "2026-10-31T14:50:00"), FRI_6PM)["state"] == "completed"
    f = pts.classify_drip(_drip("failed", 0), FRI_6PM)
    assert f["state"] == "failed" and f["needs_review"]
    assert pts.classify_drip(_drip("running", 1, None), FRI_6PM)["state"] == "unknown"


def test_friday_drip_due_tonight_is_delayed_not_removed():
    c = pts.classify_drip(_drip(sent=4, next_utc="2026-10-31T00:30:00"), FRI_6PM)   # Fri 7:30 PM CT
    assert c["state"] == "delayed" and c["after_hours"] and not c["needs_review"]


def test_friday_delayed_with_texts_left_for_weekend_needs_review():
    c = pts.classify_drip(_drip(sent=3, next_utc="2026-10-30T14:50:00"), FRI_6PM)   # overdue, 2 left
    assert c["state"] == "delayed" and c["needs_review"]


def test_friday_next_on_saturday_is_weekend_bound():
    c = pts.classify_drip(_drip(sent=4, next_utc="2026-10-31T14:50:00"), FRI_6PM)   # Sat 9:50 AM CT
    assert c["state"] == "weekend-bound"


def test_later_weekday_spilling_into_weekend_needs_review():
    assert not pts.classify_drip(_drip(sent=2, next_utc="2026-10-28T14:50:00"), TUE_NOON)["needs_review"]
    c = pts.classify_drip(_drip(sent=1, next_utc="2026-10-29T14:50:00"), TUE_NOON)
    assert c["state"] == "later-weekday" and c["needs_review"]


def test_run_friday_dry_run_removes_nothing_live_removes_only_weekend(tmp_path):
    rows = [_drip(sent=4, next_utc="2026-10-31T14:50:00"), _drip(sent=4, next_utc="2026-10-31T00:30:00"),
            _drip("completed", 5)]
    rows[0]["uuid"], rows[1]["uuid"] = "weekend", "tonight"
    with patch.object(pts, "drip_histories", return_value=rows), patch.object(pts, "Sift"), \
            patch.object(pts, "ct_now", return_value=FRI_6PM), patch.object(pts, "STATE_DIR", tmp_path), \
            patch.object(guard, "remove_history", return_value=True) as rm:
        assert guard.run_friday("k", live=False, slack=False) == 0
        assert rm.call_count == 0
        assert guard.run_friday("k", live=True, slack=False) == 0
        assert [c.args[1] for c in rm.call_args_list] == ["weekend"]
    assert json.loads((tmp_path / "friday_drips_2026-10-30.json").read_text())["removed"] == ["weekend"]


def test_run_friday_fails_when_drips_unreadable(tmp_path):
    with patch.object(pts, "drip_histories", side_effect=RuntimeError("HTTP 502")), patch.object(pts, "Sift"), \
            patch.object(pts, "ct_now", return_value=FRI_6PM), patch.object(guard, "remove_history") as rm:
        assert guard.run_friday("k", live=True, slack=False) == 1
        assert rm.call_count == 0


# ------------------------------------------------------------ readiness marker

def _marker(tmp_path, **kw):
    mk = {"ok": True, "batch_date": "2026-11-02", "uuids": ["u1"], "lines": {"u1": "SMS Line A"}, **kw}
    f = tmp_path / "topup_ready_2026-11-02.json"
    f.write_text(json.dumps(mk))
    return str(f)


def test_load_ready_file_problems(tmp_path):
    assert "not found" in pts.load_ready_file(str(tmp_path / "nope.json"), "2026-11-02")[1]
    assert pts.load_ready_file(None, "2026-11-02")[1]
    assert "not 2026-11-02" in pts.load_ready_file(_marker(tmp_path, batch_date="2026-10-26"), "2026-11-02")[1]
    assert "NOT ready" in pts.load_ready_file(_marker(tmp_path, ok=False, problem="x"), "2026-11-02")[1]
    assert "no records" in pts.load_ready_file(_marker(tmp_path, uuids=[]), "2026-11-02")[1]
    assert pts.load_ready_file(_marker(tmp_path), "2026-11-02")[1] is None


def _plan(tmp_path, uuids):
    (tmp_path / "topup_plan_2026-11-02.json").write_text(json.dumps(
        {"eligible": [{"uuid": u, "line": "A", "best_phone": PRIMARY} for u in uuids]}))


def test_validate_ready_and_excluded(tmp_path):
    _plan(tmp_path, ["u1", "u2", "u3"])
    api = FakeApi({"u1": _det(), "u2": _det(tags=("SMS Line A",)), "u3": _det()}, drips={"u3": [{"status": "failed"}]})
    with patch.object(wt, "STATE_DIR", tmp_path):
        mk, text = wt.validate(api, "2026-11-02")
    assert mk["ok"] and mk["uuids"] == ["u1"] and mk["lines"] == {"u1": "SMS Line A"}
    assert "no batch tag" in mk["excluded"]["u2"] and "has drip history" in mk["excluded"]["u3"]
    assert json.loads((tmp_path / "topup_ready_2026-11-02.json").read_text())["ok"]
    assert text.startswith(":white_check_mark:")


def test_validate_fails_closed_on_unreadable_record_or_missing_plan(tmp_path):
    _plan(tmp_path, ["u1", "u2"])
    with patch.object(wt, "STATE_DIR", tmp_path):
        mk, text = wt.validate(FakeApi({"u1": _det()}, fail={"u2"}), "2026-11-02")
        assert not mk["ok"] and "unreadable" in mk["problem"] and "will NOT launch" in text
        (tmp_path / "topup_plan_2026-11-02.json").unlink()
        assert not wt.validate(FakeApi({}), "2026-11-02")[0]["ok"]


def test_validate_excludes_ambiguous_stop(tmp_path):
    _plan(tmp_path, ["u1"])
    api = FakeApi({"u1": _det([_phone(PRIMARY), _phone(OTHER, "DNC", tags=())])}, logs={"u1": []})
    with patch.object(wt, "STATE_DIR", tmp_path):
        mk, _ = wt.validate(api, "2026-11-02")
    assert not mk["ok"] and "STOP hold" in mk["excluded"]["u1"][0]


# ------------------------------------------------------------ drip guard STOP narrowing

def test_guard_keeps_drip_for_wrong_number_stop_only():
    api = FakeApi({"u1": _det([_phone(PRIMARY), _phone(OTHER, "WRONG_DNC", tags=())])})
    with patch.object(pts, "Sift", return_value=api):
        assert guard.phone_stop_reason("k", "u1") is None


def test_guard_removes_for_owner_stop_primary_dnc_or_unreadable():
    api = FakeApi({"u1": _det([_phone(PRIMARY), _phone(OTHER, "CORRECT_DNC", tags=())]),
                   "u2": _det([_phone(PRIMARY, "WRONG_DNC")])}, fail={"u3"})
    with patch.object(pts, "Sift", return_value=api):
        assert guard.phone_stop_reason("k", "u1").startswith("STOP policy")
        assert guard.phone_stop_reason("k", "u2") == "primary phone DNC"
        assert guard.phone_stop_reason("k", "u3").startswith("STOP check unreadable")


# ------------------------------------------------------------ am-start interlocks

def _args(**kw):
    return Namespace(**{"send": True, "force": False, "only_between": None, "batch_date": None,
                        "ready_file": None, "slack": False, **kw})


def test_am_start_live_does_nothing_on_non_monday():
    with patch.object(pts, "ct_now", return_value=TUE_NOON), patch.object(pts, "Sift", side_effect=AssertionError):
        assert pts.run_am_start(_args()) == 0


def test_am_start_live_without_marker_fails_closed(tmp_path):
    api = FakeApi({"u1": _det()})
    with patch.object(pts, "ct_now", return_value=MON_950), patch.object(pts, "_env", return_value="k"), \
            patch.object(pts, "Sift", return_value=api), patch.object(pts, "STATE_DIR", tmp_path):
        assert pts.run_am_start(_args(ready_file=str(tmp_path / "missing.json"))) == 1
    assert api.writes == []


def test_am_start_live_fails_closed_when_capacity_unreadable(tmp_path):
    api = FakeApi({"u1": _det()})
    with patch.object(pts, "ct_now", return_value=MON_950), patch.object(pts, "_env", return_value="k"), \
            patch.object(pts, "Sift", return_value=api), patch.object(pts, "STATE_DIR", tmp_path), \
            patch.object(pts, "load_capacity", side_effect=RuntimeError("HTTP 502")):
        assert pts.run_am_start(_args(ready_file=_marker(tmp_path))) == 1
    assert api.writes == []


def test_am_start_live_starts_only_marker_records_within_capacity(tmp_path):
    api = FakeApi({"u1": _det(), "u2": _det(), "x": _det()})
    mk = _marker(tmp_path, uuids=["u1", "u2"], lines={"u1": "SMS Line A", "u2": "SMS Line A"})
    cap = {t: {"capacity": 0, "sent": 0, "running": 0, "started": 0} for t in pts.SMS_LINE_TAGS}
    cap["SMS Line A"]["capacity"] = 1
    with patch.object(pts, "ct_now", return_value=MON_950), patch.object(pts, "_env", return_value="k"), \
            patch.object(pts, "Sift", return_value=api), patch.object(pts, "STATE_DIR", tmp_path), \
            patch.object(pts, "load_capacity", return_value=cap), patch.object(pts.time, "sleep"):
        assert pts.run_am_start(_args(ready_file=mk)) == 0
    assert api.writes == [("POST", "/api/internal/property/u1/status/", {"status": "prospecting"})]
    state = json.loads((tmp_path / "am_start_2026-11-02.json").read_text())
    assert [r["uuid"] for r in state["results"]] == ["u1"] and state["blocked"][0]["uuid"] == "u2"


def test_load_capacity_counts_running_drips_per_line():
    rows = [{"status": "running", "details": {"event_extra_info": {"source": {"title": "P1 Niche - Morning Texts Line B"}}}}] * 3
    with patch.object(pts, "ct_now", return_value=MON_950), \
            patch.object(pts, "count_sent_by_line", return_value=(Counter({pts.SMS_LINE_NUMBERS["SMS Line B"]: 4}), Counter())), \
            patch.object(pts, "drip_histories", return_value=rows):
        cap = pts.load_capacity(None, Counter())
    assert cap["SMS Line B"] == {"sent": 4, "running": 3, "started": 0, "capacity": 4}   # (20-4-6-2)//2
    assert cap["SMS Line A"]["capacity"] == 9
