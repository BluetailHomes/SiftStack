"""Drip-stop rules added 2026-10-21: the auto-responder pulls a record from its running drip when
the dripped (primary) number replies at all, or anyone texts STOP, and the drip exit guard
pulls prospecting records whose primary phone is marked Wrong/Dead."""
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


def test_stop_from_any_number_removes_drip():
    ds = _ds(DRIP)
    assert r.stop_drip_if_needed(ds, "p1", {"category": "stop", "phone_status": "DNC"}, inbound_is_primary=False)
    ds.remove_drip.assert_called_once()


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
