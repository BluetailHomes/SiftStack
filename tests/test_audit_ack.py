import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pm_text_sender import is_ack


def test_sign_offs_need_no_reply():
    for t in ["okay", "Ok.", "Thanks!", "\U0001f44d", "ok thank you", "Got it", "k"]:
        assert is_ack(t), t


def test_real_replies_still_count():
    for t in ["Okay, call me tomorrow", "How much?", "yes", "sure", "?", "", "Not selling"]:
        assert not is_ack(t), t
