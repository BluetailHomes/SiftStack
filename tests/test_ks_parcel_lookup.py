"""Tests for ks_parcel_lookup.py -- Johnson County KS property lookup."""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from notice_parser import NoticeData
from ks_parcel_lookup import (
    _ajaxreq_lookup,
    _apply_parcel_to_notice,
    _clean_candidate_name,
    _extract_name_parts,
    _ks_name_search,
    _score_name,
    _validate_string_query,
    probate_property_lookup_ks,
)


# -- _extract_name_parts --------------------------------------------------


def test_extract_name_parts_first_last():
    assert _extract_name_parts("JOHN SMITH") == ("SMITH", "JOHN")


def test_extract_name_parts_with_suffix():
    assert _extract_name_parts("JOHN SMITH JR") == ("SMITH", "JOHN")


def test_extract_name_parts_single_token():
    assert _extract_name_parts("MADONNA") == ("MADONNA", "")


def test_extract_name_parts_three_part_name():
    assert _extract_name_parts("JOHN ROBERT SMITH") == ("SMITH", "JOHN")


def test_extract_name_parts_empty():
    assert _extract_name_parts("") == ("", "")


# -- _clean_candidate_name / _score_name -----------------------------------


def test_clean_candidate_name_strips_pipe_suffix():
    assert _clean_candidate_name("SMITH & LOVELESS, INC. | IP70000000 0011") == "SMITH & LOVELESS, INC."


def test_clean_candidate_name_no_pipe_unchanged():
    assert _clean_candidate_name("MADL, DANIEL E.") == "MADL, DANIEL E."


def test_score_name_strips_commas():
    assert _score_name("MADL, DANIEL E.") == "MADL DANIEL E."


# -- _validate_string_query (mocked HTTP) ----------------------------------


class _FakeJsonResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_validate_string_query_filters_to_owner_type():
    payload = {"d": [
        {"Name": "MADL, DANIEL E.", "Value": "QP53450000 0020", "Type": "Owner"},
        {"Name": "111 S CHERRY ST", "Value": "ADDR1", "Type": "Address"},
        {"Name": "MADL ESTATE LT 1", "Value": "QP38800000 0001", "Type": "Legal Description"},
        {"Name": "Showing first 500 records - please narrow search", "Value": "", "Type": None},
    ]}
    with patch("ks_parcel_lookup.requests.post", return_value=_FakeJsonResponse(payload)):
        results = _validate_string_query("MADL")
    assert results == [{"name": "MADL, DANIEL E.", "value": "QP53450000 0020"}]


def test_validate_string_query_short_prefix_returns_empty_without_request():
    with patch("ks_parcel_lookup.requests.post") as mock_post:
        assert _validate_string_query("M") == []
    mock_post.assert_not_called()


def test_validate_string_query_strips_pipe_suffix_in_results():
    payload = {"d": [
        {"Name": "SMITH & LOVELESS, INC. | IP70000000 0011", "Value": "IP70000000 0011", "Type": "Owner"},
    ]}
    with patch("ks_parcel_lookup.requests.post", return_value=_FakeJsonResponse(payload)):
        results = _validate_string_query("SMITH")
    assert results == [{"name": "SMITH & LOVELESS, INC.", "value": "IP70000000 0011"}]


def test_validate_string_query_empty_payload():
    with patch("ks_parcel_lookup.requests.post", return_value=_FakeJsonResponse({"d": []})):
        assert _validate_string_query("ZZQXVTH") == []


# -- _ajaxreq_lookup (mocked HTTP) -----------------------------------------


class _FakeXmlResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


_SAMPLE_XML = """<Propinfo>
  <Table>
    <SitAddline1>12615 W 52ND ST</SitAddline1>
    <Owner1FullName>MADL, DANIEL E.</Owner1FullName>
    <Owner2FullName>MEHAN, MONICA M.</Owner2FullName>
    <KUPN>0460520303002015000</KUPN>
    <QuickRefID>R162021</QuickRefID>
    <SitusCity>Shawnee</SitusCity>
    <SitusZipCode>66216</SitusZipCode>
  </Table>
</Propinfo>"""


def test_ajaxreq_lookup_parses_fields():
    with patch("ks_parcel_lookup.requests.post", return_value=_FakeXmlResponse(_SAMPLE_XML)):
        parcels = _ajaxreq_lookup("QP53450000 0020")
    assert len(parcels) == 1
    p = parcels[0]
    assert p["owner1"] == "MADL, DANIEL E."
    assert p["owner2"] == "MEHAN, MONICA M."
    assert p["situs_street"] == "12615 W 52ND ST"
    assert p["situs_city"] == "Shawnee"
    assert p["situs_zip"] == "66216"
    assert p["kupn"] == "0460520303002015000"


