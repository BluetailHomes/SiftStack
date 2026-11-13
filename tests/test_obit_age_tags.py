"""Tests for the Obit age-tag stamper: bucket boundaries, the add/remove diff, and that bulk tag
calls always scope to the given uuids."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import obit_age_tags as oat  # noqa: E402

TODAY = dt.date(2026, 11, 13)


def _ago(days):
    return (TODAY - dt.timedelta(days=days)).isoformat()


def test_bucket_boundaries():
    assert oat.bucket(None, TODAY) is None
    assert oat.bucket(TODAY - dt.timedelta(days=89), TODAY) == oat.UNDER_3
    assert oat.bucket(TODAY - dt.timedelta(days=90), TODAY) == oat.MID
    assert oat.bucket(TODAY - dt.timedelta(days=182), TODAY) == oat.MID
    assert oat.bucket(TODAY - dt.timedelta(days=183), TODAY) == oat.OVER_6


def test_change_adds_missing_tag_on_pulled_record():
    d = {"last_obituary_date": _ago(120), "tags": ["Obituary P1"]}
    assert oat.change(d, TODAY) == (oat.MID, [])


def test_change_moves_a_stale_stamp():
    d = {"last_obituary_date": _ago(209), "tags": [{"title": "Obit 3-6mo"}, "Obituary"]}
    assert oat.change(d, TODAY) == (oat.OVER_6, ["Obit 3-6mo"])


def test_change_noop_when_correct_and_cleans_extras():
    assert oat.change({"last_obituary_date": _ago(250), "tags": ["Obit 6mo+"]}, TODAY) == (None, [])
    assert oat.change({"last_obituary_date": _ago(250), "tags": ["Obit 6mo+", "Obit under 3mo"]}, TODAY) == (None, ["Obit under 3mo"])


def test_no_obituary_date_is_left_alone():
    assert oat.change({"last_obituary_date": None, "tags": ["Obit 3-6mo"]}, TODAY) == (None, [])


class FakeApi:
    def __init__(self):
        self.calls = []

    def call(self, path, method="GET", body=None):
        self.calls.append((path, method, body))
        return {"count": len(body["query"]["must"]["properties"])}


def test_remove_tags_scopes_to_uuids_in_chunks():
    api = FakeApi()
    uuids = [f"u{i}" for i in range(oat.TAG_CHUNK + 3)]
    assert oat.remove_tags(api, uuids, ["Obit 3-6mo"]) == len(uuids)
    assert len(api.calls) == 2
    for path, method, body in api.calls:
        assert path == "/api/internal/property/remove-tags/" and method == "POST"
        assert body["query"]["must"]["properties"] and "properties" not in body
        assert body["tags"] == ["Obit 3-6mo"]
