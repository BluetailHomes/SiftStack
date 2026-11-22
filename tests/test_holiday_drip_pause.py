"""Thanksgiving 2026 drip move: only running morning-text drips due Thu 11-26 move to Friday's last text."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import holiday_drip_pause as h  # noqa: E402
import pm_text_sender as pts  # noqa: E402

SEQ_A = h.LINES["A"][1]
SEQ_KC1 = h.LINES["KC1"][1]
NOW = pts.to_ct("2026-11-26T00:00:00")   # Wed 11-25 18:00 CT


def _hist(uuid, seq, next_utc, sent=3, status="running"):
    log = [{"action": "send_sms", "status": "ok"}] * sent
    return {"uuid": uuid, "property": 1, "status": status,
            "details": {"log": log, "next_run_at": next_utc,
                        "event_extra_info": {"source": {"uuid": seq, "title": "x"}}}}


def test_plan_moves_only_thursday_drips():
    rows = [
        _hist("thu", SEQ_A, "2026-11-26T15:55:00Z"),                 # Thu 9:55 CT -> move
        _hist("thu_kc", SEQ_KC1, "2026-11-27T02:00:00Z"),            # Thu 20:00 CT -> move
        _hist("fri", SEQ_A, "2026-11-27T15:55:00Z"),                 # Friday -> leave
        _hist("done", SEQ_A, "2026-11-26T15:55:00Z", status="completed"),
        _hist("other", "not-a-line-seq", "2026-11-26T15:55:00Z"),
        _hist("early", SEQ_A, "2026-11-26T15:55:00Z", sent=1),
        _hist("tonight", SEQ_A, "2026-11-26T01:30:00Z"),             # Wed 19:30 CT -> warn
        _hist("holiday", "hol-seq", "2026-11-27T15:50:00Z", sent=0),
    ]
    p = h.plan(rows, NOW, {"hol-seq"})
    assert [r["history"] for r in p["move"]] == ["thu", "thu_kc"]
    assert [r["line"] for r in p["move"]] == ["A", "KC1"]
    assert [r["history"] for r in p["early"]] == ["early"]
    assert [r["history"] for r in p["later_today"]] == ["tonight"]
    assert p["already"] == 1


def test_send_time_is_friday_950_central():
    assert h.send_at_utc() == dt.datetime(2026, 11, 27, 15, 50, tzinfo=dt.timezone.utc)
    wed_6pm = dt.datetime(2026, 11, 26, 0, 0, tzinfo=dt.timezone.utc)
    assert h.minutes_until_send(wed_6pm) == 15 * 60 + 50 + 24 * 60


def test_titles_keep_line_attribution():
    assert pts._DRIP_LINE_RE.search(h.seq_title("C")).group(1) == "Line C"
    assert not pts._DRIP_LINE_RE.search(h.seq_title("KC1"))


def test_holiday_actions_shape():
    acts = h.holiday_actions("last try", "+18160000000", 100)
    assert acts[0] == {"type": "delay", "order": 0, "details": {"unit": "minutes", "amount": 100}}
    assert acts[1]["details"]["origin_number"] == "+18160000000"
    assert acts[1]["details"]["send_to_all"] is False
