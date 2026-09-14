"""Tests for slack_notifier.evaluate_volume_anomaly() — the "success but
empty/anomalous" run-detection logic added to close a real monitoring gap:
an Apify Actor run that finishes without raising could previously produce
zero records and never notify Slack at all.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from slack_notifier import evaluate_volume_anomaly


def test_no_history_no_notices_is_not_flagged():
    # First-ever run, or a county with no prior data — nothing to compare
    # against, so a quiet 0-notice run shouldn't be treated as anomalous.
    assert evaluate_volume_anomaly(0, 0, []) is None


def test_raw_found_but_zero_survive_is_flagged():
    # This is the exact shape of the real KS test run: 1 raw notice scraped,
    # 0 survived validation (missing address). Always worth surfacing.
    warning = evaluate_volume_anomaly(0, 1, [])
    assert warning is not None
    assert "0 survived" in warning


def test_normal_volume_not_flagged():
    history = [{"date": "d", "total": t} for t in [5, 6, 4, 5, 7, 6, 5]]
    assert evaluate_volume_anomaly(5, 5, history) is None


def test_anomalously_low_nonzero_volume_is_flagged():
    history = [{"date": "d", "total": t} for t in [10, 12, 9, 11, 10, 13, 10]]
    warning = evaluate_volume_anomaly(1, 1, history)
    assert warning is not None
    assert "Volume anomaly" in warning


def test_zero_raw_and_total_vs_healthy_history_is_flagged():
    history = [{"date": "d", "total": t} for t in [4, 5, 3, 4, 6, 5, 4]]
    warning = evaluate_volume_anomaly(0, 0, history)
    assert warning is not None
    assert "Volume anomaly" in warning
    assert "possible source outage" in warning


def test_sparse_market_history_does_not_cry_wolf():
    # A genuinely low-volume county (avg < 2) shouldn't get flagged just for
    # having another quiet day — avoids alert fatigue on real 0/1-per-day markets.
    history = [{"date": "d", "total": t} for t in [1, 0, 1, 0, 1, 0, 1]]
    assert evaluate_volume_anomaly(0, 0, history) is None


def test_only_trailing_seven_entries_considered():
    # A long-stale high-volume history shouldn't keep flagging today's
    # genuinely-normal-for-now volume forever.
    old_high = [{"date": "d", "total": 50} for _ in range(20)]
    recent_normal = [{"date": "d", "total": t} for t in [3, 4, 3, 4, 3, 4, 3]]
    history = old_high + recent_normal
    assert evaluate_volume_anomaly(3, 3, history) is None


def test_malformed_history_entries_are_ignored():
    history = [{"date": "d"}, {"date": "d", "total": None}, {"date": "d", "total": "oops"}]
    # No usable numeric totals -> treated as no history, not an error.
    assert evaluate_volume_anomaly(0, 0, history) is None
