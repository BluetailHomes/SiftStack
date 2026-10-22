"""Legacy-preset follow-up line in the 7:15 caller audit."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pm_text_sender as p

CALLER = "rahaf@bluetailhomes.com"


def _m(uuid, tags=(), calls=0, texts=0, tasks=0):
    return {"uuid": uuid, "tags": list(tags),
            "calls": [{"by": CALLER, "dir": "outbound"}] * calls,
            "sms_out": [{"by": CALLER}] * texts,
            "notes": [], "marks": [], "status_changes": [], "task_events": tasks}


def test_worked_and_task_only():
    models = [_m("a", calls=2), _m("b", tasks=1)]
    legacy = {"01 Finish Out": {"in_preset": {"a", "b", "c"}, "tagged_today": set()}}
    line = p.legacy_preset_lines(models, legacy, CALLER)[0]
    assert "01 Finish Out worked 1 of 3 (calls 2, texts 0), 1 with task completions only" in line
    assert not line.startswith(":warning:")


def test_nothing_worked_warns():
    legacy = {"06 NI Recall": {"in_preset": {"x", "y"}, "tagged_today": set()}}
    line = p.legacy_preset_lines([_m("x", tasks=1)], legacy, CALLER)[0]
    assert line.startswith(":warning:") and "No follow-up on 06 NI Recall" in line


def test_extension_skips_priority1_and_other_preset():
    models = [_m("gone", calls=1), _m("p1", tags=["Priority 1", "Bluetail 1.0"], calls=3),
              _m("in01", texts=1)]
    legacy = {"01 Finish Out": {"in_preset": {"in01"}, "tagged_today": set()},
              "06 NI Recall": {"in_preset": {"z"}, "tagged_today": {"gone", "p1", "in01"}}}
    line = p.legacy_preset_lines(models, legacy, CALLER)[0]
    assert "06 NI Recall worked 1 of 2 (calls 1, texts 0)" in line
    assert "01 Finish Out worked 1 of 1 (calls 0, texts 1)" in line


def test_missing_preset():
    line = p.legacy_preset_lines([], {"01 Finish Out": None}, CALLER)[0]
    assert "01 Finish Out: preset not found" in line
