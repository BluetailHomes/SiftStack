"""Tests for the High-Potential Lead Filter (target ZIPs + equity gate).

Covers config.TARGET_ZIPS / config.HARD_DROP_EQUITY_PERCENT /
config.LOW_EQUITY_FLAG_PERCENT / config.HIGH_EQUITY_OWNERSHIP_YEARS, and
enrichment_pipeline._years_since_sale / _classify_equity_signal /
_filter_high_potential / _write_suppressed_log.

Design being tested (2026-08-09, revised same day after a second review
pass — see enrichment_pipeline.py's module comment for the full
rationale):
  - Two equity thresholds, not one: <20% = hard drop, 20-30% = keep +
    flag "low_equity", 30%+ = normal.
  - Missing/uncomputable equity data is NEVER a drop reason.
  - A property ZIP outside config.TARGET_ZIPS is dropped, but an UNKNOWN
    ZIP (property address not yet resolved) passes through unfiltered.
  - Every suppressed record (ZIP or equity) is logged to a QA CSV, never
    silently deleted.
"""

import csv
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
from notice_parser import NoticeData
from enrichment_pipeline import (
    _classify_equity_signal,
    _filter_high_potential,
    _write_suppressed_log,
    _years_since_sale,
)


# ── _years_since_sale ────────────────────────────────────────────────────


def test_years_since_sale_none_when_blank():
    assert _years_since_sale("") is None


def test_years_since_sale_none_when_unparseable():
    assert _years_since_sale("not-a-date") is None


def test_years_since_sale_computes_correctly():
    twelve_years_ago = (datetime.now() - timedelta(days=365 * 12)).strftime("%Y-%m-%d")
    years = _years_since_sale(twelve_years_ago)
    assert years in (11, 12)  # tolerate day-count rounding


def test_years_since_sale_recent_sale():
    one_year_ago = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    assert _years_since_sale(one_year_ago) == 0


# ── _classify_equity_signal ──────────────────────────────────────────────


def test_classify_confirmed_high_at_30_percent():
    n = NoticeData(equity_percent="30")
    assert _classify_equity_signal(n) == "confirmed_high"


def test_classify_confirmed_high_above_30_percent():
    n = NoticeData(equity_percent="65")
    assert _classify_equity_signal(n) == "confirmed_high"


def test_classify_low_equity_flag_at_20_percent():
    n = NoticeData(equity_percent="20")
    assert _classify_equity_signal(n) == "low_equity_flag"


def test_classify_low_equity_flag_at_25_percent():
    n = NoticeData(equity_percent="25")
    assert _classify_equity_signal(n) == "low_equity_flag"


def test_classify_low_equity_flag_just_under_30():
    n = NoticeData(equity_percent="29.9")
    assert _classify_equity_signal(n) == "low_equity_flag"


def test_classify_low_equity_drop_under_20_percent():
    n = NoticeData(equity_percent="19.9")
    assert _classify_equity_signal(n) == "low_equity_drop"


def test_classify_low_equity_drop_at_zero():
    n = NoticeData(equity_percent="0")
    assert _classify_equity_signal(n) == "low_equity_drop"


def test_classify_unparseable_equity_percent_falls_through():
    # Garbage equity_percent should not crash — falls through to the
    # ownership-length proxy rather than being treated as confirmed.
    n = NoticeData(equity_percent="not-a-number", notice_type="foreclosure")
    assert _classify_equity_signal(n) == "unknown"


def test_classify_likely_high_ownership_old_sale():
    old_date = (datetime.now() - timedelta(days=365 * 15)).strftime("%Y-%m-%d")
    n = NoticeData(mls_last_sold_date=old_date, notice_type="foreclosure")
    assert _classify_equity_signal(n) == "likely_high_ownership"


def test_classify_unknown_recent_sale_no_equity():
    recent_date = (datetime.now() - timedelta(days=365 * 2)).strftime("%Y-%m-%d")
    n = NoticeData(mls_last_sold_date=recent_date, notice_type="foreclosure")
    assert _classify_equity_signal(n) == "unknown"


def test_classify_probate_no_sale_on_record_is_likely_high_ownership():
    # No mls_last_sold_date at all + probate -> long-ownership proxy, not "unknown"
    n = NoticeData(notice_type="probate")
    assert _classify_equity_signal(n) == "likely_high_ownership"


def test_classify_non_probate_no_sale_on_record_is_unknown():
    n = NoticeData(notice_type="foreclosure")
    assert _classify_equity_signal(n) == "unknown"


def test_classify_equity_percent_takes_priority_over_ownership_proxy():
    # Even with a long-held property, a confirmed low equity_percent should
    # still classify off the real number, not the ownership proxy.
    old_date = (datetime.now() - timedelta(days=365 * 20)).strftime("%Y-%m-%d")
    n = NoticeData(equity_percent="10", mls_last_sold_date=old_date, notice_type="probate")
    assert _classify_equity_signal(n) == "low_equity_drop"


# ── _filter_high_potential ───────────────────────────────────────────────


