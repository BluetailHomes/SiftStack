"""Tests for mo_parcel_lookup.py — MO (Clay County) property lookup."""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from notice_parser import NoticeData
from mo_parcel_lookup import (
    _apply_parcel_to_notice,
    _build_situs_street,
    _extract_surname_candidates,
    _mo_name_query,
    probate_property_lookup_mo,
)


# ── _build_situs_street ──────────────────────────────────────────────────


def test_build_situs_street_basic():
    attrs = {
        "situs_num": "1412", "situs_street_prefx": "NE",
        "situs_st_name": "96TH", "situs_street_sufix": "TER",
    }
    assert _build_situs_street(attrs) == "1412 Ne 96Th Ter"


def test_build_situs_street_no_prefix():
    attrs = {
        "situs_num": "11107", "situs_street_prefx": None,
        "situs_st_name": "LOCUST", "situs_street_sufix": "ST",
    }
    assert _build_situs_street(attrs) == "11107 Locust St"


def test_build_situs_street_all_blank():
    assert _build_situs_street({}) == ""
    attrs = {"situs_num": None, "situs_street_prefx": "", "situs_st_name": None, "situs_street_sufix": None}
    assert _build_situs_street(attrs) == ""


def test_build_situs_street_no_house_number():
    # Missing situs_num entirely -- still joins what's present (mirrors
    # real Clay data shape; downstream Smarty standardization is the
    # actual mailability gate, not this function)
    attrs = {"situs_num": None, "situs_street_prefx": "N", "situs_st_name": "OAK", "situs_street_sufix": "TRFY"}
    assert _build_situs_street(attrs) == "N Oak Trfy"


# ── _extract_surname_candidates ─────────────────────────────────────────


def test_extract_surname_first_last():
    assert _extract_surname_candidates("JOHN SMITH") == ["SMITH", "JOHN"]


def test_extract_surname_with_suffix():
    assert _extract_surname_candidates("JOHN SMITH JR") == ["SMITH", "JOHN"]


def test_extract_surname_single_token():
    assert _extract_surname_candidates("MADONNA") == ["MADONNA"]


def test_extract_surname_empty():
    assert _extract_surname_candidates("") == []


# ── _mo_name_query (mocked HTTP) ─────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _features(*rows):
    """rows: list of dicts with owner, co_owner, situs parts, etc."""
    return {"features": [{"attributes": r} for r in rows]}


def _clay_attrs(owner, co_owner="", situs_num="100", situs_name="MAIN", situs_sfx="ST",
                 situs_city="", situs_zip="", owner_city="KANSAS CITY", owner_zip="64155"):
    return {
        "current_owner": owner, "co_owner": co_owner, "parcel_id": "09919000301000",
        "situs_num": situs_num, "situs_street_prefx": "", "situs_st_name": situs_name,
        "situs_street_sufix": situs_sfx, "situs_city": situs_city, "situs_zip": situs_zip,
        "owner_city": owner_city, "owner_state": "MO", "owner_zip": owner_zip,
    }


def test_mo_name_query_scores_and_sorts():
    payload = _features(
        _clay_attrs("SMITH ROBERT"),
        _clay_attrs("SMITH JOHN A"),
    )
    with patch("mo_parcel_lookup.requests.get", return_value=_FakeResponse(payload)):
        results = _mo_name_query("clay", "SMITH", full_search_name="JOHN SMITH")
    assert len(results) == 2
    assert results[0][1]["owner"] == "SMITH JOHN A"
    assert results[0][0] > results[1][0]


def test_mo_name_query_unknown_county_returns_empty():
    assert _mo_name_query("jackson", "SMITH") == []


def test_mo_name_query_error_response():
    with patch("mo_parcel_lookup.requests.get", return_value=_FakeResponse({"error": {"code": 400}})):
        assert _mo_name_query("clay", "SMITH") == []


def test_mo_name_query_filters_below_min_score():
    payload = _features(_clay_attrs("NGUYEN THI"))
    with patch("mo_parcel_lookup.requests.get", return_value=_FakeResponse(payload)):
        results = _mo_name_query("clay", "NGUYEN", full_search_name="JOHN SMITH")
    assert results == []


def test_mo_name_query_captures_co_owner():
    payload = _features(_clay_attrs("SMITH ABBY N", co_owner="AUFFERT JOSEPH W"))
    with patch("mo_parcel_lookup.requests.get", return_value=_FakeResponse(payload)):
        results = _mo_name_query("clay", "SMITH", full_search_name="ABBY SMITH")
    assert results[0][1]["co_owner"] == "AUFFERT JOSEPH W"


# ── _apply_parcel_to_notice ─────────────────────────────────────────────


def test_apply_parcel_to_notice_sets_fields_with_situs_city_zip():
    notice = NoticeData(county="Clay", notice_type="probate")
    parcel = {
        "owner": "SMITH JOHN A", "co_owner": "", "parcel_id": "09919000301000",
        "situs_street": "100 Main St", "situs_city": "Liberty", "situs_zip": "64068",
        "owner_city": "KANSAS CITY", "owner_zip": "64155",
    }
    applied = _apply_parcel_to_notice(notice, parcel, "clay")
    assert applied is True
    assert notice.address == "100 Main St"
    assert notice.city == "Liberty"
    assert notice.zip == "64068"
    assert notice.state == "MO"
    assert notice.parcel_id == "09919000301000"
    assert notice.tax_owner_name == "SMITH JOHN A"


