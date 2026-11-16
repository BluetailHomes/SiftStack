"""Drip primary swap (2026-11-13): a wrong primary hands the drip to the next Dial First/Second mobile;
a never-texted one gets a full 5-day restart on Monday; nothing changes unless DRIP_SWAP_LIVE is true."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import drip_exit_guard  # noqa: E402
import drip_swap as s  # noqa: E402
import pm_text_sender as pts  # noqa: E402
import sms_auto_responder as r  # noqa: E402

NO_STOP = {"hold": False, "why": "", "blocked": set()}
DRIP = [{"history_uuid": "h1", "name": "P1 Niche - Morning Texts (5 day) - Line B", "status": "running"}]


def _ph(n, tag="Dial First", status=None, typ="MOBILE", dict_tags=False):
    return {"number": n, "type": typ, "status": status,
            "tags": [{"title": tag}] if dict_tags else [tag]}


def _det(primary="8165550001", phones=None, status="prospecting", tags=("SMS Line B", "Priority 1")):
    phones = phones if phones is not None else [
        _ph("8165550001", status="WRONG"), _ph("(816) 555-0002", "Dial Second"), _ph("8165550003", "Dial Third")]
    return {"uuid": "p1", "status": status, "tags": list(tags), "assigned_to": pts.CALLER_USER_UUID,
            "address": {"street": "1 Main St"},
            "owner": {"uuid": "o1", "first_name": "Ann", "primary_phone": primary, "phones": phones}}


def _sent(n):
    return {"event_type": "owner.sms.sent", "payload": {"sms": {"destination_number": f"+1{n}"}}}


# ------------------------------------------------------------ decide
def test_target_is_next_dial_first_second_mobile():
    assert s.swap_target(_det(), {"8165550001"}) == "8165550002"


def test_target_skips_landline_dnc_and_dial_third():
    det = _det(phones=[_ph("8165550001", status="WRONG"), _ph("8165550002", typ="LANDLINE"),
                       _ph("8165550003", "Dial Second", status="DNC"), _ph("8165550004", "Dial Third")])
    assert s.swap_target(det, {"8165550001"}) is None


def test_target_handles_dict_tags():
    det = _det(phones=[_ph("8165550001", status="WRONG"), _ph("8165550002", "Dial Second", dict_tags=True)])
    assert s.swap_target(det, {"8165550001"}) == "8165550002"


def test_decide_swap_when_target_already_texted():
    d = s.decide(_det(), [_sent("8165550002")], NO_STOP)
    assert d["action"] == "swap" and d["target"] == "8165550002"


def test_decide_restart_when_never_texted():
    d = s.decide(_det(), [_sent("8165550001")], NO_STOP)
    assert d["action"] == "restart" and d["target"] == "8165550002"


def test_extra_texts_ledger_counts_as_texted():
    assert s.decide(_det(), [], NO_STOP, ledger={"8165550002": {}})["action"] == "swap"


def test_inbound_reply_counts_as_texted():
    ev = {"event_type": "owner.sms.received", "payload": {"sms": {"origin_number": "+18165550002"}}}
    assert s.decide(_det(), [ev], NO_STOP)["action"] == "swap"


def test_unreadable_logs_never_restart():
    assert s.decide(_det(), None, NO_STOP)["action"] == "swap"


def test_decide_remove_when_no_target():
    det = _det(phones=[_ph("8165550001", status="WRONG"), _ph("8165550003", "Dial Third")])
    assert s.decide(det, [], NO_STOP)["action"] == "remove"


def test_decide_remove_on_stop_hold():
    assert s.decide(_det(), [], {"hold": True, "why": "opted out", "blocked": set()})["action"] == "remove"


def test_blocked_number_not_a_target():
    assert s.decide(_det(), [], {**NO_STOP, "blocked": {"8165550002"}})["action"] == "remove"


def test_wrong_number_excluded_before_mark_shows():
    det = _det(phones=[_ph("8165550001"), _ph("8165550002", "Dial Second")])  # primary not yet marked
    assert s.decide(det, [_sent("8165550002")], NO_STOP, wrong="8165550001")["target"] == "8165550002"


def test_primary_problem():
    assert s.primary_problem(_det()) == "wrong"
    assert s.primary_problem(_det(primary="")) == "cleared"
    assert s.primary_problem(_det(phones=[_ph("8165550001")])) is None


# ------------------------------------------------------------ apply
def _api(primary_after):
    api = MagicMock()

    def call(path, method="GET", body=None, override=False):
        if path == "/api/internal/property/p1/" and method == "GET":
            return {"owner": {"primary_phone": primary_after}}
        if path.startswith("/api/internal/tag/?"):
            return {"results": []}
        return {}
    api.call.side_effect = call
    return api


def _calls(api):
    return [(c.args[0], c.args[1] if len(c.args) > 1 else "GET", c.args[2] if len(c.args) > 2 else None)
            for c in api.call.call_args_list]


def test_apply_swap_keeps_drip():
    api = _api("8165550002")
    with patch.object(pts, "_slack"):
        res = s.apply(api, "p1", _det(), DRIP, {"action": "swap", "target": "8165550002", "why": ""}, "8165550001", "was marked Wrong")
    calls = _calls(api)
    assert ("/api/internal/owner/o1/", "PATCH", {"primary_phone": "8165550002"}) in calls
    assert not any(m == "DELETE" for _, m, _ in calls)
    assert res["ok"] and "...0002" in res["line"]


def test_apply_restart_removes_drip_and_tags():
    api = _api("8165550002")
    with patch.object(pts, "_slack"):
        res = s.apply(api, "p1", _det(), DRIP, {"action": "restart", "target": "8165550002", "why": ""}, "8165550001", "x")
    calls = _calls(api)
    assert ("/api/internal/drip-campaign-history/h1/", "DELETE", None) in calls
    assert ("/api/internal/tag/", "POST", {"title": s.RESTART_TAG}) in calls
    assert ("/api/internal/property/p1/", "PATCH", {"tags": [s.RESTART_TAG]}) in calls
    assert res["ok"] and "Monday" in res["line"]


def test_apply_flags_primary_that_did_not_stick():
    api = _api("")
    with patch.object(pts, "_slack") as sl:
        res = s.apply(api, "p1", _det(), DRIP, {"action": "swap", "target": "8165550002", "why": ""}, "8165550001", "x")
    assert not res["ok"] and "PROBLEM" in res["line"] and ":warning:" in sl.call_args.args[0]


# ------------------------------------------------------------ restart gate (Monday)
def test_restart_gate_ok():
    det = _det(primary="8165550002")
    assert s.restart_gate(det, NO_STOP, [{"status": "removed"}]) is None


def test_restart_gate_holds_running_drip_and_moved_status():
    det = _det(primary="8165550002")
    assert "running" in s.restart_gate(det, NO_STOP, [{"status": "running"}])
    assert s.restart_gate(_det(primary="8165550002", status="new_lead"), NO_STOP, []).startswith(s.MOVED_ON)


def test_restart_gate_holds_wrong_primary():
    assert "WRONG" in s.restart_gate(_det(), NO_STOP, [])


# ------------------------------------------------------------ responder
def _ds():
    ds = MagicMock()
    ds.running_drips.return_value = DRIP
    ds.remove_drip.return_value = True
    ds.set_phone_status.return_value = True
    ds.key = "k"
    return ds


def test_responder_not_live_still_removes():
    ds = _ds()
    with patch.object(s, "swap_live", return_value=False):
        line = r.stop_drip_if_needed(ds, "p1", {"category": "wrong_number"}, True, _det()["owner"], "8165550001")
    ds.remove_drip.assert_called_once_with("h1")
    assert "removed" in line


def test_responder_live_wrong_number_swaps():
    ds = _ds()
    own = _det(phones=[_ph("8165550001"), _ph("8165550002", "Dial Second")])["owner"]
    with patch.object(s, "swap_live", return_value=True), \
         patch.object(s, "handle", return_value={"action": "swap", "line": "Drip: primary moved to ...0002", "ok": True}) as h:
        line = r.stop_drip_if_needed(ds, "p1", {"category": "wrong_number"}, True, own, "8165550001")
    ds.set_phone_status.assert_called_once()
    assert ds.set_phone_status.call_args.args[2] == "WRONG"
    ds.remove_drip.assert_not_called()
    assert h.call_args.kwargs["wrong"] == "8165550001" and h.call_args.kwargs["note"] is False
    assert "-> WRONG" in line and "...0002" in line


def test_responder_live_other_category_still_removes():
    ds = _ds()
    with patch.object(s, "swap_live", return_value=True), patch.object(s, "handle") as h:
        r.stop_drip_if_needed(ds, "p1", {"category": "interested"}, True, _det()["owner"], "8165550001")
    h.assert_not_called()
    ds.remove_drip.assert_called_once_with("h1")


def test_responder_live_swap_error_falls_back_to_removal():
    ds = _ds()
    with patch.object(s, "swap_live", return_value=True), patch.object(s, "handle", side_effect=RuntimeError("boom")):
        line = r.stop_drip_if_needed(ds, "p1", {"category": "not_owner"}, True, _det()["owner"], "8165550001")
    ds.remove_drip.assert_called_once_with("h1")
    assert "removed" in line


# ------------------------------------------------------------ guard
def _guard(swap_on, dry_run=False, handle_ret=None):
    with patch.object(drip_exit_guard, "tag_uuids", return_value=["t"]), \
         patch.object(drip_exit_guard, "search_records", side_effect=lambda k, m: [{"uuid": "p1", "status": "prospecting"}]
                      if "any_phone_status" in m and "WRONG" in m["any_phone_status"] else []), \
         patch.object(drip_exit_guard, "_get", return_value=_det(primary="")), \
         patch.object(drip_exit_guard, "running_drips", return_value=DRIP), \
         patch.object(drip_exit_guard, "remove_history", return_value=True) as rm, \
         patch.object(drip_exit_guard.time, "sleep"), \
         patch.object(s, "swap_live", return_value=swap_on), \
         patch.object(s, "preview", return_value={"action": "restart", "target": "8165550002", "why": "w"}), \
         patch.object(s, "handle", return_value=handle_ret or {"action": "restart", "ok": True, "line": ""}) as h:
        stats = drip_exit_guard.run("k", ["Priority 1"], dry_run)
    return stats, rm, h


def test_guard_cleared_primary_ignored_when_not_live():
    stats, rm, h = _guard(False)
    assert stats["candidates"] == 0
    rm.assert_not_called(); h.assert_not_called()


def test_guard_cleared_primary_restarts_when_live():
    stats, rm, h = _guard(True)
    h.assert_called_once()
    rm.assert_not_called()
    assert stats["restarts"] == 1 and stats["removed"] == 0


def test_guard_live_dry_run_writes_nothing():
    stats, rm, h = _guard(True, dry_run=True)
    h.assert_not_called(); rm.assert_not_called()
    assert stats["restarts"] == 1


def test_guard_wrong_primary_not_live_removes_as_before():
    with patch.object(drip_exit_guard, "tag_uuids", return_value=["t"]), \
         patch.object(drip_exit_guard, "search_records", side_effect=lambda k, m: [{"uuid": "p1", "status": "prospecting"}]
                      if "any_phone_status" in m and "WRONG" in m["any_phone_status"] else []), \
         patch.object(drip_exit_guard, "_get", return_value=_det()), \
         patch.object(drip_exit_guard, "running_drips", return_value=DRIP), \
         patch.object(drip_exit_guard, "remove_history", return_value=True) as rm, \
         patch.object(drip_exit_guard.time, "sleep"), \
         patch.object(s, "swap_live", return_value=False), \
         patch.object(s, "preview", return_value={"action": "swap", "target": "8165550002", "why": "w"}), \
         patch.object(s, "handle") as h, patch.object(pts, "Sift"):
        stats = drip_exit_guard.run("k", ["Priority 1"], False)
    rm.assert_called_once_with("k", "h1")
    h.assert_not_called()
    assert stats["removed"] == 1