def test_ajaxreq_lookup_empty_response():
    with patch("ks_parcel_lookup.requests.post", return_value=_FakeXmlResponse("<Propinfo />")):
        assert _ajaxreq_lookup("BOGUS") == []


def test_ajaxreq_lookup_skips_blank_owner():
    xml = """<Propinfo><Table><SitAddline1>1 MAIN ST</SitAddline1><Owner1FullName></Owner1FullName></Table></Propinfo>"""
    with patch("ks_parcel_lookup.requests.post", return_value=_FakeXmlResponse(xml)):
        assert _ajaxreq_lookup("X") == []


def test_ajaxreq_lookup_blank_value_short_circuits():
    with patch("ks_parcel_lookup.requests.post") as mock_post:
        assert _ajaxreq_lookup("   ") == []
    mock_post.assert_not_called()


# -- _ks_name_search (mocked chain) ----------------------------------------


def test_ks_name_search_narrow_query_used_first(monkeypatch):
    calls = []

    def fake_validate(prefix):
        calls.append(prefix)
        if prefix == "MADL, DANIEL":
            return [{"name": "MADL, DANIEL E.", "value": "QP53450000 0020"}]
        return []

    with patch("ks_parcel_lookup._validate_string_query", side_effect=fake_validate), \
         patch("ks_parcel_lookup._ajaxreq_lookup", return_value=[{
             "owner1": "MADL, DANIEL E.", "owner2": "", "situs_street": "12615 W 52ND ST",
             "situs_city": "Shawnee", "situs_zip": "66216", "kupn": "K1", "quick_ref_id": "R1",
         }]), \
         patch("ks_parcel_lookup.time.sleep"):
        results = _ks_name_search("MADL", "DANIEL", full_search_name="Daniel Madl")

    assert calls == ["MADL, DANIEL"]  # bare-surname fallback never triggered
    assert len(results) == 1
    assert results[0][1]["parcel_address"] == "12615 W 52ND ST"


def test_ks_name_search_falls_back_to_bare_surname():
    calls = []

    def fake_validate(prefix):
        calls.append(prefix)
        if prefix == "MADL":
            return [{"name": "MADL, JULIE ANN", "value": "QP1"}]
        return []  # narrow "MADL, JULIE" returns nothing

    with patch("ks_parcel_lookup._validate_string_query", side_effect=fake_validate), \
         patch("ks_parcel_lookup._ajaxreq_lookup", return_value=[{
             "owner1": "MADL, JULIE ANN", "owner2": "", "situs_street": "1 ELM ST",
             "situs_city": "Olathe", "situs_zip": "66061", "kupn": "K2", "quick_ref_id": "R2",
         }]), \
         patch("ks_parcel_lookup.time.sleep"):
        results = _ks_name_search("MADL", "JULIE", full_search_name="Julie Madl")

    assert calls == ["MADL, JULIE", "MADL"]
    assert len(results) == 1


def test_ks_name_search_filters_below_min_score():
    with patch("ks_parcel_lookup._validate_string_query", return_value=[
        {"name": "NGUYEN, THI", "value": "QP9"},
    ]), patch("ks_parcel_lookup._ajaxreq_lookup") as mock_ajax, \
         patch("ks_parcel_lookup.time.sleep"):
        results = _ks_name_search("SMITH", "JOHN", full_search_name="JOHN SMITH")
    assert results == []
    mock_ajax.assert_not_called()


def test_ks_name_search_no_surname_returns_empty():
    with patch("ks_parcel_lookup.requests.post") as mock_post:
        assert _ks_name_search("", "") == []
    mock_post.assert_not_called()


# -- _apply_parcel_to_notice ------------------------------------------------


def test_apply_parcel_to_notice_sets_fields():
    notice = NoticeData(county="Johnson", notice_type="probate")
    parcel = {
        "owner": "MADL, DANIEL E.",
        "parcel_address": "12615 W 52ND ST",
        "situs_city": "Shawnee",
        "situs_zip": "66216",
        "parcel_id": "0460520303002015000",
    }
    applied = _apply_parcel_to_notice(notice, parcel)
    assert applied is True
    assert notice.address == "12615 W 52Nd St"
    assert notice.city == "Shawnee"
    assert notice.zip == "66216"
    assert notice.state == "KS"
    assert notice.parcel_id == "0460520303002015000"
    assert notice.tax_owner_name == "MADL, DANIEL E."


