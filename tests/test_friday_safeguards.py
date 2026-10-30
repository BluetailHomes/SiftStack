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

    TAGS = [{"uuid": u, "title": t} for u, t in (pts.STOP_REVIEW_TAG, pts.POST_SEQ_HOLD_TAG, ("t-batch", BATCH))]

    def __init__(self, props, drips=None, logs=None, fail=(), tags=None):
        self.props, self.drips, self.logs, self.fail = props, drips or {}, logs or {}, set(fail)
        self.tags = self.TAGS if tags is None else tags
        self.writes = []

    def call(self, path, method="GET", body=None):
        if path.startswith("/api/internal/tag/") and method == "GET":
            if "tags" in self.fail:
                raise RuntimeError("HTTP 502")
            return {"results": self.tags}
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

    def search(self, must):
        """Property search over the fakes: by tag uuid, status, owner flag, or rung list."""
        titles = {t["uuid"]: t["title"] for t in self.tags}
        out = []
        for u, d in self.props.items():
            tags = [t if isinstance(t, str) else t.get("title") for t in d.get("tags") or []]
            o = d.get("owner") or {}
            if "any_tags" in must and "any_lists" not in must and not any(titles.get(x) in tags for x in must["any_tags"]):
                continue
            if "any_property_status" in must and d.get("status") not in must["any_property_status"]:
                continue
            if any(k in must and not o.get(k) for k in ("dnc", "opt_out")):
                continue
            if "any_lists" in must and d.get("_list") not in must["any_lists"]:
                continue
            out.append({"uuid": u})
        return out


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


def _plan(tmp_path, uuids, phones=None):
    (tmp_path / "topup_plan_2026-11-02.json").write_text(json.dumps(
        {"eligible": [{"uuid": u, "line": "A", "best_phone": (phones or {}).get(u, PRIMARY), "owner_uuid": f"o-{u}"}
                      for u in uuids]}))


def _det_n(n, **kw):
    return _det([_phone(n)], primary=n, **kw)


def test_validate_ready_and_excluded(tmp_path):
    nums = {"u1": "8165550011", "u2": "8165550012", "u3": "8165550013", "x": "8165550014"}
    api = FakeApi({"u1": _det_n(nums["u1"]), "u2": _det_n(nums["u2"], tags=(BATCH, "Priority 1")), "u3": _det_n(nums["u3"]),
                   "x": _det_n(nums["x"], tags=("SMS Line A",))},     # no Batch tag: not part of the batch
                  drips={"u3": [{"status": "failed"}]})
    with patch.object(wt, "STATE_DIR", tmp_path):
        mk, text = wt.validate(api, "2026-11-02")
    assert mk["ok"] and mk["uuids"] == ["u1"] and mk["lines"] == {"u1": "SMS Line A"}
    assert "line tags=[]" in mk["excluded"]["u2"] and "has drip history" in mk["excluded"]["u3"]
    assert "x" not in mk["excluded"]
    assert json.loads((tmp_path / "topup_ready_2026-11-02.json").read_text())["ok"]
    assert text.startswith(":white_check_mark:")


def test_validate_fails_closed_on_unreadable_record_or_missing_batch_tag(tmp_path):
    with patch.object(wt, "STATE_DIR", tmp_path):
        mk, text = wt.validate(FakeApi({"u1": _det(), "u2": _det()}, fail={"u2"}), "2026-11-02")
        assert not mk["ok"] and "unreadable" in mk["problem"] and "will NOT launch" in text
        mk = wt.validate(FakeApi({"u1": _det()}, tags=FakeApi.TAGS[:2]), "2026-11-02")[0]
        assert not mk["ok"] and "not found" in mk["problem"]


def test_validate_ignores_a_later_plan_file(tmp_path):
    """Sunday's dry run re-plans and overwrites the plan file; the batch must not change."""
    _plan(tmp_path, ["other"])
    with patch.object(wt, "STATE_DIR", tmp_path):
        mk, _ = wt.validate(FakeApi({"u1": _det()}), "2026-11-02")
    assert mk["ok"] and mk["uuids"] == ["u1"]


def test_validate_excludes_ambiguous_stop(tmp_path):
    api = FakeApi({"u1": _det([_phone(PRIMARY), _phone(OTHER, "DNC", tags=())])}, logs={"u1": []})
    with patch.object(wt, "STATE_DIR", tmp_path):
        mk, _ = wt.validate(api, "2026-11-02")
    assert not mk["ok"] and "STOP hold" in mk["excluded"]["u1"][0]


# ------------------------------------------------------------ P1 exclusion tags

STOP_REVIEW = "STOP – Review"
HOLD = "P1 Hold – Post-Sequence"
ARAM = "52f78fea-445b-4d1e-8a1f-27be5269b5ea"


