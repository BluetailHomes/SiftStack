import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import wholesale_offer as wo


def uw(arv, rehab, m, holding, buying, selling, **kw):
    return {"arv": arv, "rehab": rehab, "hold_months": m, "holding_total": holding,
            "buying_fixed": buying, "selling_total": selling, "annual_rate": 0.12,
            "title_rate": 0.0077, "arv_confidence": "MODERATE", "arv_confidence_basis": "4 renovated sold comps",
            "rehab_confidence": "LOW", "rehab_confidence_basis": "no photos", **kw}


# v3 CRM notes (output/account_backups/record_logs_all.jsonl) whose cost inputs were all recorded.
# Recorded figures were rounded in the notes, so allow a few dollars.
V3_NOTES = [
    (uw(310_000, 136_800, 6, 3_000, 1_000, 21_422), 101_684),
    (uw(180_000, 52_332, 6, 2_700, 1_000, 13_277), 82_000),
    (uw(187_000, 44_840, 5, 1_600, 1_000, 13_700), 97_970),
    (uw(148_000, 43_750, 4, 1_400, 1_000, 11_272), 65_700),
    (uw(350_000, 15_000, 3, 1_635, 1_100, 24_010), 262_900),
    (uw(460_000, 169_000, 5, 5_300, 1_100, 30_900), 188_400),   # 505 NE Olympic, v3 pass
]


@pytest.mark.parametrize("inputs,recorded", V3_NOTES)
def test_reproduces_v3_end_buyer_max(inputs, recorded):
    assert abs(wo.end_buyer_max(inputs) - recorded) <= 30


def test_required_profit_floor_and_pct():
    assert wo.required_profit(150_000) == 20_000
    assert wo.required_profit(460_000) == 46_000


def test_olympic_gap_is_legacy_math():
    # The $161,013 "ABSOLUTE CEILING" in the Olympic workbook is the 75% rule minus the
    # retired $15K fee, not v3. With Ty's own workbook inputs v3 gives ~$195.4K ($190.4K ceiling).
    assert 0.75 * 460_000 - 168_987 - 15_000 == 161_013
    ty_inputs = uw(460_000, 168_987, 4, 4_084, 900, 28_546)
    assert round(wo.end_buyer_max(ty_inputs)) == 195_403


def test_sensitivity_matches_v3_note():
    # 3462 S 151st E Ave pass: EBM $112,629, ceiling $107,629, tested at $101,500.
    # Holding/selling were not recorded; these back-solve to the recorded EBM.
    i = uw(204_000, 44_840, 5, 2_856, 1_000, 13_534)
    assert abs(wo.end_buyer_max(i) - 112_629) <= 30
    got = {s["scenario"]: s for s in wo.sensitivity(i, 101_500)}
    expected = {"Base": (11_700, "PASS"), "ARV -5%": (2_600, "PASS-THIN"),
                "Rehab +20%": (2_300, "PASS-THIN"), "Hold +2 mo": (7_700, "PASS"),
                "Combined": (-10_900, "FAIL")}
    for name, (cushion, verdict) in expected.items():
        assert abs(got[name]["cushion"] - cushion) <= 150, name
        assert got[name]["verdict"] == verdict, name


def case(**over):
    c = {
        "property": {"address": "123 Test St", "city": "Kansas City", "state": "MO", "zip": "64130",
                     "verified": True},
        "seller": {"names": ["Pat Example"], "verified": True},
        "underwriting": uw(204_000, 44_840, 5, 2_856, 1_000, 13_534),
        "seller_position": {},
        "terms": {"buyer_name": "Bluetail Homes LLC", "signer_name": "Kaycie Robinson",
                  "contact_phone": "(816) 555-0100", "closing_days": 30, "earnest_money": 1_000,
                  "inspection_days": 10, "expires_on": "November 13, 2026"},
    }
    for k, v in over.items():
        c[k] = {**c[k], **v} if isinstance(v, dict) and isinstance(c.get(k), dict) else v
    return c


def test_default_tier_is_first_three_and_explicit():
    r = wo.underwrite(case())
    assert r["fee_tier"] == {"key": "first_three", "label": "Deals 1-3", "gross": 5_000, "net": 2_500}
    assert r["seller_ceiling"] == r["end_buyer_max"] - 5_000
    r4 = wo.underwrite(case(fee_tier="deal_four_plus"))
    assert r4["seller_ceiling"] == r4["end_buyer_max"] - 10_000


