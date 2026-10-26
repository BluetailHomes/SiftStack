"""Tests for the inbound call watch (pm_text_sender.group_inbound_calls / call_message / run_call_watch)."""
import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pm_text_sender as pts  # noqa: E402

LINE_A = pts.SMS_LINE_NUMBERS["SMS Line A"]
CALLER = "8168534785"


def _call(utc, event="owner.call.missed", ext="x1", caller=CALLER, line=LINE_A, direction="inbound",
          duration=0, recording=None, first="James", last="Updyke"):
    return {"event_type": event, "timestamp": utc,
            "payload": {"call": {"direction": direction, "origin_number": caller,
                                 "destination_number": line, "external_id": ext,
                                 "duration": duration, "recording": recording},
                        "extra_info": {"owner": {"first_name": first, "last_name": last}}}}


def _api(pages):
    api = MagicMock()

    def call(path):
        i = int(path.split("cursor=")[1]) if "cursor=" in path else 0
        r = {"results": pages[i]}
        if i + 1 < len(pages):
            r["next_cursor"] = str(i + 1)
        return r

    api.call.side_effect = call
    return api


# ---------------------------------------------------------------- line_label

def test_line_label():
    assert pts.line_label(LINE_A) == "SMS Line A"
    assert pts.line_label("+1" + LINE_A) == "SMS Line A"
    assert pts.line_label(pts.CALLBACK_LINE) == "callback line"
    assert pts.line_label("8165550000") == "line"


# ------------------------------------------------------- group_inbound_calls

def test_groups_fanout_by_call_and_merges_names():
    rows = [
        _call("2026-10-26 15:01:00", first="James", last="Updyke"),
        _call("2026-10-26 15:01:00", first="Mary", last="Updyke"),
        _call("2026-10-26 15:01:00", first="James", last="Updyke"),   # same owner twice
    ]
    [g] = pts.group_inbound_calls(rows)
    assert g["caller"] == CALLER and g["line"] == LINE_A
    assert g["names"] == ["James Updyke", "Mary Updyke"]
    assert g["missed"] is True


def test_skips_outbound_internal_and_other_events():
    rows = [
        _call("2026-10-26 15:01:00", direction="outbound", ext="o"),
        _call("2026-10-26 15:01:00", caller=pts.CALLBACK_LINE, ext="i"),   # our own line calling
        _call("2026-10-26 15:01:00", line="8165550000", ext="n"),          # not our line
        {"event_type": "owner.sms.received", "timestamp": "2026-10-26 15:01:00", "payload": {}},
    ]
    assert pts.group_inbound_calls(rows) == []


def test_answered_call_not_missed_and_sorted():
    rows = [_call("2026-10-26 16:00:00", event="owner.call.received", ext="b", duration=95),
            _call("2026-10-26 15:00:00", ext="a")]
    a, b = pts.group_inbound_calls(rows)
    assert a["missed"] is True and b["missed"] is False and b["duration"] == 95


# -------------------------------------------------------------- call_message

def _msg(**kw):
    [g] = pts.group_inbound_calls([_call("2026-10-26 15:01:00", **kw)])
    return pts.call_message(g)


def test_message_outcomes():
    assert _msg().endswith("missed call, no voicemail.")
    assert "left a voicemail. Recording: http://r" in _msg(recording="http://r")
    assert "answered, 95s." in _msg(event="owner.call.received", duration=95)
    m = _msg()
    assert m.startswith(f"Incoming call: {pts.fmt_phone(CALLER)} (James Updyke) called SMS Line A")
    assert "10:01 AM" in m


def test_message_unknown_caller():
    assert "(not in DataSift)" in _msg(first="", last="")


# ---------------------------------------------------------- fetch_events_since

def test_fetch_pages_and_stops_at_cutoff():
    p1 = [_call("2026-10-26 16:00:00")]
    p2 = [_call("2026-10-26 15:30:00"), _call("2026-10-26 14:00:00")]
    p3 = [_call("2026-10-26 15:59:00")]                      # never fetched
    api = _api([p1, p2, p3])
    got = pts.fetch_events_since(api, dt.datetime(2026, 10, 26, 10, 0))   # 10:00 CT = 15:00 UTC
    assert len(got) == 2
    assert api.call.call_count == 2


# ------------------------------------------------------------- run_call_watch

def _run(rows, now, monkeypatch, no_post=False, day=None):
    monkeypatch.setenv("REISIFT_API_KEY", "x")
    args = SimpleNamespace(day=day, window_min=6, no_post=no_post)
    chat, slack = MagicMock(), MagicMock()
    with patch.object(pts, "ct_now", return_value=now), \
         patch.object(pts, "Sift", return_value=_api([rows])), \
         patch.object(pts, "_google_chat", chat), patch.object(pts, "_slack", slack):
        assert pts.run_call_watch(args) == 0
    return chat, slack


NOW = dt.datetime(2026, 10, 26, 10, 5)   # CT


def test_posts_recent_call_only(monkeypatch):
    rows = [_call("2026-10-26 15:02:00", ext="new"), _call("2026-10-26 14:50:00", ext="old")]
    chat, slack = _run(rows, NOW, monkeypatch)
    chat.assert_called_once()
    assert "10:02 AM" in chat.call_args.args[0]
    slack.assert_called_once_with(chat.call_args.args[0])


def test_no_post_prints_only(monkeypatch, capsys):
    chat, slack = _run([_call("2026-10-26 15:02:00")], NOW, monkeypatch, no_post=True)
    chat.assert_not_called()
    slack.assert_not_called()
    assert "CALL: Incoming call" in capsys.readouterr().out
