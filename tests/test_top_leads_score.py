"""Top Leads score (points approved by Kaycie 2026-11-12)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from top_leads_score import score  # noqa: E402


def test_stack_equity_nbhd_band():
    d = {"tags": ["Priority 1"], "lists": ["Jackson - Priority 1 - Free & Clear + Vacant - 18.9 doors per deal"],
         "system_lists": ["Tax Delinquent", "Vacant", "Absentee Owner", "Free & Clear"],
         "equity_percent": None, "neighborhood": "Ruskin Heights", "estimate_value": 120000}
    s = score(d)
    # tax 4 + vacant 3 (counted once) + absentee 1 + stack 2 + F&C 3 + nbhd 2 + band 1
    assert s["score"] == 16
    assert s["signals"][0] == "tax_delinquent"


def test_unknown_equity_scores_zero_not_dropped():
    s = score({"tags": [], "system_lists": ["Vacant"], "equity_percent": None})
    assert s["equity_points"] == 0 and s["score"] == 3


def test_low_equity_penalty_and_stale_obit():
    s = score({"tags": ["Obituary", "Obit 6mo+"], "equity_percent": 20})
    assert s["equity_points"] == -2
    assert s["score"] == 2 - 2


def test_foreclosure_counts_once():
    s = score({"tags": ["Notice of Default"], "system_lists": ["Pre-Foreclosure"], "equity_percent": 40})
    assert s["score"] == 5 + 1