def test_tag_match_ignores_dash_style_and_case():
    assert pts._tag_key("STOP - Review") == pts._tag_key(STOP_REVIEW) == pts._tag_key("stop — review")
    assert pts._tag_key({"title": HOLD}) == pts._tag_key("P1 Hold - Post-Sequence")


def test_outreach_and_enroll_blocks():
    assert pts.outreach_block(_det()) is None and pts.enroll_block(_det()) is None
    assert "STOP" in pts.outreach_block(_det(tags=(BATCH, "SMS Line A", STOP_REVIEW)))
    held = _det(tags=(BATCH, "SMS Line A", HOLD))
    assert pts.outreach_block(held) is None and "Post-Sequence" in pts.enroll_block(held)
    aram = _det(owner={"uuid": ARAM})
    assert "Aram" in pts.outreach_block(aram) and "Aram" in pts.enroll_block(aram)
    assert pts.enroll_block(_det(tags=(BATCH, "SMS Line A", "Text Only"))) is None   # dialer rule, not a text block


def test_gates_refuse_excluded_records():
    ok = pts.stop_policy({}, {}, {})
    for d in (_det(tags=(BATCH, "SMS Line A", STOP_REVIEW)), _det(tags=(BATCH, "SMS Line A", HOLD)), _det(owner={"uuid": ARAM})):
        assert pts.am_start_gate(d, ok, [], BATCH)
        assert wt.skip_reason(dict(d, assigned_to=None)).startswith("excluded")


def test_pm_text_plan_skips_stop_review_record():
    m = {"uuid": "u1", "street": "1 MAIN ST", "status": "prospecting", "owner_first": "JANE", "tags": [STOP_REVIEW],
         "phones": {PRIMARY: _phone(PRIMARY)}, "stop": pts.stop_policy({}, {PRIMARY: _phone(PRIMARY)}, {}),
         "blocked": pts.outreach_block({"tags": [STOP_REVIEW]}), "sms_out": [], "sms_in": [], "marks": [], "attempts": 1, "attempt_bumps": 1,
         "calls": [{"by": "c", "dir": "outbound", "result": "answered", "to": PRIMARY, "dur": 40,
                    "ct": dt.datetime(2026, 10, 28, 14, 0)}]}
    with patch.object(pts, "final_mark", return_value="NO_ANSWER"):
        to_send, skipped = pts.plan_pm_texts([m], "2026-10-28", "c", set())[:2]
    assert not to_send and "no automated outreach" in skipped[0]["why"]


def test_review_tags_problem_fails_closed():
    assert pts.review_tags_problem(FakeApi({})) is None
    assert "renamed" in pts.review_tags_problem(FakeApi({}, tags=[{"uuid": pts.STOP_REVIEW_TAG[0], "title": "STOP"}]))
    assert "unreadable" in pts.review_tags_problem(FakeApi({}, fail={"tags"}))


def test_validate_excludes_tagged_and_duplicate_records(tmp_path):
    nums = {"u1": "8165550011", "u2": "8165550012", "u3": "8165550013", "u4": "8165550013"}
    api = FakeApi({"u1": _det_n(nums["u1"]), "u2": _det_n(nums["u2"], tags=(BATCH, "SMS Line A", HOLD)),
                   "u3": _det_n(nums["u3"]), "u4": _det_n(nums["u4"])})
    with patch.object(wt, "STATE_DIR", tmp_path):
        mk, _ = wt.validate(api, "2026-11-02")
    assert mk["ok"] and mk["uuids"] == ["u1"]
    assert "Post-Sequence" in " ".join(mk["excluded"]["u2"])
    assert all("batch records" in " ".join(mk["excluded"][u]) for u in ("u3", "u4"))


def test_validate_fails_closed_when_review_tags_unconfirmed(tmp_path):
    with patch.object(wt, "STATE_DIR", tmp_path):
        mk, text = wt.validate(FakeApi({"u1": _det()}, tags=FakeApi.TAGS[2:]), "2026-11-02")
    assert not mk["ok"] and "review tag" in mk["problem"] and "will NOT launch" in text


def test_am_start_live_fails_closed_when_review_tags_unreadable(tmp_path):
    api = FakeApi({"u1": _det()}, fail={"tags"})
    with patch.object(pts, "ct_now", return_value=MON_950), patch.object(pts, "_env", return_value="k"), \
            patch.object(pts, "Sift", return_value=api), patch.object(pts, "STATE_DIR", tmp_path):
        assert pts.run_am_start(_args(ready_file=_marker(tmp_path))) == 1
    assert api.writes == []


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


# ------------------------------------------------------------ finished drips stay out of new batches

