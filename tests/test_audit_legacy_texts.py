"""Legacy call -> voicemail -> text rule in the 7:15 caller audit."""
from test_audit_flags import CALLER, T, _audit, _m

KC, OK, NM = "8167889386", "4057641708", "5055201714"
TO = "+14055553333"


def _vm(line, to=TO, at=T(11)):
    return {"ct": at, "dir": "outbound", "by": CALLER, "to": to, "from": line, "result": "answered", "dur": 30}


def _sms(line, to=TO, at=T(11, 2), manual=True):
    return {"ct": at, "from": line, "to": to, "manual": manual, "by": CALLER}


def _lead(street, line, sms=()):
    m = _m(street, phones={TO: {"status": "NO_ANSWER", "tags": []}}, calls=[_vm(line)], tags=["Legacy Recall"])
    m["sms_out"] = list(sms)
    return m


def test_voicemail_without_text_flagged():
    assert "Legacy voicemail with no text after it from the same line: 1 Okc St (3333)" in _audit(_lead("1 Okc St", OK))


def test_voicemail_then_text_same_line_ok():
    out = _audit(_lead("2 Abq St", NM, sms=[_sms(NM)]))
    assert "no text after it" not in out and "different line" not in out


def test_text_from_wrong_line_flagged():
    out = _audit(_lead("3 Kc St", KC, sms=[_sms(OK)]))
    assert "Legacy text sent from a different line than the call: 3 Kc St" in out
    assert "3 Kc St (3333)" in out  # no text from the call's own line either


def test_over_cap_flagged():
    sms = [_sms(OK, to=f"+1405555{i:04d}", at=T(12, i)) for i in range(21)]
    assert "Legacy line over 20 texts today: 1708=21" in _audit(_lead("4 Cap St", OK, sms=sms))


def test_calls_counted_on_all_three_lines():
    out = _audit(*[_lead(f"{i} Line St", line, sms=[_sms(line)]) for i, line in enumerate((KC, OK, NM))])
    assert "Legacy: 3 calls from the legacy lines on 3 records" in out
