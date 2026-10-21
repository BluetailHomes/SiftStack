"""Drip-stop rules added 2026-10-21: the auto-responder pulls a record from its running drip when
the dripped (primary) number replies at all (a STOP from another number leaves the drip and only
gets that number marked DNC), STOPs never post to Google Chat, and the drip exit guard pulls
prospecting records whose primary phone is marked Wrong/Dead."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import drip_exit_guard  # noqa: E402
import sms_auto_responder as r  # noqa: E402


def _ds(drips):
    ds = MagicMock()
    ds.running_drips.return_value = drips
    ds.remove_drip.return_value = True
    return ds


DRIP = [{"history_uuid": "h1", "name": "P1 Niche - Morning Texts (5 day) - Line B", "status": "running"}]


def test_not_owner_on_primary_removes_drip():
    ds = _ds(DRIP)
    line = r.stop_drip_if_needed(ds, "p1", {"category": "not_owner", "phone_status": "WRONG"}, inbound_is_primary=True)
    ds.remove_drip.assert_called_once_with("h1")
    assert "removed from P1 Niche" in line and "not_owner" in line


def test_not_owner_on_secondary_number_leaves_drip():
    ds = _ds(DRIP)
    assert r.stop_drip_if_needed(ds, "p1", {"category": "not_owner", "phone_status": "WRONG"}, inbound_is_primary=False) == ""
    ds.remove_drip.assert_not_called()


def test_stop_from_secondary_number_leaves_drip():
    ds = _ds(DRIP)
    assert r.stop_drip_if_needed(ds, "p1", {"category": "stop", "phone_status": "DNC"}, inbound_is_primary=False) == ""
    ds.remove_drip.assert_not_called()


def test_stop_from_primary_removes_drip():
    ds = _ds(DRIP)
    assert r.stop_drip_if_needed(ds, "p1", {"category": "stop", "phone_status": "DNC"}, inbound_is_primary=True)
    ds.remove_drip.assert_called_once_with("h1")


def _owner(status):
    return {"uuid": "o1", "phones": [{"number": "(816) 555-1234", "status": status, "type": "MOBILE",
                                      "tags": [{"title": "Dial First"}]},
                                     {"number": "8165559999", "status": "CORRECT"}]}


@pytest.mark.parametrize("old,new", [("UNKNOWN", "CORRECT_DNC"), (None, "CORRECT_DNC"), ("CORRECT", "CORRECT_DNC"),
                                     ("NO_ANSWER", "CORRECT_DNC"), ("WRONG", "WRONG_DNC"), ("DEAD", "WRONG_DNC")])
def test_stop_marks_only_that_number_dnc(old, new):
    ds = MagicMock()
    ds.set_phone_status.return_value = True
    line = r.mark_number_dnc_if_stop(ds, _owner(old), "8165551234", {"category": "stop"})
    ds.set_phone_status.assert_called_once()
    uuid, phone, status = ds.set_phone_status.call_args.args
    assert (uuid, phone["number"], status) == ("o1", "(816) 555-1234", new)
    assert new in line and "1234" in line


@pytest.mark.parametrize("old", ["CORRECT_DNC", "WRONG_DNC"])
def test_already_dnc_is_skipped(old):
    ds = MagicMock()
    assert r.mark_number_dnc_if_stop(ds, _owner(old), "8165551234", {"category": "stop"}) == ""
    ds.set_phone_status.assert_not_called()


def test_non_stop_does_not_mark_dnc():
    ds = MagicMock()
    assert r.mark_number_dnc_if_stop(ds, _owner("UNKNOWN"), "8165551234", {"category": "wrong_number"}) == ""
    ds.set_phone_status.assert_not_called()


def test_stop_from_unknown_number_asks_for_hand_mark():
    ds = MagicMock()
    assert "by hand" in r.mark_number_dnc_if_stop(ds, _owner("UNKNOWN"), "9135550000", {"category": "stop"})
    ds.set_phone_status.assert_not_called()


def test_failed_dnc_write_is_reported():
    ds = MagicMock()
    ds.set_phone_status.side_effect = RuntimeError("boom")
    assert "FAILED" in r.mark_number_dnc_if_stop(ds, _owner("UNKNOWN"), "8165551234", {"category": "stop"})


def test_set_phone_status_sends_one_phone_with_tag_titles():
    ds = r.DataSift.__new__(r.DataSift)
    ds._req = MagicMock(return_value=MagicMock(status_code=200))
    assert ds.set_phone_status("o1", _owner("UNKNOWN")["phones"][0], "CORRECT_DNC")
    method, url = ds._req.call_args.args
    body = ds._req.call_args.kwargs["json"]
    assert method == "POST" and url.endswith("/owner/o1/upsert-phones/")
    assert body["phones"] == [{"number": "(816) 555-1234", "type": "MOBILE", "tags": ["Dial First"],
                               "status": "CORRECT_DNC", "is_connected": True, "verified": False}]


def test_slack_google_chat_false_skips_google_chat(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://slack.example/x")
    monkeypatch.setenv("GOOGLE_CHAT_WEBHOOK_URL", "https://chat.example/y")
    monkeypatch.setenv("GOOGLE_CHAT_ALL", "true")
    with patch.object(r.requests, "post") as post:
        r.slack("STOP draft", needs_human=True, google_chat=False)
    assert [c.args[0] for c in post.call_args_list] == ["https://slack.example/x"]


def test_slack_needs_human_still_reaches_google_chat(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://slack.example/x")
    monkeypatch.setenv("GOOGLE_CHAT_WEBHOOK_URL", "https://chat.example/y")
    monkeypatch.delenv("GOOGLE_CHAT_ALL", raising=False)
    with patch.object(r.requests, "post") as post:
        r.slack("question", needs_human=True)
    assert [c.args[0] for c in post.call_args_list] == ["https://slack.example/x", "https://chat.example/y"]


@pytest.mark.parametrize("cat", ["who_is_this", "interest", "price_question", "call_me", "unclear"])
def test_any_reply_from_primary_removes_drip(cat):
    ds = _ds(DRIP)
    line = r.stop_drip_if_needed(ds, "p1", {"category": cat, "phone_status": "leave"}, inbound_is_primary=True)
    ds.remove_drip.assert_called_once_with("h1")
    assert cat in line


def test_question_from_secondary_number_leaves_drip():
    ds = _ds(DRIP)
    assert r.stop_drip_if_needed(ds, "p1", {"category": "who_is_this", "phone_status": "leave"}, inbound_is_primary=False) == ""
    ds.running_drips.assert_not_called()


def test_no_running_drip_is_quiet():
    ds = _ds([])
    assert r.stop_drip_if_needed(ds, "p1", {"category": "wrong_number", "phone_status": "WRONG"}, inbound_is_primary=True) == ""


def test_failed_remove_is_reported():
    ds = _ds(DRIP)
    ds.remove_drip.return_value = False
    assert "REMOVE FAILED" in r.stop_drip_if_needed(ds, "p1", {"category": "tenant", "phone_status": "WRONG"}, True)


def _record(primary, phones):
    return {"owner": {"primary_phone": primary, "phones": [{"number": n, "status": s} for n, s in phones]}}


def test_guard_primary_wrong():
    with patch.object(drip_exit_guard, "_get", return_value=_record("8166559658", [("8166559658", "WRONG"), ("8165291831", "UNKNOWN")])):
        assert drip_exit_guard.primary_phone_is_wrong("k", "p") is True


def test_guard_secondary_wrong_primary_fine():
    with patch.object(drip_exit_guard, "_get", return_value=_record("8166559658", [("8166559658", "NO_ANSWER"), ("8165291831", "WRONG")])):
        assert drip_exit_guard.primary_phone_is_wrong("k", "p") is False


def test_guard_no_primary():
    with patch.object(drip_exit_guard, "_get", return_value=_record(None, [("8166559658", "WRONG")])):
        assert drip_exit_guard.primary_phone_is_wrong("k", "p") is False
