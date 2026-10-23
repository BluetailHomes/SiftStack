"""Tests for the extra-number confirmation texts (pm_text_sender.extra_numbers / plan_extra_texts)."""
import datetime as dt
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pm_text_sender as pts  # noqa: E402

TODAY = dt.date(2026, 10, 22)
LINE_A = pts.SMS_LINE_NUMBERS["SMS Line A"]
PRIMARY = "8165550001"
EXTRA = "8165550003"


def _phone(number, tags=("Dial Third",), type_="MOBILE", status="UNKNOWN"):
    return {"number": number, "tags": list(tags), "type": type_, "status": status}


def _det(phones, primary=PRIMARY, **extra):
    det = {
        "assigned_to": pts.CALLER_USER_UUID,
        "tags": ["SMS Line A", "Priority 1"],
        "address": {"street": "123 MAIN ST"},
        "owner": {"first_name": "JANE", "primary_phone": {"number": primary}, "phones": phones},
    }
    det.update(extra)
    return det


# ------------------------------------------------------------ extra_numbers

def test_never_texted_is_stage_1_and_primary_skipped():
    det = _det([_phone(PRIMARY, tags=("Dial First",)), _phone(EXTRA)])
    send, skip = pts.extra_numbers(det, {}, {}, TODAY)
    assert send == [(EXTRA, 1)]
    assert skip == []


def test_primary_as_bare_string():
    det = _det([_phone(PRIMARY), _phone(EXTRA)], primary=PRIMARY)
    det["owner"]["primary_phone"] = PRIMARY
    send, _ = pts.extra_numbers(det, {}, {}, TODAY)
    assert send == [(EXTRA, 1)]


def test_phone_level_skips():
    det = _det([
        _phone("8165550010", tags=("Dial Fifth",)),
        _phone("8165550011", type_="LANDLINE"),
        _phone("8165550012", status="WRONG"),
        _phone("8165550013"),
    ])
    history = {"8165550013": {"sent": [], "replied": True}}
    send, skip = pts.extra_numbers(det, history, {}, TODAY)
    assert send == []
    assert dict(skip) == {
        "8165550010": "not Dial First-Fourth",
        "8165550011": "type LANDLINE",
        "8165550012": "marked WRONG",
        "8165550013": "replied",
    }


def test_no_answer_mark_still_eligible():
    send, _ = pts.extra_numbers(_det([_phone(EXTRA, status="NO_ANSWER")]), {}, {}, TODAY)
    assert send == [(EXTRA, 1)]


def test_ledger_blocks_same_day_rerun():
    _, skip = pts.extra_numbers(_det([_phone(EXTRA)]), {}, {EXTRA: {"stage": 1}}, TODAY)
    assert skip == [(EXTRA, "already sent a confirmation text")]


def test_follow_up_on_later_day():
    history = {EXTRA: {"sent": [(TODAY - dt.timedelta(days=1), "SMS Line A")], "replied": False}}
    send, _ = pts.extra_numbers(_det([_phone(EXTRA)]), history, {}, TODAY)
    assert send == [(EXTRA, 2)]


def test_no_follow_up_same_day():
    history = {EXTRA: {"sent": [(TODAY, "SMS Line A")], "replied": False}}
    _, skip = pts.extra_numbers(_det([_phone(EXTRA)]), history, {}, TODAY)
    assert skip == [(EXTRA, "first text was today")]


def test_never_a_third_text():
    past = TODAY - dt.timedelta(days=3)
    two = {EXTRA: {"sent": [(past, "SMS Line A"), (past, "SMS Line A")], "replied": False}}
    _, skip = pts.extra_numbers(_det([_phone(EXTRA)]), two, {}, TODAY)
    assert skip == [(EXTRA, "already followed up")]
    one = {EXTRA: {"sent": [(past, "SMS Line A")], "replied": False}}
    _, skip = pts.extra_numbers(_det([_phone(EXTRA)]), one, {EXTRA: {"stage": 2}}, TODAY)
    assert skip == [(EXTRA, "already followed up")]