def test_apply_parcel_to_notice_blank_street_returns_false():
    notice = NoticeData(county="Johnson", notice_type="probate")
    applied = _apply_parcel_to_notice(notice, {"owner": "X", "parcel_address": ""})
    assert applied is False
    assert notice.address == ""


def test_apply_parcel_to_notice_blank_city_uses_default():
    notice = NoticeData(county="Johnson", notice_type="probate")
    parcel = {"owner": "X", "parcel_address": "1 MAIN ST", "situs_city": "", "situs_zip": ""}
    _apply_parcel_to_notice(notice, parcel)
    assert notice.city == "Olathe"
    assert notice.zip == ""  # left blank, not guessed -- Smarty corrects downstream


# -- probate_property_lookup_ks (end-to-end, mocked) ------------------------


def test_probate_lookup_tier1_hit():
    notice = NoticeData(
        county="Johnson", notice_type="probate",
        decedent_name="Daniel Madl", owner_name="",
    )
    with patch("ks_parcel_lookup._ks_name_search", return_value=[
        (0.67, {"owner": "MADL, DANIEL E.", "owner2": "MEHAN, MONICA M.",
                "parcel_address": "12615 W 52ND ST", "situs_city": "Shawnee",
                "situs_zip": "66216", "parcel_id": "K1"})
    ]), patch("ks_parcel_lookup.time.sleep"):
        probate_property_lookup_ks([notice])
    assert notice.address == "12615 W 52Nd St"
    assert notice.state == "KS"


def test_probate_lookup_skips_non_johnson_county():
    notice = NoticeData(county="Clay", notice_type="probate", decedent_name="John Smith")
    with patch("ks_parcel_lookup._ks_name_search") as mock_search:
        probate_property_lookup_ks([notice])
    mock_search.assert_not_called()
    assert notice.address == ""


def test_probate_lookup_skips_notice_with_existing_address():
    notice = NoticeData(
        county="Johnson", notice_type="probate",
        decedent_name="John Smith", address="Already Set",
    )
    with patch("ks_parcel_lookup._ks_name_search") as mock_search:
        probate_property_lookup_ks([notice])
    mock_search.assert_not_called()


def test_probate_lookup_falls_through_to_tier2_executor():
    notice = NoticeData(
        county="Johnson", notice_type="probate",
        decedent_name="John Smith", owner_name="Jane Family",
    )

    def fake_search(surname, first_name="", full_search_name="", min_score=0.4):
        if surname.upper() == "SMITH":
            return []
        if surname.upper() == "FAMILY":
            return [(0.5, {
                "owner": "Family, Jane", "owner2": "Smith, Estate Trust",
                "parcel_address": "200 ELM ST", "situs_city": "Olathe",
                "situs_zip": "66061", "parcel_id": "K9",
            })]
        return []

    with patch("ks_parcel_lookup._ks_name_search", side_effect=fake_search), \
         patch("ks_parcel_lookup.time.sleep"):
        probate_property_lookup_ks([notice])

    assert notice.address == "200 Elm St"


def test_probate_lookup_falls_through_to_tier3_people_search():
    notice = NoticeData(
        county="Johnson", notice_type="probate",
        decedent_name="John Smith", owner_name="",
    )
    with patch("ks_parcel_lookup._ks_name_search", return_value=[]), \
         patch("ks_parcel_lookup.time.sleep"), \
         patch("ks_parcel_lookup._people_search_property_ks", return_value="300 Oak Rd Olathe"):
        probate_property_lookup_ks([notice])
    assert notice.address == "300 Oak Rd Olathe"
    assert notice.city == "Olathe"


def test_probate_lookup_no_match_leaves_address_blank():
    notice = NoticeData(
        county="Johnson", notice_type="probate",
        decedent_name="John Smith", owner_name="",
    )
    with patch("ks_parcel_lookup._ks_name_search", return_value=[]), \
         patch("ks_parcel_lookup.time.sleep"), \
         patch("ks_parcel_lookup._people_search_property_ks", return_value=None):
        probate_property_lookup_ks([notice])
    assert notice.address == ""


def test_probate_lookup_no_decedent_name_skipped():
    notice = NoticeData(county="Johnson", notice_type="probate", decedent_name="")
    with patch("ks_parcel_lookup._ks_name_search") as mock_search:
        probate_property_lookup_ks([notice])
    mock_search.assert_not_called()