def _make_notice(zip_code="", equity_percent="", notice_type="probate"):
    return NoticeData(zip=zip_code, equity_percent=equity_percent, notice_type=notice_type)


def test_filter_drops_off_target_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    n_in = _make_notice(zip_code="64130")  # Tier 1 in config.TARGET_ZIPS
    n_out = _make_notice(zip_code="99999")  # not in target set
    result = _filter_high_potential(
        [n_in, n_out], target_zips=config.TARGET_ZIPS, hard_drop_equity_percent=20.0
    )
    assert result == [n_in]
    assert n_in.zip_tier == "Tier 1"


def test_filter_passes_through_unknown_zip_unfiltered(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    n_unknown_zip = _make_notice(zip_code="")  # property zip not yet resolved
    result = _filter_high_potential(
        [n_unknown_zip], target_zips=config.TARGET_ZIPS, hard_drop_equity_percent=20.0
    )
    assert result == [n_unknown_zip]
    assert n_unknown_zip.zip_tier == ""


def test_filter_drops_confirmed_low_equity(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    n_low = _make_notice(zip_code="64130", equity_percent="10")
    result = _filter_high_potential(
        [n_low], target_zips=config.TARGET_ZIPS, hard_drop_equity_percent=20.0
    )
    assert result == []


def test_filter_keeps_and_flags_20_to_30_band(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    n_flagged = _make_notice(zip_code="64130", equity_percent="25")
    result = _filter_high_potential(
        [n_flagged], target_zips=config.TARGET_ZIPS, hard_drop_equity_percent=20.0
    )
    assert result == [n_flagged]
    assert n_flagged.equity_signal == "low_equity_flag"


def test_filter_never_drops_for_missing_equity_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    n_unknown = _make_notice(zip_code="64130", equity_percent="")
    result = _filter_high_potential(
        [n_unknown], target_zips=config.TARGET_ZIPS, hard_drop_equity_percent=20.0
    )
    assert result == [n_unknown]
    assert n_unknown.equity_signal in ("unknown", "likely_high_ownership")


def test_filter_skip_zip_filter_flag_disables_zip_drop(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    n_out = _make_notice(zip_code="99999")
    result = _filter_high_potential(
        [n_out], target_zips=config.TARGET_ZIPS, hard_drop_equity_percent=20.0,
        skip_zip_filter=True,
    )
    assert result == [n_out]


def test_filter_skip_equity_filter_flag_disables_equity_drop(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    n_low = _make_notice(zip_code="64130", equity_percent="5")
    result = _filter_high_potential(
        [n_low], target_zips=config.TARGET_ZIPS, hard_drop_equity_percent=20.0,
        skip_equity_filter=True,
    )
    assert result == [n_low]
    assert n_low.equity_signal == "low_equity_drop"  # tag still computed, just not enforced


def test_filter_writes_suppressed_leads_to_qa_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    n_zip_drop = _make_notice(zip_code="99999")
    n_equity_drop = _make_notice(zip_code="64130", equity_percent="5")
    _filter_high_potential(
        [n_zip_drop, n_equity_drop], target_zips=config.TARGET_ZIPS,
        hard_drop_equity_percent=20.0,
    )
    log_path = tmp_path / "suppressed_leads.csv"
    assert log_path.exists()
    with open(log_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    reasons = {r["reason_suppressed"] for r in rows}
    assert "zip_out_of_target" in reasons
    assert any(r.startswith("equity_below_") for r in reasons)


def test_filter_suppressed_log_appends_across_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    n1 = _make_notice(zip_code="99999")
    n2 = _make_notice(zip_code="88888")
    _filter_high_potential([n1], target_zips=config.TARGET_ZIPS, hard_drop_equity_percent=20.0)
    _filter_high_potential([n2], target_zips=config.TARGET_ZIPS, hard_drop_equity_percent=20.0)
    log_path = tmp_path / "suppressed_leads.csv"
    with open(log_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2


def test_write_suppressed_log_noop_on_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    _write_suppressed_log([])
    assert not (tmp_path / "suppressed_leads.csv").exists()


# ── config constants ─────────────────────────────────────────────────────


def test_target_zips_only_contains_tier_1_and_2():
    assert set(config.TARGET_ZIPS.values()) == {"Tier 1", "Tier 2"}


def test_target_zips_covers_all_three_live_scraper_markets():
    # Sample one known zip per market to confirm coverage, not an
    # exhaustive re-list of the source workbook.
    mo_sample = {"64130", "64118", "64152", "64012"}  # Jackson/Clay/Platte/Cass
    ks_sample = {"66061"}  # Johnson
    nm_sample = {"87120", "87124"}  # Bernalillo/Sandoval
    for z in mo_sample | ks_sample | nm_sample:
        assert z in config.TARGET_ZIPS


def test_hard_drop_threshold_below_flag_threshold():
    assert config.HARD_DROP_EQUITY_PERCENT < config.LOW_EQUITY_FLAG_PERCENT


def test_deprecated_alias_matches_flag_threshold():
    assert config.DEFAULT_MIN_EQUITY_PERCENT == config.LOW_EQUITY_FLAG_PERCENT
