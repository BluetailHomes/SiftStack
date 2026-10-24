"""Tests for tracerfy_skip_tracer.poll_queue_until_stable."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import tracerfy_skip_tracer as tst


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


@pytest.fixture
def fake_queue(monkeypatch):
    """Feed a sequence of responses; the last one repeats once exhausted."""
    monkeypatch.setattr(tst.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def install(responses):
        def fake_get(url, headers=None, timeout=None):
            i = min(calls["n"], len(responses) - 1)
            calls["n"] += 1
            return _Resp(responses[i])
        monkeypatch.setattr(tst.requests, "get", fake_get)
        return calls

    return install


def _recs(n):
    return [{"first_name": f"A{i}", "last_name": "B"} for i in range(n)]


def test_growing_list_waits_for_final_count(fake_queue):
    fake_queue([_recs(31), _recs(90), _recs(187)])
    records = tst.poll_queue_until_stable("q1")
    assert len(records) == 187


def test_needs_three_equal_polls(fake_queue):
    calls = fake_queue([_recs(5), _recs(5), _recs(5)])
    tst.poll_queue_until_stable("q1")
    assert calls["n"] == 3


def test_dict_completed(fake_queue):
    fake_queue([{"status": "pending"}, {"status": "completed", "records": _recs(4)}])
    assert len(tst.poll_queue_until_stable("q1")) == 4


def test_dict_failed_returns_none(fake_queue):
    fake_queue([{"status": "pending"}, {"status": "failed"}])
    assert tst.poll_queue_until_stable("q1") is None


def test_timeout_returns_last_records(fake_queue):
    # Count never settles: 1, 2, 3, ...
    seq = [_recs(i) for i in range(1, 200)]
    fake_queue(seq)
    records = tst.poll_queue_until_stable("q1", interval=5, max_wait=50)
    assert records is not None and len(records) == 10


def test_timeout_with_nothing_returns_none(fake_queue):
    fake_queue([{"status": "pending"}])
    assert tst.poll_queue_until_stable("q1", max_wait=30) is None


def test_no_accept_before_estimated_wait(fake_queue):
    calls = fake_queue([_recs(7)])
    records = tst.poll_queue_until_stable("q1", est_wait=60, interval=5)
    assert len(records) == 7
    assert calls["n"] * 5 >= 60


def test_unknown_estimated_wait_is_ignored(fake_queue):
    calls = fake_queue([_recs(2)])
    tst.poll_queue_until_stable("q1", est_wait="unknown")
    assert calls["n"] == 3
