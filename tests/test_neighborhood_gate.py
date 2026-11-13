"""Tests for the target-neighborhood gate (Step 8c), the Jackson SiftMap name tier
(Step 3c), and the Jackson ZIP-drop exemption in Step 8b. No network: SiftMap is faked."""

import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import siftmap_lookup
from datasift_formatter import _build_tags
from enrichment_pipeline import _filter_high_potential, _filter_target_neighborhoods
from notice_parser import NoticeData
from siftmap_lookup import (SiftMap, name_variants, norm_hood, probate_property_lookup_siftmap,
                            split_address)

TARGETS = {"Jackson": frozenset({"oak park southwest", "three trails"})}


def _jackson(address="4232 S Benton Ave", **kw):
    return NoticeData(address=address, city="Kansas City", state="MO", zip="64130",
                      county="Jackson", notice_type="probate", **kw)


def _gate(notices, hoods, drop=True, tmp_path=None, monkeypatch=None):
    if monkeypatch and tmp_path:
        monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    return _filter_target_neighborhoods(notices, TARGETS, drop=drop, lookup=lambda n: hoods.get(n.address, ""))


# ── helpers ──────────────────────────────────────────────────────────────


def test_norm_hood_strips_state_suffix_and_case():
    assert norm_hood("Oak Park Southwest, MO") == "oak park southwest"
    assert norm_hood(None) == ""


def test_split_address():
    assert split_address("4232 S Benton Ave, Kansas City, MO 64130") == {
        "street": "4232 S Benton Ave", "city": "Kansas City", "state": "MO", "zip": "64130"}
    assert split_address("garbage") == {}


def test_name_variants_drop_punctuation_and_suffix():
    assert name_variants("John W. Shinn, Jr.") == ["John W Shinn", "John Shinn", "Shinn John"]


def test_config_list_is_shared_with_obituary_dp():
    import obituary_dp
    assert obituary_dp.P1_NEIGHBORHOODS is config.TARGET_NEIGHBORHOODS
    assert len(config.TARGET_NEIGHBORHOODS["Jackson"]) == 25


# ── Step 8c gate ─────────────────────────────────────────────────────────


