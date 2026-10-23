"""Tests for the SMS line volume watch (pm_text_sender.line_crossings / count_sent_by_line / run_line_watch)."""
import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pm_text_sender as pts  # noqa: E402

DAY = dt.date(2026, 10, 22)
LINE_A = pts.SMS_LINE_NUMBERS["SMS Line A"]
LINE_B = pts.SMS_LINE_NUMBERS["SMS Line B"]


def _sent(line, utc, event="owner.sms.sent", to="8165559000"):
    return {"event_type": event, "timestamp": utc,
            "payload": {"sms": {"origin_number": line, "destination_number": to}}}


def _api(pages):
    """pages: list of row lists; page i links to page i+1 via next_cursor."""
    api = MagicMock()

    def call(path):
        i = int(path.split("cursor=")[1]) if "cursor=" in path else 0
        r = {"results": pages[i]}
        if i + 1 < len(pages):
            r["next_cursor"] = str(i + 1)
        return r

    api.call.side_effect = call
    return api


# ------------------------------------------------------------ line_crossings

def test_line_crossings():
    assert pts.line_crossings(19, 20) == [20]
    assert pts.line_crossings(18, 26) == [20, 25]
    assert pts.line_crossings(20, 22) == []
    assert pts.line_crossings(5, 19) == []
    assert pts.line_crossings(24, 31) == [25, 30]


# --------------------------------------------------------- count_sent_by_line

def test_counts_only_todays_sends_from_our_lines():
    rows = [
        _sent(LINE_A, "2026-10-22 15:00:00"),            # 10:00 CT, counted
        _sent("+1" + LINE_A, "2026-10-22 20:00:00"),     # 15:00 CT, +1 prefix, counted
        _sent(LINE_B, "2026-10-22 16:00:00"),
        _sent(LINE_A, "2026-10-22 15:30:00", event="owner.sms.received"),
        _sent("8165550000", "2026-10-22 15:30:00"),      # not one of our lines
        _sent(LINE_A, "2026-10-21 15:00:00"),            # yesterday
    ]
    cutoff = dt.datetime(2026, 10, 22, 12, 0)
    total, upto = pts.count_sent_by_line(_api([rows]), DAY, cutoff)
    assert total == {LINE_A: 2, LINE_B: 1}
    assert upto == {LINE_A: 1, LINE_B: 1}


def test_pages_through_cursor_and_stops_at_older_rows():
    p1 = [_sent(LINE_A, "2026-10-22 18:00:00")]
    p2 = [_sent(LINE_A, "2026-10-22 14:00:00"), _sent(LINE_A, "2026-10-21 23:00:00")]
    p3 = [_sent(LINE_A, "2026-10-22 13:00:00")]          # never fetched: p2 already reached yesterday
    api = _api([p1, p2, p3])
    total, _ = pts.count_sent_by_line(api, DAY, dt.datetime(2026, 10, 22, 23, 0))
    assert total[LINE_A] == 2
    assert api.call.call_count == 2


# ------------------------------------------------------------ run_line_watch

def _run(rows, now, monkeypatch, no_post=False, day=None):
    monkeypatch.setenv("REISIFT_API_KEY", "x")
    args = SimpleNamespace(day=day, window_min=5, threshold=pts.LINE_ALERT_AT, no_post=no_post)
    chat, slack = MagicMock(), MagicMock()
    with patch.object(pts, "ct_now", return_value=now), \
         patch.object(pts, "Sift", return_value=_api([rows])), \
         patch.object(pts, "_google_chat", chat), patch.object(pts, "_slack", slack):
        assert pts.run_line_watch(args) == 0
    return chat, slack


def _twenty(last_utc):
    """19 sends at 10:00 CT plus a 20th at `last_utc`."""
    return [_sent(LINE_A, "2026-10-22 15:00:00") for _ in range(19)] + [_sent(LINE_A, last_utc)]


def test_crossing_inside_window_alerts_once(monkeypatch):
    chat, slack = _run(_twenty("2026-10-22 21:28:00"), dt.datetime(2026, 10, 22, 16, 30), monkeypatch)
    msg = f"Line {pts.fmt_phone(LINE_A)} has sent 20 texts today — stop texting from it."
    chat.assert_called_once_with(msg)
    slack.assert_called_once_with(msg)


def test_crossing_before_window_does_not_alert(monkeypatch):
    chat, slack = _run(_twenty("2026-10-22 21:00:00"), dt.datetime(2026, 10, 22, 16, 30), monkeypatch)
    chat.assert_not_called()
    slack.assert_not_called()


def test_no_post_prints_but_does_not_post(monkeypatch, capsys):
    chat, slack = _run(_twenty("2026-10-22 21:28:00"), dt.datetime(2026, 10, 22, 16, 30), monkeypatch,
                       no_post=True)
    chat.assert_not_called()
    slack.assert_not_called()
    assert "ALERT: Line" in capsys.readouterr().out


def test_backtest_day_reports_every_crossing(monkeypatch):
    rows = [_sent(LINE_A, "2026-10-22 15:00:00") for _ in range(26)]
    chat, _ = _run(rows, dt.datetime(2026, 10, 23, 9, 0), monkeypatch, day="2026-10-22")
    assert [c.args[0].split(" has sent ")[1].split(" ")[0] for c in chat.call_args_list] == ["20", "25"]
