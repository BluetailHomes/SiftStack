"""Tests for nm_parcel_lookup.py — NM (Bernalillo/Sandoval) property lookup."""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from notice_parser import NoticeData
from nm_parcel_lookup import (
    _apply_parcel_to_notice,
    _extract_surname_candidates,
    _ose_name_query,
    _parse_situs_address,
    probate_property_lookup_nm,
)


# ── _parse_situs_address ────────────────────────────────────────────────


def test_parse_situs_address_basic():
    result = _parse_situs_address("2941 Madison St Ne  Albuquerque 87110")
    assert result == {"street": "2941 Madison St Ne", "city": "Albuquerque", "zip": "87110"}


def test_parse_situs_address_multiword_city():
    result = _parse_situs_address("5704 Tinnin Rd Nw  Los Ranchos De Albuquerque 87107")
    assert result["street"] == "5704 Tinnin Rd Nw"
    assert result["city"] == "Los Ranchos De Albuquerque"
    assert result["zip"] == "87107"


def test_parse_situs_address_blank():
    assert _parse_situs_address("") is None
    assert _parse_situs_address("   ") is None


def test_parse_situs_address_no_house_number():
    # Bare lot description, no leading house number — not mailable
    assert _parse_situs_address("Tract A  Placitas 87043") is None


def test_parse_situs_address_unparseable():
    assert _parse_situs_address("garbage with no zip") is None


# ── _extract_surname_candidates ─────────────────────────────────────────


def test_extract_surname_first_last():
    assert _extract_surname_candidates("JOHN SMITH") == ["SMITH", "JOHN"]


def test_extract_surname_with_suffix():
    assert _extract_surname_candidates("JOHN SMITH JR") == ["SMITH", "JOHN"]


def test_extract_surname_single_token():
    assert _extract_surname_candidates("MADONNA") == ["MADONNA"]


def test_extract_surname_three_part_name():
    result = _extract_surname_candidates("JOHN ROBERT SMITH")
    assert result == ["SMITH", "JOHN"]


def test_extract_surname_empty():
    assert _extract_surname_candidates("") == []


# ── _ose_name_query (mocked HTTP) ───────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _features(*owners_and_addrs):
    return {
        "features": [
            {"attributes": {"Owner1": owner, "SitusAddressAll": addr,
                             "AccountNumber": "123", "ParcelOID": 1}}
            for owner, addr in owners_and_addrs
        ]
    }


def test_ose_name_query_scores_and_sorts():
    # A surname-only substring search legitimately pulls back other people
    # who share that surname (e.g. "Smith Robert") — same shared-surname
    # noise Knox's own min_score=0.4 threshold already tolerates. What
    # matters is that the better name match ("Smith John A" vs "John
    # Smith") sorts first.
    payload = _features(
        ("Smith Robert", "200 Elm St Nw  Albuquerque 87120"),
        ("Smith John A", "100 Main St Ne  Albuquerque 87110"),
    )
    with patch("nm_parcel_lookup.requests.get", return_value=_FakeResponse(payload)):
        results = _ose_name_query("bernalillo", "SMITH", full_search_name="JOHN SMITH")
    assert len(results) == 2
    assert results[0][1]["owner"] == "Smith John A"
    assert results[0][0] > results[1][0]


def test_ose_name_query_handles_sandoval_comma_format():
    # Sandoval's Owner1 uses "Last, First" — comma must not break scoring
    payload = _features(("Trujillo, Christopher A", "50 Camino Rd  Rio Rancho 87124"))
    with patch("nm_parcel_lookup.requests.get", return_value=_FakeResponse(payload)):
        results = _ose_name_query("sandoval", "TRUJILLO", full_search_name="CHRISTOPHER TRUJILLO")
    assert len(results) == 1
    assert results[0][0] >= 0.4


def test_ose_name_query_unknown_county_returns_empty():
    assert _ose_name_query("valencia", "SMITH") == []


def test_ose_name_query_error_response():
    with patch("nm_parcel_lookup.requests.get", return_value=_FakeResponse({"error": {"code": 400}})):
        assert _ose_name_query("bernalillo", "SMITH") == []


def test_ose_name_query_filters_below_min_score():
    payload = _features(("Nguyen Thi", "1 Zzz Ave  Albuquerque 87101"))
    with patch("nm_parcel_lookup.requests.get", return_value=_FakeResponse(payload)):
        results = _ose_name_query("bernalillo", "NGUYEN", full_search_name="JOHN SMITH")
    assert results == []