def test_opening_is_90pct_of_ceiling_rounded_down():
    r = wo.underwrite(case())
    assert r["offer"] == 96_500 and r["offer_method"] == "90% of ceiling"


def test_opening_uses_minimum_then_cash_comps_and_never_exceeds_ask():
    r = wo.underwrite(case(seller_position={"verified_minimum": 80_000,
                                            "verified_minimum_source": "seller, call 10/27"}))
    assert r["offer"] == 80_000
    r = wo.underwrite(case(seller_position={"payoff": 99_000, "payoff_source": "lender payoff letter"}))
    assert r["offer"] == 99_000 and "lien payoff" in r["offer_method"]
    r = wo.underwrite(case(seller_position={"cash_comp": 90_000, "cash_comp_credible": True}))
    assert r["offer"] == 90_000
    r = wo.underwrite(case(seller_position={"cash_comp": 150_000, "cash_comp_credible": True}))
    assert r["offer_method"] == "90% of ceiling"            # comps above ceiling fall through
    r = wo.underwrite(case(seller_position={"cash_comp": 90_000}))
    assert r["offer_method"] == "90% of ceiling"            # not marked credible
    assert wo.underwrite(case(seller_position={"ask": 70_000}))["offer"] == 70_000


def test_no_viable_vs_below_minimum():
    r = wo.underwrite(case(underwriting=uw(100_000, 70_000, 6, 3_000, 1_000, 7_000)))
    assert r["status"] == wo.NO_VIABLE and not r["letter_allowed"]
    olympic = case(underwriting=uw(460_000, 169_000, 5, 5_300, 1_100, 30_900),
                   seller_position={"verified_minimum": 285_000, "verified_minimum_source": "seller, call 10/20"})
    r = wo.underwrite(olympic)
    assert r["status"] == wo.BELOW_MIN and not r["letter_allowed"]
    r = wo.underwrite(case(seller_position={"payoff": 120_000, "payoff_source": "lender payoff letter"}))
    assert r["status"] == wo.BELOW_MIN and "lien payoff" in r["reasons"][0]


def test_ask_far_above_ceiling_still_drafts():
    # An ask is not a minimum: a substantially below-ask offer is drafted when the economics support it.
    r = wo.underwrite(case(seller_position={"ask": 150_000}))
    assert r["status"] == wo.REVIEW and r["letter_allowed"] and r["offer"] == 96_500
    assert any("not a verified minimum" in x for x in r["reasons"])


def test_ready_only_when_nothing_to_acknowledge():
    r = wo.underwrite(case(underwriting=uw(350_000, 15_000, 3, 1_635, 1_100, 24_010,
                                           rehab_confidence="MODERATE", rehab_confidence_basis="walkthrough")))
    assert r["status"] == wo.READY and r["risks"] == []
    assert all(s["verdict"] != "FAIL" for s in r["sensitivity"])


def test_stress_failure_is_review_with_failing_assumptions():
    r = wo.underwrite(case(underwriting={"rehab_confidence": "MODERATE", "rehab_confidence_basis": "walkthrough"}))
    assert r["status"] == wo.REVIEW and r["letter_allowed"]       # flagged, not blocked
    assert wo.risk_codes(r) == ["stress-combined"]
    assert "short of the required profit" in r["risks"][0]["detail"]
    b = r["breakeven"]
    assert 0.06 < b["arv_drop_pct"] < 0.07 and 0.24 < b["rehab_rise_pct"] < 0.26


def test_single_low_confidence_is_review_and_both_low_qualifies():
    r = wo.underwrite(case())
    assert r["status"] == wo.REVIEW and "low-rehab-confidence" in wo.risk_codes(r)
    assert "no photos" in next(x["detail"] for x in r["risks"] if x["code"] == "low-rehab-confidence")
    r = wo.underwrite(case(underwriting={"arv_confidence": "LOW", "rehab_confidence": "LOW"}))
    assert r["status"] == wo.QUALIFY and "offer" not in r and not r["letter_allowed"]


def test_defaulted_costs_are_listed_and_need_acknowledgment(tmp_path):
    u = uw(350_000, 15_000, 3, 1_635, 1_100, 24_010, rehab_confidence="MODERATE", rehab_confidence_basis="walkthrough")
    del u["title_rate"], u["annual_rate"]
    (tmp_path / "case.json").write_text(json.dumps({**case(), "underwriting": u}))
    c = wo.load_case(tmp_path / "case.json")
    r = wo.underwrite(c)
    assert r["status"] == wo.REVIEW and wo.risk_codes(r) == ["assumed-costs"]
    packet = wo.packet_md(c, r, "0" * 16)
    assert "Provisional defaults (NOT verified market costs)" in packet and "title 0.77%" in packet


