"""Tests for src/kpi_scorecard.py, on synthetic load_day() models."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import kpi_scorecard as k  # noqa: E402

R = "rahaf@bluetailhomes.com"


def call(h, mi, to, result="answered", dur=90, by=R, direction="outbound"):
    return {"ct": dt.datetime(2026, 10, 26, h, mi), "from": "8160000000", "to": to, "dir": direction,
            "by": by, "dur": dur, "result": result, "rec": None}


def model(uuid="u1", calls=(), marks=(), phones=None, status_changes=(), tags_added=(), open_tasks=()):
    return {"uuid": uuid, "calls": list(calls), "marks": list(marks), "phones": phones or {},
            "status_changes": list(status_changes), "tags_added": list(tags_added), "open_tasks": list(open_tasks)}


def mark(to, number, frm="UNKNOWN"):
    return {"ct": dt.datetime(2026, 10, 26, 12, 0), "phone": number, "frm": frm, "to": to, "by": R}


def test_cn_needs_answered_and_correct_mark():
    m = model(calls=[call(10, 20, "1", dur=130), call(10, 25, "2"), call(10, 30, "1", result="no-answer")],
              marks=[mark("CORRECT", "1"), mark("WRONG", "2")])
    d = k.daily_kpis([m], R)
    assert d["dials"] == 3
    assert d["cn"] == 1
    assert (d["conv60"], d["conv120"]) == (1, 1)


def test_none_duration_and_other_callers_ignored():
    m = model(calls=[call(10, 20, "1", dur=None), call(10, 21, "1", by="kaycie@bluetailhomes.com"),
                     call(10, 22, "1", direction="inbound")],
              phones={"1": {"status": "CORRECT"}})
    d = k.daily_kpis([m], R)
    assert d["dials"] == 1 and d["cn"] == 1 and d["conv60"] == 0


def test_blocks_and_new_number_marking():
    m = model(calls=[call(10, 0, "1"), call(16, 30, "2"), call(16, 31, "3")],
              marks=[mark("NO_ANSWER", "1")], phones={"2": {"status": "UNKNOWN"}, "3": {"status": "CORRECT"}})
    d = k.daily_kpis([m], R)
    assert d["blocks"] == {"10:15": True, "14:00": False, "16:45": True}
    assert (d["new_numbers"], d["new_marked"]) == (2, 1)
    text = k.daily_lines([m], R)
    assert "blocks missed 14:00" in text and "1 new numbers left unmarked" in text


def test_weekly_tiers_count_each_record_once_at_highest():
    m1 = model("a", status_changes=[{"to": "follow_up"}, {"to": "Hot Lead"}],
               calls=[call(10, 15, "1")], open_tasks=[{"id": 1}], tags_added=[("Photos Received", R)])
    m2 = model("b", status_changes=[{"to": "follow_up"}], calls=[call(10, 16, "2")])
    w = k.weekly_kpis({"2026-10-26": [m1], "2026-10-27": [m2]}, R)
    assert w["advanced"] == {"Offer made / under contract": 0, "Hot / qualified": 1, "Callback scheduled": 1}
    assert (w["engaged"], w["engaged_tasked"], w["photos"]) == (2, 1, 1)


def test_weekly_rate_not_judged_below_min_cn(tmp_path, monkeypatch):
    monkeypatch.setattr(k, "COACH_DIR", tmp_path)
    text = k.weekly_lines({"2026-10-26": [model(calls=[call(10, 15, "1")], phones={"1": {"status": "CORRECT"}})]}, R)
    assert "not judged yet (1/100 CN calls)" in text
    assert "gap, coached" in text
