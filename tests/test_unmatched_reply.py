"""Unmatched replies (added 2026-10-26): a reply from a number on no DataSift record is matched to a
property via our recent texts from the same line; STOPs add that number to the owner as CORRECT_DNC;
anything unplaceable is flagged once to Slack/Google Chat."""
import datetime as dt
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import sms_auto_responder as r  # noqa: E402

TS = "2026-10-26 15:00:00"
LINE = "8162545235"


def _sent(owner, to, msg="Hi, about your house"):
    return {"resource": owner, "payload": {"sms": {"owner": owner, "destination_number": to, "message": msg}}}


def _prop(uuid, street):
    return {"uuid": uuid, "address": {"street": street}}


def _ds(sent, props):
    """sent: list of sent-log entries; props: owner uuid -> property."""
    ds = MagicMock()
    ds.sent_from_line.return_value = sent
    return ds, (lambda _ds, owner, _msg: props.get(owner))


def _reply(text):
    return {"origin_number": "9135550000", "destination_number": LINE, "message": text}


def test_single_candidate_is_used():
    ds, pick = _ds([_sent("o1", "8165551111"), _sent("o1", "8165551111")], {"o1": _prop("p1", "123 Main St")})
    with patch.object(r, "pick_property", side_effect=pick):
        cand, info = r.match_by_line(ds, _reply("who is this"), TS)
    assert cand["prop"]["uuid"] == "p1" and cand["owner"] == "o1" and cand["to"] == "8165551111"
    assert info["line"] == LINE and len(info["candidates"]) == 1


def test_several_candidates_street_named():
    ds, pick = _ds([_sent("o1", "8165551111"), _sent("o2", "8165552222")],
                   {"o1": _prop("p1", "123 Main St"), "o2": _prop("p2", "4410 Wabash Ave")})
    with patch.object(r, "pick_property", side_effect=pick):
        cand, info = r.match_by_line(ds, _reply("Not selling Wabash"), TS)
    assert cand["prop"]["uuid"] == "p2" and len(info["candidates"]) == 2


def test_several_candidates_house_number_named():
    ds, pick = _ds([_sent("o1", "8165551111"), _sent("o2", "8165552222")],
                   {"o1": _prop("p1", "123 Main St"), "o2": _prop("p2", "4410 Wabash Ave")})
    with patch.object(r, "pick_property", side_effect=pick):
        cand, _ = r.match_by_line(ds, _reply("123 is sold"), TS)
    assert cand["prop"]["uuid"] == "p1"


def test_several_candidates_unnamed_is_none():
    ds, pick = _ds([_sent("o1", "8165551111"), _sent("o2", "8165552222")],
                   {"o1": _prop("p1", "123 Main St"), "o2": _prop("p2", "4410 Wabash Ave")})
    with patch.object(r, "pick_property", side_effect=pick):
        cand, info = r.match_by_line(ds, _reply("stop"), TS)
    assert cand is None and len(info["candidates"]) == 2


def test_nothing_sent_is_none():
    ds, pick = _ds([], {})
    with patch.object(r, "pick_property", side_effect=pick):
        cand, info = r.match_by_line(ds, _reply("hello"), TS)
    assert cand is None and info["candidates"] == []


def test_window_is_24h_before_reply():
    ds, pick = _ds([], {})
    with patch.object(r, "pick_property", side_effect=pick):
        r.match_by_line(ds, _reply("hello"), TS)
    line, since, until = ds.sent_from_line.call_args.args
    assert line == LINE and until - since == dt.timedelta(hours=24)


def _owner():
    return {"uuid": "o1", "phones": [{"number": "(816) 555-1234", "status": "UNKNOWN", "type": "MOBILE"}]}


def test_stop_adds_new_number_as_correct_dnc():
    ds = MagicMock()
    ds.set_phone_status.return_value = True
    line = r.add_number_dnc_if_stop(ds, _owner(), "9135550000", {"category": "stop"})
    ds.set_phone_status.assert_called_once_with("o1", {"number": "9135550000"}, "CORRECT_DNC")
    assert "added 9135550000" in line


def test_stop_from_number_already_on_owner_marks_it():
    ds = MagicMock()
    ds.set_phone_status.return_value = True
    r.add_number_dnc_if_stop(ds, _owner(), "8165551234", {"category": "stop"})
    uuid, phone, status = ds.set_phone_status.call_args.args
    assert (uuid, phone["number"], status) == ("o1", "(816) 555-1234", "CORRECT_DNC")


def test_non_stop_adds_nothing():
    ds = MagicMock()
    assert r.add_number_dnc_if_stop(ds, _owner(), "9135550000", {"category": "who_is_this"}) == ""
    ds.set_phone_status.assert_not_called()


def test_owner_without_uuid_asks_for_hand_mark():
    ds = MagicMock()
    assert "by hand" in r.add_number_dnc_if_stop(ds, {"phones": []}, "9135550000", {"category": "stop"})
    ds.set_phone_status.assert_not_called()


def test_failed_add_is_reported():
    ds = MagicMock()
    ds.set_phone_status.side_effect = RuntimeError("boom")
    assert "FAILED" in r.add_number_dnc_if_stop(ds, _owner(), "9135550000", {"category": "stop"})


def _ago(minutes):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _hooks(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://slack.example/x")
    monkeypatch.setenv("GOOGLE_CHAT_WEBHOOK_URL", "https://chat.example/y")
    monkeypatch.delenv("GOOGLE_CHAT_ALL", raising=False)


INFO = {"line": LINE, "candidates": [{"prop": _prop("p1", "123 Main St"), "owner": "o1", "to": "8165551111"},
                                     {"prop": _prop("p2", "4410 Wabash Ave"), "owner": "o2", "to": "8165552222"}]}


def test_flag_recent_reply_posts_to_both(monkeypatch):
    _hooks(monkeypatch)
    with patch.object(r.requests, "post") as post:
        assert r.flag_unmatched(_reply("who is this"), _ago(2), INFO, dry_run=False) is True
    assert [c.args[0] for c in post.call_args_list] == ["https://slack.example/x", "https://chat.example/y"]
    body = str(post.call_args_list[0])
    assert "needs a human" in body and "123 Main St" in body and "4410 Wabash Ave" in body


def test_flag_old_reply_is_skipped(monkeypatch):
    _hooks(monkeypatch)
    with patch.object(r.requests, "post") as post:
        assert r.flag_unmatched(_reply("who is this"), _ago(10), INFO, dry_run=False) is False
    post.assert_not_called()


def test_flag_dry_run_posts_nothing(monkeypatch):
    _hooks(monkeypatch)
    with patch.object(r.requests, "post") as post:
        assert r.flag_unmatched(_reply("who is this"), _ago(1), INFO, dry_run=True) is False
    post.assert_not_called()


def test_flag_stop_reaches_google_chat(monkeypatch):
    _hooks(monkeypatch)
    with patch.object(r.requests, "post") as post:
        assert r.flag_unmatched(_reply("STOP"), _ago(1), {"line": LINE, "candidates": []}, dry_run=False)
    assert [c.args[0] for c in post.call_args_list] == ["https://slack.example/x", "https://chat.example/y"]
    assert "We sent nothing" in str(post.call_args_list[0])
