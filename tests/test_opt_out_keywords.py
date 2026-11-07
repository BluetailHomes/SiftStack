"""Tests for the FCC 26-67 opt-out keywords in the SMS responder and pm_text_sender."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pm_text_sender as pm  # noqa: E402
import sms_auto_responder as r  # noqa: E402

FCC_SEVEN = ["STOP", "QUIT", "END", "REVOKE", "OPT OUT", "CANCEL", "UNSUBSCRIBE"]


@pytest.mark.parametrize("text", FCC_SEVEN + ["stop.", " Quit! ", "opt-out", "Opt  Out", "revoke", "Remove me"])
def test_keyword_is_stop(text):
    assert r.is_opt_out_keyword(text)
    assert pm.is_stop(text)


@pytest.mark.parametrize("text", ["Stop texting me about my house", "what's the end date", "", "cancel my appointment"])
def test_longer_wording_goes_to_classifier(text):
    assert not r.is_opt_out_keyword(text)


@pytest.mark.parametrize("text", FCC_SEVEN)
def test_classify_skips_the_model(monkeypatch, text):
    def boom(*a, **k):
        raise AssertionError("classifier should not be called for a bare keyword")
    monkeypatch.setattr(r, "_ask", boom)
    out = r.classify("rules", {"record": {}, "thread": [], "inbound_number": "8165550100", "inbound_text": text}, "m")
    assert out["category"] == "stop" and out["reply"] == "" and out["phone_status"] == "DNC"
    assert not out["needs_human"]