@pytest.mark.parametrize("over", [
    {"underwriting": {"selling_total": None}},
    {"underwriting": {"arv_confidence": None}},
    {"underwriting": {"rehab_confidence_basis": None}},              # confidence without evidence
    {"underwriting": {"arv_confidence": "MEDIUM-ish"}},
    {"seller_position": {"verified_minimum": 50_000}},                # minimum without a source
    {"property": {"verified": False}},
    {"seller": {"verified": False}},
])
def test_missing_or_unverified_inputs_qualify(over, tmp_path):
    r = wo.draft(case(**over), tmp_path)
    assert r["status"] == wo.QUALIFY and r["missing"]
    assert not (tmp_path / "offer_letter_DRAFT.pdf").exists()


def test_load_case_never_defaults_confidence(tmp_path):
    u = uw(204_000, 44_840, 5, 2_856, 1_000, 13_534)
    del u["arv_confidence"], u["arv_confidence_basis"]
    (tmp_path / "case.json").write_text(json.dumps({**case(), "underwriting": u}))
    c = wo.load_case(tmp_path / "case.json")
    assert "arv_confidence" not in c["underwriting"]
    r = wo.draft(c, tmp_path)
    assert r["status"] == wo.QUALIFY and "underwriting.arv_confidence" in r["missing"]
    assert "NOT RECORDED" in (tmp_path / "packet.md").read_text(encoding="utf-8")


def test_missing_terms_block_letter(tmp_path):
    r = wo.draft(case(terms={"earnest_money": None}), tmp_path)
    assert not r["letter_allowed"] and "terms.earnest_money" in r["missing"]
    assert not (tmp_path / "offer_letter_DRAFT.pdf").exists()


def _pdf_text(path):
    import pypdfium2 as pdfium
    return "\n".join(p.get_textpage().get_text_range() for p in pdfium.PdfDocument(str(path)))


def test_draft_letter_is_marked_and_leak_free(tmp_path):
    c = case()
    r = wo.draft(c, tmp_path)
    text = _pdf_text(tmp_path / "offer_letter_DRAFT.pdf")
    assert "DRAFT" in text and "$96,500" in text and "nonbinding" in text
    assert wo.leak_check(text.replace("DRAFT - FOR INTERNAL REVIEW - DO NOT SEND", ""), c, r) == []
    for v in (r["end_buyer_max"], r["seller_ceiling"], 204_000, 44_840, 20_400):
        assert f"{v:,}" not in text
    packet = (tmp_path / "packet.md").read_text(encoding="utf-8")
    assert "INTERNAL" in packet and f"{r['seller_ceiling']:,}" in packet


def test_leak_check_catches_internal_content():
    c = case()
    r = wo.underwrite(c)
    assert wo.leak_check("Our ARV supports this.", c, r) == ["arv"]
    assert "profit" in wo.leak_check("leaves room for profit", c, r)
    assert f"${r['seller_ceiling']:,}" in wo.leak_check(f"up to ${r['seller_ceiling']:,}", c, r)
    assert wo.leak_check("value near 204,000", c, r) == ["internal figure 204,000"]
    cushion = abs(r["sensitivity"][0]["cushion"])
    assert wo.leak_check(f"about {cushion:,} left", c, r) == [f"internal figure {cushion:,}"]


def test_letter_blocked_if_leak(tmp_path, monkeypatch):
    c = case()
    monkeypatch.setattr(wo, "letter_lines", lambda *_: ["Our rehab budget is tight."])
    with pytest.raises(SystemExit, match="blocked"):
        wo.write_letter_pdf(c, wo.underwrite(c), tmp_path / "x.pdf", draft=True, version="0" * 16)


def test_final_requires_matching_approval(tmp_path):
    c = case()
    with pytest.raises(SystemExit, match="No approval"):
        wo.final(c, tmp_path)
    r = wo.draft(c, tmp_path)
    with pytest.raises(SystemExit, match="does not match"):
        wo.approve(c, tmp_path, "bad", r["offer"], "Kaycie", wo.risk_codes(r))
    with pytest.raises(SystemExit, match="Price"):
        wo.approve(c, tmp_path, r["offer_version"], r["offer"] + 500, "Kaycie", wo.risk_codes(r))
    wo.approve(c, tmp_path, r["offer_version"], r["offer"], "Kaycie", wo.risk_codes(r))
    path = wo.final(c, tmp_path)
    text = _pdf_text(path)
    assert "DRAFT" not in text and "$96,500" in text
    assert wo.leak_check(text, c, r) == []


