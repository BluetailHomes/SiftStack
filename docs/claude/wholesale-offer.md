# Wholesale offer layer (`src/wholesale_offer.py`)

Built 2026-10-27. Sits on top of Ty's deal analyzer, which stays unchanged and remains the source for comps, ARV, rehab and the flip / wholetail / wholesale exits. This module adds only the wholesale decision: the v3 End-Buyer Max (bluetail-rehab-estimator Step 5.6), the Bluetail seller ceiling, the opening offer, an internal approval packet and a nonbinding written-offer PDF.

Nothing here sends a letter, notifies a seller or touches DataSift. **Offers stay manual.** Only Kaycie runs `approve`; `--approved-by` is a label, not authentication.

## 1. Prepare a case

Make a folder per property (e.g. `output/deal_analysis/<address>/offer/`) with a `case.json`:

```json
{
 "property": {"address": "3118 Spruce Ave", "city": "Kansas City", "state": "MO", "zip": "64128", "verified": true},
 "seller": {"names": ["<owner of record>"], "verified": true},
 "ty_rehab_data": "../rehab_data.json",
 "ty_comp_data": "../comp_data.json",
 "underwriting": {
  "rehab_confidence": "MODERATE",
  "rehab_confidence_basis": "walkthrough 10/27: roof 2019, furnace missing"
 },
 "seller_position": {"ask": 72000},
 "fee_tier": "first_three",
 "terms": {"buyer_name": "<buyer entity>", "signer_name": "Kaycie Robinson", "contact_phone": "<phone>",
           "closing_days": 30, "earnest_money": 1000, "inspection_days": 10, "expires_on": "November 13, 2026"}
}
```

- `ty_rehab_data` / `ty_comp_data` (paths relative to `case.json`) pull ARV, rehab, hold, holding, selling, rate, points and ARV confidence from Ty's analyzer output. Any value in `underwriting` overrides them. Without Ty files, give `arv, rehab, hold_months, holding_total, selling_total` directly.
- **Confidence is never defaulted.** `arv_confidence` and `rehab_confidence` (HIGH / MODERATE / LOW) each need a `*_basis` describing the evidence (comps, photos, walkthrough).
- `verified: true` only when the address and the seller's identity are confirmed (owner of record / probate PR).
- `seller_position`: `ask` is what the seller said and is never treated as a minimum. `verified_minimum` and `payoff` each need a `verified_minimum_source` / `payoff_source`. `cash_comp` counts only with `cash_comp_credible: true`.
- `annual_rate`, `title_rate` and `buying_fixed` default to v3 values and are listed as provisional defaults until set from a real quote.
- `fee_tier`: `first_three` (5K gross / 2.5K net, default) or `deal_four_plus` (10K / 5K).

## 2. Draft: internal packet + DRAFT letter

```bash
python src/wholesale_offer.py draft <dir>/case.json --out <dir>
```

Writes `packet.md` (internal: decision, risks, defaults, inputs, ceiling, stress tests, break-even, legal flags, the exact approve command), `underwriting.json`, and `offer_letter_DRAFT.pdf` when a letter is allowed.

| Decision | Meaning | Letter |
|---|---|---|
| OFFER READY | Viable, nothing to acknowledge | DRAFT |
| REVIEW REQUIRED | Viable, but risks listed by code (low confidence, stress-test failures, thin base, assumed or unmodeled costs) | DRAFT |
| QUALIFY / GET PHOTOS | Missing / unverified / undocumented inputs, or both confidences LOW | none |
| VIABLE ONLY BELOW SELLER MINIMUM | Verified minimum or lien payoff above the ceiling | none |
| NO ECONOMICALLY VIABLE WHOLESALE OFFER | Ceiling at or below zero | none |

## 3. Review the draft PDF

Open `offer_letter_DRAFT.pdf`. It is watermarked DRAFT and carries a version reference. It must contain only price, as-is, closing, earnest money, inspection period, expiry and the nonbinding statement; the module refuses to write a letter containing internal figures, underwriting terms or assignment language.

## 4. Approve the exact price and terms (Kaycie only)

Copy the command from the end of `packet.md`:

```bash
python src/wholesale_offer.py approve <dir>/case.json --out <dir> --version <offer_version> --price <offer> --approved-by Kaycie --acknowledge <every risk code in the packet>
```

Refused unless the version, price and the full set of risk codes match. Writes `approval.json`. Approving sends nothing.

## 5. Final PDF

```bash
python src/wholesale_offer.py final <dir>/case.json --out <dir>
```

Writes `offer_letter_FINAL.pdf` only if `approval.json` matches the current case. Any change to price, terms, inputs or risks changes the version and requires a new approval. Delivering the letter is a manual step.

## Outstanding before the first live seller letter

- [ ] Reconcile the 10-day inspection in the letter terms with the Assignment SOP's 14-day due diligence.
- [ ] Validate market-specific costs: lender points (excluded by v3; the packet shows their effect), transfer/recording taxes (not modeled), title rate, buying fixed and interest.
- [ ] Confirm the buyer entity name, the seller's identity and the contact phone for each letter.
- [ ] Attorney / title review of state-specific purchase agreement and assignment requirements per market (Oklahoma has a wholesaling statute; verify).
- [ ] Only Kaycie operates `approve` for now; no automated delivery.

The 2026-10-27 Spruce example (REVIEW REQUIRED, $36,500) was a workflow test only and is **not approved**.
