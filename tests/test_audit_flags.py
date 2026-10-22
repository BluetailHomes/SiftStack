"""Callback and batch-idle flags in the 7:15 caller audit."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pm_text_sender as p

CALLER = "rahaf@bluetailhomes.com"
DAY = "2026-10-21"
T = lambda h, m=0: dt.datetime(2026, 10, 21, h, m)


def _m(street, phones=None, calls=(), sms_in=(), marks=(), tags=(), status="prospecting", assigned=None):
    return {"uuid": street, "street": street, "status": status, "tags": list(tags), "phones": phones or {},
            "calls": list(calls), "sms_out": [], "sms_in": list(sms_in), "marks": list(marks), "notes": [],
            "status_changes": [], "open_tasks": 0, "attempt_bumps": [], "assigned_to": assigned,
            "actions": [], "task_events": 0}


DIAL = {"ct": T(11), "dir": "outbound", "by": CALLER, "to": "+18165550000", "from": sorted(p.CALL_LINES)[0],
        "result": "noanswer", "dur": 0}
BASE = _m("1 Dialed St", phones={"+18165550000": {"status": "NO_ANSWER", "tags": ["Dial First"]}}, calls=[DIAL])


def _audit(*models):
    return p.build_audit([BASE, *models], DAY, CALLER)


def test_stop_text_is_not_a_missed_callback():
    m = _m("7042 Walrond", phones={"+18165551111": {"status": "CORRECT_DNC", "tags": []}},
           sms_in=[{"ct": T(12), "from": "+18165551111", "text": "Stop"}])
    assert "7042 Walrond" not in _audit(m)


def test_responder_dnc_mark_counts_as_handled():
    m = _m("9 Opt St", phones={"+18165551112": {"status": "NO_ANSWER", "tags": []}},
           sms_in=[{"ct": T(12), "from": "+18165551112", "text": "please take me off your list"}],
           marks=[{"ct": T(12, 1), "phone": "+18165551112", "to": "CORRECT_DNC", "by": "kaycie@bluetailrealestate.com"}])
    assert "9 Opt St" not in _audit(m)


def test_real_question_still_flagged():
    m = _m("5 Ask St", phones={"+18165551113": {"status": "UNKNOWN", "tags": []}},
           sms_in=[{"ct": T(12), "from": "+18165551113", "text": "What would you offer?"}])
    assert "5 Ask St (1 inbound, last 12:00)" in _audit(m)


def test_batch_idle_only_with_eligible_number():
    dead = _m("2905 E 52nd", phones={"+18165552222": {"status": "WRONG_DNC", "tags": ["Dial First"]}},
              tags=["Batch 1"], assigned=p.CALLER_USER_UUID)
    live = _m("8 Live St", phones={"+18165552223": {"status": "UNKNOWN", "tags": ["Dial Second"]}},
              tags=["Batch 1"], assigned=p.CALLER_USER_UUID)
    out = _audit(dead, live)
    line = next(l for l in out.splitlines() if "Batch records" in l)
    assert "8 Live St" in line and "2905 E 52nd" not in line
