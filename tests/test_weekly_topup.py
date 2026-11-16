"""Tests for weekly_topup.report(): the Batch-tag-exists warning shows in dry runs too."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weekly_topup import report  # noqa: E402


def _plan():
    return {"eligible": [{"rung": "X", "line": "SMS Line A", "primary_change": True}],
            "mail_only": [], "rungs_used": [], "rungs_added": [], "rungs_missing": [],
            "pool_by_rung": {"X": 1}, "skipped": {}}


def test_dry_run_warns_when_batch_exists():
    first = report(_plan(), "2026-10-26", 1, batch_exists=True).splitlines()[0]
    assert "Batch 2026-10-26 already exists" in first
    assert "a live run would refuse" in first


def test_live_force_says_overriding():
    first = report(_plan(), "2026-10-26", 1, live=True, batch_exists=True, force=True).splitlines()[0]
    assert "--force is overriding" in first


def test_no_warning_when_batch_missing():
    first = report(_plan(), "2026-10-26", 1).splitlines()[0]
    assert first.startswith("Weekly top-up plan")


def test_report_shows_score_order():
    p = _plan()
    p["eligible"] = [dict(p["eligible"][0], score=16), dict(p["eligible"][0], score=11)]
    assert "batch scores 16 down to 11 (avg 13.5)" in report(p, "2026-10-26", 2)


def test_rungs_include_notice_of_default_lowest_first():
    from weekly_topup import RUNGS
    assert RUNGS[0] == "Absentee + Notice of Default - 1.7 doors per deal"
    assert "Notice of Default - 7.1 doors per deal" in RUNGS


def _index():
    import weekly_topup as w
    idx = w.OwnerIndex.__new__(w.OwnerIndex)
    from collections import defaultdict
    idx.by_owner, idx.by_key, idx.by_phone = defaultdict(set), defaultdict(set), defaultdict(set)
    idx.unreadable = 2
    idx.by_owner["own-1"].add(("declined (not_interested)", "prop-A"))
    idx.by_key["jane doe|123 main"].add(("active (new_lead)", "prop-B"))
    idx.by_phone["8165551234"].add(("tagged 'Do Not Market'", "prop-C"))
    return idx


def test_owner_index_round_trip_blocks_the_same(tmp_path, monkeypatch):
    import weekly_topup as w
    monkeypatch.setattr(w, "STATE_DIR", tmp_path)
    idx = _index()
    idx.save(w.index_path("2026-11-23"))
    back = w.load_owner_index("2026-11-23")
    assert back is not None and back.unreadable == 2
    d = {"owner": {"uuid": "own-1", "phones": [{"number": "8165551234"}]}}
    assert back.block("prop-X", d) == idx.block("prop-X", d)
    assert "declined" in back.block("prop-X", d) and "Do Not Market" in back.block("prop-X", d)
    assert back.block("prop-A", {"owner": {"uuid": "own-1"}}) is None  # its own property never blocks


def test_stale_or_missing_index_is_rebuilt(tmp_path, monkeypatch):
    import datetime as dt
    import weekly_topup as w
    monkeypatch.setattr(w, "STATE_DIR", tmp_path)
    assert w.load_owner_index("2026-11-23") is None
    _index().save(w.index_path("2026-11-23"))
    later = w.ct_now() + w.INDEX_MAX_AGE + dt.timedelta(minutes=1)
    assert w.load_owner_index("2026-11-23", now=later) is None
    assert w.load_owner_index("2026-11-30") is None  # another Monday's batch
    w.index_path("2026-11-23").write_text("{not json", encoding="utf-8")
    assert w.load_owner_index("2026-11-23") is None


def _rec(tags):
    return {"tags": tags, "status": None, "assigned_to": None, "owner": {"type": "person", "first_name": "Ann"},
            "sms_attempts": 0, "predictivecall_attempts": 0}


def test_obituary_blocked_until_dp_cleared():
    from weekly_topup import skip_reason
    deep = "probate/obituary (deep prospecting)"
    assert skip_reason(_rec(["Obituary P1", "Obit 6mo+"])) == deep                          # not researched yet
    assert skip_reason(_rec(["Obit 6mo+", "DP Researched", "DP Review"]))                   # awaiting review
    assert skip_reason(_rec(["Obit 6mo+", "DP Researched", "Deep Prospecting"]))            # heir case
    assert skip_reason(_rec(["Obit 6mo+", "Probate", "DP Researched"])) == deep             # probate still waits
    assert skip_reason(_rec(["Obituary P1", "Obit 6mo+", "DP Researched"])) != deep         # cleared