def test_apply_parcel_to_notice_falls_back_to_owner_city_when_situs_blank():
    # Real Clay data shape: situs_city/situs_zip are typically blank.
    notice = NoticeData(county="Clay", notice_type="probate")
    parcel = {
        "owner": "SMITH LAUREN", "co_owner": "", "parcel_id": "09919000301000",
        "situs_street": "1412 Ne 96Th Ter", "situs_city": "", "situs_zip": "",
        "owner_city": "KANSAS CITY", "owner_zip": "64155",
    }
    applied = _apply_parcel_to_notice(notice, parcel, "clay")
    assert applied is True
    assert notice.address == "1412 Ne 96Th Ter"
    assert notice.city == "KANSAS CITY"  # from owner_city, not a guess
    assert notice.zip == ""  # left blank for Smarty to derive downstream
    assert notice.state == "MO"


def test_apply_parcel_to_notice_falls_back_to_default_city_when_all_blank():
    notice = NoticeData(county="Clay", notice_type="probate")
    parcel = {
        "owner": "SMITH LAUREN", "co_owner": "", "parcel_id": "1",
        "situs_street": "1412 Ne 96Th Ter", "situs_city": "", "situs_zip": "",
        "owner_city": "", "owner_zip": "",
    }
    applied = _apply_parcel_to_notice(notice, parcel, "clay")
    assert applied is True
    assert notice.city == "Kansas City"  # _DEFAULT_CITY fallback


def test_apply_parcel_to_notice_blank_situs_street_leaves_notice_untouched():
    notice = NoticeData(county="Clay", notice_type="probate")
    parcel = {"owner": "SMITH LAUREN", "situs_street": "", "parcel_id": "1"}
    applied = _apply_parcel_to_notice(notice, parcel, "clay")
    assert applied is False
    assert notice.address == ""


# ── probate_property_lookup_mo (end-to-end, mocked HTTP) ────────────────


def test_probate_lookup_tier1_hit():
    notice = NoticeData(
        county="Clay", notice_type="probate",
        decedent_name="John Smith", owner_name="Jane Executor",
    )
    payload = _features(_clay_attrs("SMITH JOHN A"))
    with patch("mo_parcel_lookup.requests.get", return_value=_FakeResponse(payload)), \
         patch("mo_parcel_lookup.time.sleep"):
        probate_property_lookup_mo([notice])
    assert notice.address == "100 Main St"
    assert notice.state == "MO"


def test_probate_lookup_skips_non_mo_county():
    notice = NoticeData(county="Jackson", notice_type="probate", decedent_name="John Smith")
    with patch("mo_parcel_lookup.requests.get") as mock_get:
        probate_property_lookup_mo([notice])
    mock_get.assert_not_called()
    assert notice.address == ""


def test_probate_lookup_skips_notice_with_existing_address():
    notice = NoticeData(
        county="Clay", notice_type="probate",
        decedent_name="John Smith", address="Already Set",
    )
    with patch("mo_parcel_lookup.requests.get") as mock_get:
        probate_property_lookup_mo([notice])
    mock_get.assert_not_called()


def test_probate_lookup_falls_through_to_tier2_executor_via_co_owner():
    notice = NoticeData(
        county="Clay", notice_type="probate",
        decedent_name="John Smith", owner_name="Jane Family",
    )

    def fake_query(county, surname, full_search_name="", min_score=0.4):
        if surname.upper() == "SMITH":
            return []
        if surname.upper() == "FAMILY":
            return [(0.5, {
                "owner": "Family Trust", "co_owner": "SMITH ESTATE",
                "parcel_id": "R-999", "situs_street": "200 Elm St",
                "situs_city": "", "situs_zip": "", "owner_city": "KANSAS CITY", "owner_zip": "64155",
            })]
        return []

    with patch("mo_parcel_lookup._mo_name_query", side_effect=fake_query), \
         patch("mo_parcel_lookup.time.sleep"):
        probate_property_lookup_mo([notice])

    assert notice.address == "200 Elm St"


def test_probate_lookup_falls_through_to_tier3_people_search():
    notice = NoticeData(
        county="Clay", notice_type="probate",
        decedent_name="John Smith", owner_name="",
    )
    with patch("mo_parcel_lookup._mo_name_query", return_value=[]), \
         patch("mo_parcel_lookup.time.sleep"), \
         patch("mo_parcel_lookup._people_search_property_mo", return_value="300 Oak Rd Liberty"):
        probate_property_lookup_mo([notice])
    assert notice.address == "300 Oak Rd Liberty"
    assert notice.city == "Kansas City"


def test_probate_lookup_no_match_leaves_address_blank():
    notice = NoticeData(
        county="Clay", notice_type="probate",
        decedent_name="John Smith", owner_name="",
    )
    with patch("mo_parcel_lookup._mo_name_query", return_value=[]), \
         patch("mo_parcel_lookup.time.sleep"), \
         patch("mo_parcel_lookup._people_search_property_mo", return_value=None):
        probate_property_lookup_mo([notice])
    assert notice.address == ""


def test_probate_lookup_jackson_platte_cass_not_yet_wired():
    # Confirms the scoping decision is reflected in code: only Clay is
    # live. Jackson/Platte/Cass notices should pass through untouched.
    notices = [
        NoticeData(county=c, notice_type="probate", decedent_name="John Smith")
        for c in ("Jackson", "Platte", "Cass")
    ]
    with patch("mo_parcel_lookup.requests.get") as mock_get:
        probate_property_lookup_mo(notices)
    mock_get.assert_not_called()
    assert all(n.address == "" for n in notices)