def test_finished_drip_records_stay_ineligible(tmp_path):
    """Kyuhong/Julie/Chereena (Batch 2026-10-19): once their drips finish, every blocker but the
    run state stays, so top-up must still refuse them (P1 handoff, 2026-10-27)."""
    d = _det(tags=("Batch 2026-10-19", "SMS Line A"), status="prospecting")
    assert wt.skip_reason(d) == "assigned"
    assert wt.skip_reason(dict(d, assigned_to=None, status=None)) == "batch/line tag"
    nums = {"u1": "8165550011"}
    api = FakeApi({"u1": _det_n(nums["u1"])}, drips={"u1": [{"status": "completed"}]})
    with patch.object(wt, "STATE_DIR", tmp_path):
        mk, _ = wt.validate(api, "2026-11-02")
    assert "u1" not in mk["uuids"] and "has drip history" in mk["excluded"]["u1"]


# ------------------------------------------------------------ suppression and owner-level checks

def test_suppress_reason_equity_sold_entity():
    today = dt.date(2026, 11, 2)
    assert wt.suppress_reason(_det(), today) is None
    assert wt.suppress_reason(dict(_det(), equity_percent=None), today) is None        # unknown equity kept
    assert "equity" in wt.suppress_reason(dict(_det(), equity_percent=12.5), today)
    assert wt.suppress_reason(dict(_det(), equity_percent=30), today) is None
    assert "sold" in wt.suppress_reason(dict(_det(), last_sold="2026-01-15"), today)
    assert wt.suppress_reason(dict(_det(), last_sold="2025-10-01"), today) is None
    assert "Entity Owner" in wt.suppress_reason(_det(tags=(BATCH, "entity owner")), today)
    assert "Classification" in wt.suppress_reason(_det(tags=("Entity Classification Hold",)), today)
    assert "litigator" in wt.suppress_reason(_det([_phone(PRIMARY, tags=("Dial First", "Litigator"))]), today)


def test_validate_excludes_owner_declined_or_active_elsewhere(tmp_path):
    nums = {"u1": "8165550011", "u2": "8165550012", "u3": "8165550013"}
    api = FakeApi({"u1": _det_n(nums["u1"]), "u2": _det_n(nums["u2"], owner={"uuid": "o2"}),
                   "u3": _det_n(nums["u3"]),
                   "d2": _det_n("8165559999", tags=(), status="not_interested", owner={"uuid": "o2"}),   # same owner
                   "d3": _det([_phone("8165559998"), _phone(nums["u3"])], tags=(), status="new_lead",
                              owner={"uuid": "o3x"})})                                                   # same phone
    with patch.object(wt, "STATE_DIR", tmp_path):
        mk, _ = wt.validate(api, "2026-11-02")
    assert mk["ok"] and mk["uuids"] == ["u1"]
    assert "declined (not_interested) on another property" in " ".join(mk["excluded"]["u2"])
    assert "active (new_lead)" in " ".join(mk["excluded"]["u3"])


def _fresh(n, **kw):
    """An unworked P1 rung record: what the top-up may pick."""
    d = _det_n(n, tags=("Priority 1",), **kw)
    d["assigned_to"] = None
    d["owner"].update(type="person", primary_phone=None)
    d["_list"] = "L0"
    return d


class PlanApi(FakeApi):
    def call(self, path, method="GET", body=None):
        if path.startswith("/api/internal/list/"):
            return {"results": [{"uuid": f"L{i}", "title": wt.LIST_PREFIX + r} for i, r in enumerate(wt.RUNGS)]}
        return super().call(path, method, body)


def test_pinned_plan_takes_exactly_the_reviewed_batch(tmp_path):
    props = {u: _fresh(f"81655500{i:02d}", owner={"uuid": f"o-{u}"}) for i, u in enumerate(("u1", "u2", "u3", "u4"))}
    props["x9"] = _det_n(props["u4"]["owner"]["phones"][0]["number"], tags=(), status="not_interested")
    api = PlanApi(props, tags=FakeApi.TAGS + [{"uuid": "t-p1", "title": wt.P1_TAG}])
    p01 = {"uuid": wt.PRESET_01_UUID, "filters": {"must": {"any_lists": ["L0"]}}}
    f = tmp_path / "batch.csv"
    f.write_text("seq,line,property_uuid\n1,B,u3\n2,A,u1\n3,C,u4\n4,D,missing\n")
    with patch.object(wt, "load_presets", return_value=(p01, {"uuid": "p02"})):
        p = wt.plan(api, 45, pinned=wt.load_pinned(str(f)))
    assert [(r["uuid"], r["line"]) for r in p["eligible"]] == [("u3", "B"), ("u1", "A")]
    assert p["pinned"] and not p["mail_only"] and p["index_unreadable"] == 0
    assert any(k.startswith("owner-level") for k in p["skipped"])           # u4 shares a decliner's phone
    assert p["skipped"]["pinned record not in a P1 rung search"] == 1         # 'missing'
