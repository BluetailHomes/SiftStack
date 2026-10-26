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