# ── _apply_parcel_to_notice ─────────────────────────────────────────────


def test_apply_parcel_to_notice_sets_fields():
    notice = NoticeData(county="Bernalillo", notice_type="probate")
    parcel = {
        "owner": "Smith John A",
        "parcel_address": "100 Main St Ne  Albuquerque 87110",
        "account_number": "R-12345",
    }
    applied = _apply_parcel_to_notice(notice, parcel, "bernalillo")
    assert applied is True
    assert notice.address == "100 Main St Ne"
    assert notice.city == "Albuquerque"
    assert notice.zip == "87110"
    assert notice.state == "NM"
    assert notice.parcel_id == "R-12345"
    assert notice.tax_owner_name == "Smith John A"


def test_apply_parcel_to_notice_blank_situs_leaves_notice_untouched():
    notice = NoticeData(county="Sandoval", notice_type="probate")
    parcel = {"owner": "Larranaga, Rosina R", "parcel_address": "  ", "account_number": "X-1"}
    applied = _apply_parcel_to_notice(notice, parcel, "sandoval")
    assert applied is False
    assert notice.address == ""


# ── probate_property_lookup_nm (end-to-end, mocked HTTP) ────────────────


def test_probate_lookup_tier1_hit():
    notice = NoticeData(
        county="Bernalillo", notice_type="probate",
        decedent_name="John Smith", owner_name="Jane Executor",
    )
    payload = _features(("Smith John A", "100 Main St Ne  Albuquerque 87110"))
    with patch("nm_parcel_lookup.requests.get", return_value=_FakeResponse(payload)), \
         patch("nm_parcel_lookup.time.sleep"):
        probate_property_lookup_nm([notice])
    assert notice.address == "100 Main St Ne"
    assert notice.state == "NM"


def test_probate_lookup_skips_non_nm_county():
    notice = NoticeData(county="Knox", notice_type="probate", decedent_name="John Smith")
    with patch("nm_parcel_lookup.requests.get") as mock_get:
        probate_property_lookup_nm([notice])
    mock_get.assert_not_called()
    assert notice.address == ""


def test_probate_lookup_skips_notice_with_existing_address():
    notice = NoticeData(
        county="Bernalillo", notice_type="probate",
        decedent_name="John Smith", address="Already Set",
    )
    with patch("nm_parcel_lookup.requests.get") as mock_get:
        probate_property_lookup_nm([notice])
    mock_get.assert_not_called()


def test_probate_lookup_falls_through_to_tier2_executor(monkeypatch):
    notice = NoticeData(
        county="Bernalillo", notice_type="probate",
        decedent_name="John Smith", owner_name="Jane Family",
    )

    def fake_query(county, surname, full_search_name="", min_score=0.4):
        # Tier 1 (decedent surname "Smith") comes back empty; Tier 2
        # (executor surname "Family") returns a Smith-owned property.
        if surname.upper() == "SMITH":
            return []
        if surname.upper() == "FAMILY":
            return [(0.5, {
                "owner": "Smith Family Trust",
                "parcel_address": "200 Elm St Nw  Albuquerque 87120",
                "account_number": "R-999",
            })]
        return []

    with patch("nm_parcel_lookup._ose_name_query", side_effect=fake_query), \
         patch("nm_parcel_lookup.time.sleep"):
        probate_property_lookup_nm([notice])

    assert notice.address == "200 Elm St Nw"


def test_probate_lookup_falls_through_to_tier3_people_search():
    notice = NoticeData(
        county="Sandoval", notice_type="probate",
        decedent_name="John Smith", owner_name="",
    )
    with patch("nm_parcel_lookup._ose_name_query", return_value=[]), \
         patch("nm_parcel_lookup.time.sleep"), \
         patch("nm_parcel_lookup._people_search_property_nm", return_value="300 Rio Rd Rio Rancho"):
        probate_property_lookup_nm([notice])
    assert notice.address == "300 Rio Rd Rio Rancho"
    assert notice.city == "Rio Rancho"


def test_probate_lookup_no_match_leaves_address_blank():
    notice = NoticeData(
        county="Bernalillo", notice_type="probate",
        decedent_name="John Smith", owner_name="",
    )
    with patch("nm_parcel_lookup._ose_name_query", return_value=[]), \
         patch("nm_parcel_lookup.time.sleep"), \
         patch("nm_parcel_lookup._people_search_property_nm", return_value=None):
        probate_property_lookup_nm([notice])
    assert notice.address == ""