def test_inside_kept_and_tagged(tmp_path, monkeypatch):
    out = _gate([_jackson()], {"4232 S Benton Ave": "Oak Park Southwest"}, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert len(out) == 1 and out[0].neighborhood_status == "in_target"
    assert "p1_neighborhood" in _build_tags(out[0])


def test_outside_suppressed_and_logged(tmp_path, monkeypatch):
    out = _gate([_jackson("826 N Ponca Dr")], {"826 N Ponca Dr": "Cler-mont"}, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert out == []
    rows = list(csv.DictReader(open(tmp_path / "suppressed_leads.csv", encoding="utf-8")))
    assert rows[0]["reason_suppressed"] == "outside_target_neighborhoods:Cler-mont"


def test_outside_kept_when_drops_disabled(tmp_path, monkeypatch):
    out = _gate([_jackson("826 N Ponca Dr")], {"826 N Ponca Dr": "Cler-mont"}, drop=False,
                tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert len(out) == 1 and "nbhd_outside" in _build_tags(out[0])


def test_no_address_kept_as_unplaced(tmp_path, monkeypatch):
    calls = []
    out = _filter_target_neighborhoods([_jackson("")], TARGETS, lookup=lambda n: calls.append(n) or "x")
    assert len(out) == 1 and out[0].neighborhood_status == "unplaced"
    assert calls == []  # no lookup without an address
    assert "nbhd_unplaced" in _build_tags(out[0])


def test_no_siftmap_match_kept_as_unplaced(tmp_path, monkeypatch):
    out = _gate([_jackson()], {}, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert len(out) == 1 and out[0].neighborhood_status == "unplaced"


def test_lookup_error_holds_not_drops():
    def boom(n):
        raise RuntimeError("api changed")
    out = _filter_target_neighborhoods([_jackson()], TARGETS, lookup=boom)
    assert len(out) == 1 and out[0].neighborhood_status == "unplaced"


def test_county_without_list_untouched():
    n = NoticeData(address="1 Main St", county="Clay", notice_type="probate")
    out = _filter_target_neighborhoods([n], TARGETS, lookup=lambda n: "Cler-mont")
    assert out == [n] and n.neighborhood_status == "" and n.neighborhood == ""


def test_existing_neighborhood_skips_lookup(tmp_path, monkeypatch):
    n = _jackson(neighborhood="Three Trails, MO")
    out = _filter_target_neighborhoods([n], TARGETS, lookup=lambda n: (_ for _ in ()).throw(AssertionError))
    assert out[0].neighborhood_status == "in_target"


# ── Step 8b exemption ────────────────────────────────────────────────────


def test_jackson_zip_drop_exempt_when_neighborhood_list(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    n = NoticeData(address="2456 S Scott Ave", zip="64052", county="Jackson", notice_type="probate")
    kept = _filter_high_potential([n], {"64130": "Tier 1"}, 20.0, zip_exempt_counties=frozenset({"jackson"}))
    assert kept == [n]
    dropped = _filter_high_potential([NoticeData(address="2456 S Scott Ave", zip="64052", county="Jackson")],
                                     {"64130": "Tier 1"}, 20.0)
    assert dropped == []


# ── SiftMap client (faked HTTP) ──────────────────────────────────────────


class FakeSiftMap(SiftMap):
    def __init__(self, address_rows=None, name_rows=None, hood="Oak Park Southwest"):
        self.address_rows, self.name_rows, self.hood, self.queries = address_rows or {}, name_rows or {}, hood, []

    def autocomplete(self, text, entity_type="address"):
        self.queries.append((entity_type, text))
        return (self.address_rows if entity_type == "address" else self.name_rows).get(text, [])

    def neighborhood(self, property_id):
        return self.hood


def _row(addr, owner="", county="Jackson", kind="address", pid="1"):
    return {"id": pid, "address": addr, "state": "MO", "searchType": kind,
            "owner": {"full_name": owner}, "counties": [{"county_name": county}]}


def test_lookup_address_requires_same_house_number():
    sm = FakeSiftMap(address_rows={"4232 S Benton Ave Kansas City MO 64130": [
        _row("4234 S Benton Ave, Kansas City, MO 64130")]})
    assert sm.lookup_address("4232 S Benton Ave", "Kansas City", "MO", "64130") is None


def test_lookup_address_retries_without_street_type():
    sm = FakeSiftMap(address_rows={"4232 S Benton Kansas City MO 64130": [
        _row("4232 S Benton Ave, Kansas City, MO 64130")]})
    hit = sm.lookup_address("4232 S Benton Ave", "Kansas City", "MO", "64130")
    assert hit["neighborhood"] == "Oak Park Southwest" and len(sm.queries) == 2


def test_lookup_address_ignores_other_county():
    sm = FakeSiftMap(address_rows={"1 Main St MO": [_row("1 Main St, Liberty, MO 64068", county="Clay")]})
    assert sm.lookup_address("1 Main St", state="MO") is None


def test_name_tier_fills_unique_match_and_overwrites_fallback_zip():
    sm = FakeSiftMap(name_rows={"Eugene Jackson": [
        _row("4232 S Benton Ave, Kansas City, MO 64130", "Eugene Jackson", kind="full_name")]})
    n = NoticeData(decedent_name="Eugene Jackson", county="Jackson", zip="64106")  # 64106 = PR's mailing ZIP
    probate_property_lookup_siftmap([n], sm=sm)
    assert (n.address, n.city, n.zip, n.address_source) == ("4232 S Benton Ave", "Kansas City", "64130", "siftmap_owner")
    assert "addr_from_owner_name" in _build_tags(n)


def test_name_tier_skips_ambiguous_and_surname_mismatch():
    sm = FakeSiftMap(name_rows={
        "John W Shinn": [_row("907 E Stone St, Independence, MO 64050", "John W Shinn", kind="full_name", pid="1"),
                         _row("1 Elm St, Independence, MO 64050", "John W Shinn", kind="full_name", pid="2")],
        "Mary Smit": [_row("2 Oak St, Kansas City, MO 64130", "Mary Smith", kind="full_name")],
    })
    a = NoticeData(decedent_name="John W Shinn", county="Jackson")
    b = NoticeData(decedent_name="Mary Smit", county="Jackson")
    probate_property_lookup_siftmap([a, b], sm=sm)
    assert a.address == "" and b.address == ""


def test_name_tier_stops_at_first_variant_with_hits():
    sm = FakeSiftMap(name_rows={
        "John W Shinn": [_row("907 E Stone St, Independence, MO 64050", "John W Shinn", kind="full_name")],
        "John Shinn": [_row("9 X St, Kansas City, MO 64130", "John Shinn", kind="full_name", pid="9")],
    })
    assert len(sm.lookup_owner("John W Shinn")) == 1
    assert ("full_name", "John Shinn") not in sm.queries


def test_siftmap_requires_key(monkeypatch):
    monkeypatch.delenv("REISIFT_API_KEY", raising=False)
    try:
        siftmap_lookup.SiftMap()
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError")
