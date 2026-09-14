"""Tests for mo_cass_parcel_lookup.py — Cass County, MO property lookup.

Row-HTML fixtures below mirror the exact shape confirmed live 2026-09-14
against cass.missouriassessors.com/results.php — see that module's
docstring for the full investigation (including why the site's own search
form makes the required mode/history hidden fields easy to miss).
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from notice_parser import NoticeData
from mo_cass_parcel_lookup import (
    _apply_parcel_to_notice,
    _cass_name_query,
    _extract_surname_candidates,
    probate_property_lookup_cass,
)


class _FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _row_html(gid, parcel_id, owner, street, city):
    return f'''
        <tr class="even">
            <td>  <!-- Parcel Number -->
                <a href="parcel.php?gid={gid}">{parcel_id}</a>
            </td>
            <td>
                <a href=""><img src="//photos.camavision.com/x/70" /></a>
            </td>
            <td class="mobileHide">{owner}<br/>
            </td>
            <td>
                {street}
                <br/>{city}
            </td>
        </tr>'''


def _results_page(*rows):
    body = "\n".join(_row_html(*r) for r in rows)
    return f'<table id="resultsWrapper"><tr><th>Parcel Number</th></tr>{body}</table>'


# ── _extract_surname_candidates ──────────────────────────────────────────


def test_extract_surname_first_last():
    assert _extract_surname_candidates("JOHN SMITH") == ["SMITH", "JOHN"]


def test_extract_surname_with_suffix():
    assert _extract_surname_candidates("JOHN SMITH JR") == ["SMITH", "JOHN"]


def test_extract_surname_single_token():
    assert _extract_surname_candidates("MADONNA") == ["MADONNA"]


def test_extract_surname_empty():
    assert _extract_surname_candidates("") == []


# ── _cass_name_query (mocked HTTP) ───────────────────────────────────────


def test_cass_name_query_parses_real_row_shape():
    html = _results_page(
        ("39310", "05-06-14-300-002-148.000", "ALLEN &amp; CHERYL SMITH TR", "516 ELDORADO DR", "BELTON"),
    )
    with patch("mo_cass_parcel_lookup.requests.get", return_value=_FakeResponse(html)):
        results = _cass_name_query("SMITH", full_search_name="CHERYL SMITH")
    assert len(results) == 1
    score, parcel = results[0]
    assert score > 0
    assert parcel["owner"] == "ALLEN & CHERYL SMITH TR"  # &amp; decoded
    assert parcel["situs_street"] == "516 Eldorado Dr"
    assert parcel["situs_city"] == "Belton"
    assert parcel["parcel_id"] == "05-06-14-300-002-148.000"


def test_cass_name_query_scores_and_sorts_multiple_rows():
    html = _results_page(
        ("1", "P-1", "RANDOM UNRELATED OWNER", "1 ELM ST", "BELTON"),
        ("2", "P-2", "JOHN Q SMITH", "2 OAK ST", "RAYMORE"),
    )
    with patch("mo_cass_parcel_lookup.requests.get", return_value=_FakeResponse(html)):
        results = _cass_name_query("SMITH", full_search_name="JOHN SMITH")
    assert len(results) == 1
    assert results[0][1]["owner"] == "JOHN Q SMITH"


def test_cass_name_query_filters_below_min_score():
    html = _results_page(
        ("1", "P-1", "ALLEN & CHERYL SMITH TR", "1 ELM ST", "BELTON"),
    )
    with patch("mo_cass_parcel_lookup.requests.get", return_value=_FakeResponse(html)):
        results = _cass_name_query("SMITH", full_search_name="ZZQXVTH WOBBLEFARB")
    assert results == []


def test_cass_name_query_empty_surname_returns_empty():
    assert _cass_name_query("") == []


def test_cass_name_query_request_error_returns_empty():
    import requests
    with patch("mo_cass_parcel_lookup.requests.get", side_effect=requests.RequestException("boom")):
        assert _cass_name_query("SMITH") == []


def test_cass_name_query_no_rows_returns_empty():
    html = '<table id="resultsWrapper"><tr><th>Parcel Number</th></tr></table>'
    with patch("mo_cass_parcel_lookup.requests.get", return_value=_FakeResponse(html)):
        assert _cass_name_query("ZZZNOTFOUND") == []


# ── _apply_parcel_to_notice ──────────────────────────────────────────────


def _notice(**kwargs):
    defaults = dict(county="Cass", notice_type="probate", source_url="x", state="")
    defaults.update(kwargs)
    return NoticeData(**defaults)


def test_apply_parcel_sets_address_city_state_parcel_owner():
    n = _notice()
    parcel = {"owner": "ALLEN & CHERYL SMITH TR", "parcel_id": "05-06-14-300-002-148.000",
              "situs_street": "516 Eldorado Dr", "situs_city": "Belton"}
    assert _apply_parcel_to_notice(n, parcel) is True
    assert n.address == "516 Eldorado Dr"
    assert n.city == "Belton"
    assert n.state == "MO"
    assert n.parcel_id == "05-06-14-300-002-148.000"
    assert n.tax_owner_name == "ALLEN & CHERYL SMITH TR"


def test_apply_parcel_no_street_returns_false():
    n = _notice()
    assert _apply_parcel_to_notice(n, {"owner": "X", "situs_street": ""}) is False
    assert n.address == ""


def test_apply_parcel_does_not_overwrite_existing_tax_owner_name():
    n = _notice(tax_owner_name="ALREADY SET")
    parcel = {"owner": "NEW OWNER", "situs_street": "1 Elm St", "situs_city": "Belton"}
    _apply_parcel_to_notice(n, parcel)
    assert n.tax_owner_name == "ALREADY SET"


# ── probate_property_lookup_cass (end-to-end with mocked HTTP) ──────────


def test_probate_lookup_skips_non_cass_and_already_addressed():
    notices = [
        _notice(county="Clay", decedent_name="JOHN SMITH"),
        _notice(county="Cass", decedent_name="", ),
        _notice(county="Cass", decedent_name="JOHN SMITH", address="123 Already Set"),
    ]
    with patch("mo_cass_parcel_lookup.requests.get") as mock_get:
        probate_property_lookup_cass(notices)
    mock_get.assert_not_called()


def test_probate_lookup_tier1_direct_match():
    html = _results_page(
        ("1", "P-1", "CHERYL SMITH", "516 Eldorado Dr", "Belton"),
    )
    n = _notice(decedent_name="CHERYL SMITH", owner_name="")
    with patch("mo_cass_parcel_lookup.requests.get", return_value=_FakeResponse(html)), \
         patch("mo_cass_parcel_lookup.time.sleep"):
        probate_property_lookup_cass([n])
    assert n.address == "516 Eldorado Dr"
    assert n.city == "Belton"


def test_probate_lookup_tier2_executor_family_match():
    # Decedent's name alone finds nothing; executor's surname search finds a
    # parcel whose combined deed-holder string contains the decedent's
    # surname (family property transferred to/held with the executor).
    def fake_get(url, params=None, **kwargs):
        if params.get("ideed") in ("SMITH", "JOHN"):
            return _FakeResponse(_results_page())  # decedent search: no hits
        return _FakeResponse(_results_page(
            ("1", "P-1", "MARY JONES & JOHN SMITH", "9 Pine Rd", "Raymore"),
        ))

    n = _notice(decedent_name="JOHN SMITH", owner_name="MARY JONES")
    with patch("mo_cass_parcel_lookup.requests.get", side_effect=fake_get), \
         patch("mo_cass_parcel_lookup.time.sleep"):
        probate_property_lookup_cass([n])
    assert n.address == "9 Pine Rd"
    assert n.city == "Raymore"


def test_probate_lookup_falls_through_to_people_search():
    html = _results_page()  # no county-search hits at all
    n = _notice(decedent_name="ZZQXVTH WOBBLEFARB", owner_name="")
    with patch("mo_cass_parcel_lookup.requests.get", return_value=_FakeResponse(html)), \
         patch("mo_cass_parcel_lookup.time.sleep"), \
         patch("mo_cass_parcel_lookup._people_search_property_cass", return_value="42 Test Ave"):
        probate_property_lookup_cass([n])
    assert n.address == "42 Test Ave"
    assert n.city == "Belton"


def test_probate_lookup_no_match_leaves_address_blank():
    html = _results_page()
    n = _notice(decedent_name="ZZQXVTH WOBBLEFARB", owner_name="")
    with patch("mo_cass_parcel_lookup.requests.get", return_value=_FakeResponse(html)), \
         patch("mo_cass_parcel_lookup.time.sleep"), \
         patch("mo_cass_parcel_lookup._people_search_property_cass", return_value=None):
        probate_property_lookup_cass([n])
    assert n.address == ""