def test_text_from_other_line_is_not_followed_up():
    history = {EXTRA: {"sent": [(TODAY - dt.timedelta(days=2), None)], "replied": False}}
    _, skip = pts.extra_numbers(_det([_phone(EXTRA)]), history, {}, TODAY)
    assert skip == [(EXTRA, "already has SMS history")]


# --------------------------------------------------------- plan_extra_texts

def _api(dets: dict, logs: dict | None = None):
    api = MagicMock()
    api.search.return_value = [{"uuid": u} for u in dets]

    def call(path):
        u = path.split("/property/")[1].split("/")[0]
        if "/logs/" in path:
            return {"results": (logs or {}).get(u, [])}
        return dets[u]

    api.call.side_effect = call
    return api


def _plan(api, **kw):
    now = dt.datetime(2026, 10, 22, 16, 30)
    with patch.object(pts, "ct_now", return_value=now):
        return pts.plan_extra_texts(api, **kw)


def test_record_with_correct_number_skipped_entirely():
    det = _det([_phone(PRIMARY, status="CORRECT"), _phone(EXTRA)])
    planned, skipped, _ = _plan(_api({"p1": det}))
    assert planned == []
    assert [s["why"] for s in skipped] == ["owner's number already confirmed Correct"]


def test_correct_dnc_also_skips_record():
    det = _det([_phone(PRIMARY), _phone("8165550002", status="CORRECT_DNC"), _phone(EXTRA)])
    planned, skipped, _ = _plan(_api({"p1": det}))
    assert planned == []
    assert skipped[0]["why"] == "owner's number already confirmed Correct"


def test_record_level_skips():
    dets = {
        "a": _det([_phone(EXTRA)], assigned_to="someone-else"),
        "b": _det([_phone(EXTRA)], tags=["SMS Line A", "SMS Line B"]),
        "c": _det([_phone(EXTRA)]),
    }
    dets["c"]["owner"]["dnc"] = True
    _, skipped, _ = _plan(_api(dets))
    assert [s["why"] for s in skipped] == [
        "not assigned to the caller",
        "needs exactly one SMS Line tag (has 2)",
        "owner DNC/opt-out",
    ]


def test_planned_message_and_line():
    planned, _, _ = _plan(_api({"p1": _det([_phone(PRIMARY), _phone(EXTRA)])}))
    assert len(planned) == 1
    p = planned[0]
    assert (p["number"], p["stage"], p["from"]) == (EXTRA, 1, LINE_A)
    assert p["message"] == pts.EXTRA_TEMPLATE.format(first="Jane", street="123 Main St")


def test_follow_up_from_logs():
    logs = {"p1": [{
        "event_type": "owner.sms.sent",
        "timestamp": "2026-10-20 21:00:00",
        "payload": {"sms": {"origin_number": "+1" + LINE_A, "destination_number": "+1" + EXTRA}},
    }]}
    planned, _, _ = _plan(_api({"p1": _det([_phone(EXTRA)])}, logs))
    assert [(p["number"], p["stage"]) for p in planned] == [(EXTRA, 2)]
    assert planned[0]["message"].startswith("Following up")


def test_line_cap_counts_texts_already_sent_today():
    # 24 texts already went out from Line A today on another record; only one more fits.
    sent = [{
        "event_type": "owner.sms.sent",
        "timestamp": "2026-10-22 15:00:00",
        "payload": {"sms": {"origin_number": LINE_A, "destination_number": f"81655590{i:02d}"}},
    } for i in range(24)]
    dets = {"busy": _det([], assigned_to="someone-else"),
            "p1": _det([_phone("8165550003"), _phone("8165550004")])}
    planned, skipped, prior = _plan(_api(dets, {"busy": sent}))
    assert prior["SMS Line A"] == 24
    assert [p["number"] for p in planned] == ["8165550003"]
    assert skipped[-1]["why"] == "SMS Line A hit 25 today"


def test_line_cap_uses_prior_line_counts_when_higher():
    planned, skipped, prior = _plan(_api({"p1": _det([_phone(EXTRA)])}),
                                    prior_line_counts=Counter({"SMS Line A": 25}))
    assert prior["SMS Line A"] == 25
    assert planned == []
    assert skipped[0]["why"] == "SMS Line A hit 25 today"
