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
    assert w["advanced"] == {"Offer made / under contract": 0, "New / warm / hot lead": 1, "Callback scheduled": 1}
    assert (w["engaged"], w["engaged_tasked"], w["photos"]) == (2, 1, 1)


def test_weekly_rate_not_judged_below_min_cn(tmp_path, monkeypatch):
    monkeypatch.setattr(k, "COACH_DIR", tmp_path)
    text = k.weekly_lines({"2026-10-26": [model(calls=[call(10, 15, "1")], phones={"1": {"status": "CORRECT"}})]}, R)
    assert "not judged yet (1/100 CN calls)" in text
    assert "gap, coached" in text


def test_status_slugs_case_insensitive_and_new_lead_counts():
    # The account's real slug is "warm lead" (lowercase, space); new_lead was missed entirely before.
    ms = [model("a", status_changes=[{"to": "warm lead"}]), model("b", status_changes=[{"to": "new_lead"}]),
          model("c", status_changes=[{"to": "HOT-LEAD"}]), model("d", status_changes=[{"to": "Under_Contract"}])]
    w = k.weekly_kpis({"2026-10-26": ms}, R)
    assert w["advanced"] == {"Offer made / under contract": 1, "New / warm / hot lead": 3, "Callback scheduled": 0}
    assert k.daily_kpis(ms, R)["qualified"] == 4


def test_weekly_qualified_line_counts_new_and_warm(tmp_path, monkeypatch):
    monkeypatch.setattr(k, "COACH_DIR", tmp_path)
    text = k.weekly_lines({"2026-10-26": [model("a", status_changes=[{"to": "warm lead"}]),
                                          model("b", status_changes=[{"to": "new_lead"}])]}, R)
    assert "Qualified leads (proxy: new/warm/hot lead or better) 2" in text
    assert "gap, coached" not in text


def test_same_call_on_two_records_counted_once():
    # One smrtPhone call is logged on every record of the dialed owner.
    a = model("a", calls=[call(10, 20, "1", dur=130)], marks=[mark("CORRECT", "1")])
    b = model("b", calls=[call(10, 20, "1", dur=130)], phones={"1": {"status": "UNKNOWN"}})
    d = k.daily_kpis([a, b], R)
    assert (d["dials"], d["rows"], d["cn"], d["conv60"]) == (1, 2, 1, 1)
    assert "1 duplicate log rows on other records not counted" in k.daily_lines([a, b], R)
    assert (d["new_numbers"], d["new_marked"]) == (1, 1)     # marked on record a, unmarked copy on b


def test_external_id_preferred_over_from_to_time():
    c1, c2 = call(10, 20, "1"), call(10, 20, "1")
    c1["id"], c2["id"] = "x1", "x2"           # same second, different smrtPhone calls
    assert k.daily_kpis([model(calls=[c1, c2])], R)["dials"] == 2


def test_dials_labelled_provisional():
    text = k.daily_lines([model(calls=[call(10, 20, "1")])], R)
    assert "DataSift-visible dials 1 (provisional, not the smrtPhone total" in text


def test_photos_and_offers_not_tracked_when_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(k, "COACH_DIR", tmp_path)
    text = k.weekly_lines({"2026-10-26": [model()]}, R)
    assert "Photos received: not tracked yet · offers presented: not tracked yet" in text
    text = k.weekly_lines({"2026-10-26": [model(tags_added=[("Photos Received", R)])]}, R)
    assert "Photos received: at least 1 (tag-based; notes not counted)" in text


def test_meaningful_calls_list():
    m = model("u9", calls=[call(11, 5, "8165551234", dur=95, ), call(11, 30, "8165550000", dur=70),
                           call(12, 0, "8165559999", dur=40), call(13, 0, "8165558888", dur=200),
                           call(14, 0, "8165557777", result="no-answer", dur=0)],
              marks=[mark("CORRECT", "8165551234"), mark("NO_ANSWER", "8165550000"), mark("WRONG", "8165558888")])
    m["street"] = "123 Main St"
    m["calls"][0]["id"] = "smrt-42"
    convs = k.conversations([m], R)
    assert [(v["to"], v["mark"], v["id"]) for v in convs] == [("8165551234", "CORRECT", "smrt-42")]
    text = k.daily_lines([m], R)
    assert "Meaningful answered calls (≥60s, not voicemail/wrong/dead): 1" in text
    assert "11:05 AM · 123 Main St · …1234 · 95s · CORRECT · call smrt-42" in text
    assert "http" not in text.split("Meaningful")[1].split("\n")[1]   # no recording URL leaks


def test_unmarked_meaningful_call_is_flagged_with_record_link():
    m = model("u7", calls=[call(11, 5, "8165551234", dur=95)])
    m["street"] = "9 Elm"
    text = k.daily_lines([m], R)
    assert "UNMARKED - mark it" in text
    assert "app.reisift.io/records/properties/u7|record" in text


def test_warning_live_vs_past_day():
    m = model(calls=[call(10, 20, "1")])
    live = k.daily_lines([m], R, today=dt.date(2026, 10, 26))
    assert "may not be visible yet" in live and "re-run for past day" not in live
    retro = k.daily_lines([m], R, today=dt.date(2026, 10, 28))
    assert "re-run for past day 2026-10-26" in retro and "re-running will not fix" in retro
    explicit = k.daily_lines([], R, day=dt.date(2026, 10, 20), today=dt.date(2026, 10, 28))
    assert "re-run for past day 2026-10-20" in explicit


def test_warning_when_records_skipped_or_queries_fail():
    m = model(calls=[call(10, 20, "1")])
    text = k.daily_lines([m], R, stats={"selected": 90, "skipped": 3, "errors": 1}, today=dt.date(2026, 10, 26))
    assert "3 records skipped after API errors/rate limits" in text
    assert "1 source query errors" in text
    clean = k.daily_lines([m], R, stats={"selected": 90, "skipped": 0, "errors": 0}, today=dt.date(2026, 10, 26))
    assert "skipped" not in clean and "query errors" not in clean