def test_review_needs_exact_risk_acknowledgment(tmp_path):
    c = case()
    r = wo.draft(c, tmp_path)
    codes = wo.risk_codes(r)
    assert codes == ["low-rehab-confidence", "stress-combined"]
    for ack in ([], codes[:1], codes + ["made-up"]):
        with pytest.raises(SystemExit, match="acknowledge exactly"):
            wo.approve(c, tmp_path, r["offer_version"], r["offer"], "Kaycie", ack)
    assert not (tmp_path / "approval.json").exists()
    rec = wo.approve(c, tmp_path, r["offer_version"], r["offer"], "Kaycie", codes)
    assert rec["status"] == wo.REVIEW and [x["code"] for x in rec["acknowledged_risks"]] == codes
    packet = (tmp_path / "packet.md").read_text(encoding="utf-8")
    assert "--acknowledge low-rehab-confidence,stress-combined" in packet and "not authentication" in packet


@pytest.mark.parametrize("change", [
    lambda c: c["terms"].update(closing_days=21),
    lambda c: c["underwriting"].update(rehab=50_000),
    lambda c: c.update(fee_tier="deal_four_plus"),
    lambda c: c["seller_position"].update(ask=90_000),
])
def test_any_change_after_approval_needs_reapproval(change, tmp_path):
    c = case()
    r = wo.draft(c, tmp_path)
    wo.approve(c, tmp_path, r["offer_version"], r["offer"], "Kaycie", wo.risk_codes(r))
    c2 = copy.deepcopy(c)
    change(c2)
    with pytest.raises(SystemExit, match="Reapproval"):
        wo.final(c2, tmp_path)


def test_packet_flags_legal_and_sop_mismatch_letter_never_mentions_assignment(tmp_path):
    c = case(property={"state": "OK"})
    r = wo.draft(c, tmp_path)
    packet = (tmp_path / "packet.md").read_text(encoding="utf-8")
    assert "attorney" in packet and "Oklahoma" in packet and "14-day due diligence" in packet
    assert "Transfer and recording taxes are not modeled" in packet
    text = _pdf_text(tmp_path / "offer_letter_DRAFT.pdf").lower()
    assert "assign" not in text and "risk" not in text and "stress" not in text
    assert "assign" in wo.leak_check("Buyer and/or assigns", c, r)


def test_ty_mapping_excludes_points_and_defaults_buying(tmp_path):
    ty = {"deal_analysis": {
        "arv": 172_000, "rehab_cost": 94_417, "hold_time_months": 5.8,
        "holding_costs": {"total_holding": 3_144}, "selling_costs": {"total_selling": 14_620},
        "buying_costs": {"escrow_attorney": 0, "total_buying": 3_125},
        "financing": {"interest_rate_annual": 0.12, "points_pct": 0.02},
        "rehab_net_profit": -85_421, "wholetail_net_profit": -84_991}}
    (tmp_path / "rehab_data.json").write_text(json.dumps(ty))
    (tmp_path / "comp_data.json").write_text(json.dumps({"arv_calculation": {
        "confidence_level": "Low", "confidence_band_pct": 7.0, "arv_low": 160_000, "arv_high": 184_000}}))
    (tmp_path / "case.json").write_text(json.dumps({**case(), "underwriting": {
        "rehab_confidence": "MODERATE", "rehab_confidence_basis": "walkthrough 10/27"},
        "ty_rehab_data": "rehab_data.json", "ty_comp_data": "comp_data.json"}))
    c = wo.load_case(tmp_path / "case.json")
    assert c["underwriting"]["buying_fixed"] == wo.BUYING_FIXED_DEFAULT
    assert c["underwriting"]["arv_confidence"] == "LOW"
    assert "range $160,000-$184,000" in c["underwriting"]["arv_confidence_basis"]
    r = wo.underwrite(c)
    assert r["reference"]["flip_net_profit"] == -85_421 and r["reference"]["mao_75"] == 34_583
    assert "buying fixed $1,000" in " ".join(r["assumed"]) and "title 0.77%" in " ".join(r["assumed"])
    assert "Lender points excluded" in r["cost_notes"][0] and "lower" in r["cost_notes"][0]
