"""Bluetail wholesale offer layer (built 2026-10-27).

Sits on top of Ty's deal analyzer, which stays the source for comps, ARV,
rehab, flip and wholetail. This module adds only the wholesale decision:
the v3 locked End-Buyer Max formula (bluetail-rehab-estimator Step 5.6),
the Bluetail seller ceiling, the opening offer, an internal approval packet
and a nonbinding written-offer PDF.

Nothing here sends anything or touches DataSift. The letter is DRAFT until
Kaycie approves the exact price, terms and offer version; any change to the
case afterwards changes the version and needs a new approval. A REVIEW
REQUIRED case also needs every listed risk acknowledged by its code.

--approved-by is a label for the record, not authentication: only Kaycie
runs `approve`, and nothing here sends the letter.

    python src/wholesale_offer.py draft   case.json --out DIR
    python src/wholesale_offer.py approve case.json --out DIR --version V --price P --approved-by NAME [--acknowledge CODE,...]
    python src/wholesale_offer.py final   case.json --out DIR
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import sys
from pathlib import Path

FORMULA_VERSION = "v3-step5.6"
MIN_PROFIT, PROFIT_PCT = 20_000, 0.10    # screening floor, not a promise a buyer accepts
OPEN_PCT, OPEN_ROUND = 0.90, 500         # 90% of ceiling, rounded down to $500
THIN_PCT = 0.20                          # cushion under 20% of required profit = PASS-THIN
BUYING_FIXED_DEFAULT = 1_000             # v3's escrow/attorney assumption; title is t x P
SOP_DUE_DILIGENCE_DAYS = 14              # Assignment_Clause_SOP.docx

# Gross assignment fee and Bluetail's expected share after EZREIClosings' 50%.
FEE_TIERS = {
    "first_three": {"label": "Deals 1-3", "gross": 5_000, "net": 2_500},
    "deal_four_plus": {"label": "Deal 4+", "gross": 10_000, "net": 5_000},
}
DEFAULT_TIER = "first_three"             # Bluetail has closed 0 wholesale deals (2026-10-27)

READY = "OFFER READY"
REVIEW = "REVIEW REQUIRED"               # viable, but risks need Kaycie's acknowledgment
QUALIFY = "QUALIFY / GET PHOTOS"         # missing, unverified or undocumented inputs
BELOW_MIN = "VIABLE ONLY BELOW SELLER MINIMUM"
NO_VIABLE = "NO ECONOMICALLY VIABLE WHOLESALE OFFER"

CONFIDENCE = ("HIGH", "MODERATE", "LOW")
UW_REQUIRED = ("arv", "rehab", "hold_months", "holding_total", "selling_total",
               "arv_confidence", "arv_confidence_basis", "rehab_confidence", "rehab_confidence_basis")
TERMS_REQUIRED = ("buyer_name", "signer_name", "contact_phone", "closing_days",
                  "earnest_money", "expires_on")

# Questions for an attorney or title company; shown in the packet, never in the letter.
LEGAL_FLAGS = (
    "The letter is nonbinding and does not mention assignment. Per the Assignment Clause SOP, the "
    "clause is introduced when the purchase agreement goes out, after price and terms are agreed.",
    "Confirm with a {state} attorney or title company that the purchase agreement's assignment "
    "clause is enforceable there and how the assignment must be disclosed to the seller.",
    "Confirm with title how earnest money is held and which closing costs each side pays in {state}.",
)
STATE_LEGAL_FLAGS = {
    "OK": "Oklahoma has a wholesaling statute (Predatory Real Estate Wholesaler Prohibition Act, 2021). "
          "Confirm license and disclosure requirements with an Oklahoma attorney before any offer.",
}


def required_profit(arv: float) -> float:
    return max(MIN_PROFIT, PROFIT_PCT * arv)


def end_buyer_max(uw: dict) -> float:
    """v3 closed form: P = [ARV - Rehab - Holding - Rehab*r*m - Buying - Selling - Profit] / (1 + r*m + t)."""
    r, m = uw["annual_rate"] / 12, uw["hold_months"]
    num = (uw["arv"] - uw["rehab"] - uw["holding_total"] - uw["rehab"] * r * m
           - uw["buying_fixed"] - uw["selling_total"] - required_profit(uw["arv"]))
    return num / (1 + r * m + uw["title_rate"])


def buyer_cushion(uw: dict, price: float) -> float:
    """End buyer's profit at `price` minus the required profit (negative = fails the screen)."""
    r, m = uw["annual_rate"] / 12, uw["hold_months"]
    profit = (uw["arv"] - uw["rehab"] - uw["holding_total"] - uw["rehab"] * r * m
              - uw["buying_fixed"] - uw["selling_total"] - price * (1 + r * m + uw["title_rate"]))
    return profit - required_profit(uw["arv"])


def _scenario(uw: dict, arv_pct=1.0, rehab_pct=1.0, extra_months=0) -> dict:
    # Selling scales with ARV and holding with months, as the v3 sensitivity notes do.
    m = uw["hold_months"]
    return {**uw, "arv": uw["arv"] * arv_pct, "rehab": uw["rehab"] * rehab_pct,
            "selling_total": uw["selling_total"] * arv_pct, "hold_months": m + extra_months,
            "holding_total": uw["holding_total"] * (m + extra_months) / m}


# (name, risk code if it fails, scenario)
SCENARIOS = (("Base", "fail-base", {}),
             ("ARV -5%", "stress-arv", {"arv_pct": 0.95}),
             ("Rehab +20%", "stress-rehab", {"rehab_pct": 1.2}),
             ("Hold +2 mo", "stress-hold", {"extra_months": 2}),
             ("Combined", "stress-combined", {"arv_pct": 0.95, "rehab_pct": 1.2, "extra_months": 2}))


def sensitivity(uw: dict, price: float) -> list[dict]:
    rows = []
    for name, code, kw in SCENARIOS:
        s = _scenario(uw, **kw)
        c = buyer_cushion(s, price)
        verdict = "FAIL" if c < 0 else "PASS-THIN" if c < THIN_PCT * required_profit(s["arv"]) else "PASS"
        rows.append({"scenario": name, "code": code, "cushion": round(c), "verdict": verdict})
    return rows


def breakeven(uw: dict, price: float) -> dict:
    """How far each assumption can move on its own before the end buyer misses the required profit.
    None = still passes at the end of the searched range."""
    def solve(f, hi):
        if f(0) < 0:
            return 0.0
        if f(hi) >= 0:
            return None
        lo = 0.0
        for _ in range(40):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if f(mid) >= 0 else (lo, mid)
        return lo
    return {"arv_drop_pct": solve(lambda x: buyer_cushion(_scenario(uw, arv_pct=1 - x), price), 0.5),
            "rehab_rise_pct": solve(lambda x: buyer_cushion(_scenario(uw, rehab_pct=1 + x), price), 3.0),
            "extra_months": solve(lambda x: buyer_cushion(_scenario(uw, extra_months=x), price), 24.0)}


def underwriting_from_ty(rehab_data: dict) -> dict:
    """Map Ty's rehab_data.json deal_analysis onto v3 inputs. Points are excluded, as in v3."""
    da = rehab_data["deal_analysis"]
    fin = da.get("financing") or {}
    escrow = (da.get("buying_costs") or {}).get("escrow_attorney") or 0
    assumed = [] if escrow else [f"buying fixed ${BUYING_FIXED_DEFAULT:,} (v3 default; Ty file has no escrow)"]
    if "interest_rate_annual" not in fin:
        assumed.append("interest 12%/yr (v3 default; Ty file has no rate)")
    return {
        "_assumed": assumed,
        "arv": da["arv"], "rehab": da["rehab_cost"], "hold_months": da["hold_time_months"],
        "holding_total": da["holding_costs"]["total_holding"],
        "selling_total": da["selling_costs"]["total_selling"],
        "buying_fixed": escrow or BUYING_FIXED_DEFAULT,
        "annual_rate": fin.get("interest_rate_annual", 0.12),
        "points_pct": fin.get("points_pct") or 0,
        "_ty_transfer_tax": (da.get("selling_costs") or {}).get("transfer_tax") or 0,
        "_reference": {"flip_net_profit": da.get("rehab_net_profit"),
                       "wholetail_net_profit": da.get("wholetail_net_profit")},
    }


def confidence_from_ty_comps(comp_data: dict) -> dict:
    """ARV confidence as Ty's comping recorded it, with the evidence it was based on."""
    calc = comp_data.get("arv_calculation") or {}
    level = calc.get("confidence_level")
    if not level:
        return {}
    basis = [f"Ty comp_data confidence_level {level}"]
    if calc.get("confidence_band_pct") is not None:
        basis.append(f"band +/-{calc['confidence_band_pct']}%")
    if calc.get("arv_low") and calc.get("arv_high"):
        basis.append(f"range ${calc['arv_low']:,.0f}-${calc['arv_high']:,.0f}")
    if calc.get("confidence_notes"):
        basis.append(str(calc["confidence_notes"]))
    return {"arv_confidence": str(level).upper(), "arv_confidence_basis": "; ".join(basis)}


def load_case(path: Path) -> dict:
    case = json.loads(path.read_text(encoding="utf-8"))
    uw = dict(case.get("underwriting") or {})
    base = {}
    if case.get("ty_rehab_data"):
        base.update(underwriting_from_ty(json.loads((path.parent / case["ty_rehab_data"]).read_text(encoding="utf-8"))))
    if case.get("ty_comp_data"):
        base.update(confidence_from_ty_comps(json.loads((path.parent / case["ty_comp_data"]).read_text(encoding="utf-8"))))
    uw = {**base, **uw}                              # explicit values in the case win
    # Defaults stay provisional until market costs are validated; each one is listed as assumed.
    for key, val, note in (("annual_rate", 0.12, "interest 12%/yr (v3 default)"),
                           ("title_rate", 0.0077, "title 0.77% of price (v3 default, not a quote)"),
                           ("buying_fixed", BUYING_FIXED_DEFAULT,
                            f"buying fixed ${BUYING_FIXED_DEFAULT:,} (v3 default)")):
        if key not in uw:
            uw[key] = val
            uw["_assumed"] = list(uw.get("_assumed", [])) + [note]
    case["underwriting"] = uw
    return case


def _missing(case: dict) -> list[str]:
    uw, out = case["underwriting"], []
    out += [f"underwriting.{k}" for k in UW_REQUIRED if uw.get(k) in (None, "")]
    for k in ("arv_confidence", "rehab_confidence"):
        if uw.get(k) not in (None, "") and str(uw[k]).upper() not in CONFIDENCE:
            out.append(f"underwriting.{k} (HIGH, MODERATE or LOW)")
    if not (case.get("property") or {}).get("verified"):
        out.append("property (verified address)")
    if not (case.get("seller") or {}).get("verified") or not (case.get("seller") or {}).get("names"):
        out.append("seller (verified names)")
    pos = case.get("seller_position") or {}
    for k in ("verified_minimum", "payoff"):
        if pos.get(k) is not None and not pos.get(f"{k}_source"):
            out.append(f"seller_position.{k}_source (how it was verified)")
    return out


def _cost_notes(case: dict, ebm: float) -> list[str]:
    """Material costs v3 does not model, so they are never silently treated as zero."""
    uw, state = case["underwriting"], (case.get("property") or {}).get("state") or "the state"
    pts = uw.get("points_pct") or 0
    if pts:
        r, m, t = uw["annual_rate"] / 12, uw["hold_months"], uw["title_rate"]
        with_pts = (ebm * (1 + r * m + t) - pts * uw["rehab"]) / (1 + r * m + t + pts)
        notes = [f"Lender points excluded (v3 convention). At {pts:.0%} on purchase + rehab, End-Buyer Max "
                 f"would be ${with_pts:,.0f} (${ebm - with_pts:,.0f} lower)."]
    else:
        notes = ["Lender points excluded (v3 convention); none recorded for this deal."]
    notes.append(f"Transfer and recording taxes are not modeled; confirm what applies in {state} with title.")
    if uw.get("_ty_transfer_tax"):
        notes.append(f"Ty's selling costs include ${uw['_ty_transfer_tax']:,.0f} transfer tax; "
                     f"confirm it applies in {state}.")
    return notes


def underwrite(case: dict) -> dict:
    """The one authoritative wholesale calculation. Returns status, numbers, risks and reasons."""
    uw = case["underwriting"]
    tier_key = case.get("fee_tier") or DEFAULT_TIER
    tier = FEE_TIERS[tier_key]
    pos = case.get("seller_position") or {}
    res = {"formula": FORMULA_VERSION, "fee_tier": {"key": tier_key, **tier}, "reasons": [],
           "assumed": list(uw.get("_assumed", [])), "risks": [], "letter_allowed": False}

    missing = _missing(case)
    if missing:
        return {**res, "status": QUALIFY, "missing": missing, "reasons": [
            "Required inputs are missing, unverified or undocumented. Confidence is never defaulted; "
            "record it with the evidence behind it."]}

    ebm = end_buyer_max(uw)
    ceiling = ebm - tier["gross"]
    res.update({"required_profit": round(required_profit(uw["arv"])), "end_buyer_max": round(ebm),
                "seller_ceiling": round(ceiling), "cost_notes": _cost_notes(case, ebm),
                "reference": {"mao_75": round(0.75 * uw["arv"] - uw["rehab"]),
                              **{k: v for k, v in (uw.get("_reference") or {}).items() if v is not None}}})

    conf = {k: str(uw[f"{k}_confidence"]).upper() for k in ("arv", "rehab")}
    if conf["arv"] == conf["rehab"] == "LOW":
        return {**res, "status": QUALIFY, "reasons": [
            "ARV and rehab confidence are both LOW. Get photos, a walkthrough or better comps first."]}
    if ceiling <= 0:
        return {**res, "status": NO_VIABLE,
                "reasons": ["No positive purchase price leaves the end buyer the required profit plus the fee."]}

    # The ask is what the seller said; only a verified minimum or payoff constrains the offer.
    minimum, payoff, ask = pos.get("verified_minimum"), pos.get("payoff"), pos.get("ask")
    for amt, label, key in ((minimum, "verified minimum", "verified_minimum"), (payoff, "lien payoff", "payoff")):
        if amt is not None and amt > ceiling:
            return {**res, "status": BELOW_MIN, "reasons": [
                f"Seller's {label} ${amt:,.0f} is above the ${ceiling:,.0f} ceiling ({pos[f'{key}_source']})."]}

    if minimum is not None:
        offer, method = minimum, "Seller's verified minimum"
    elif pos.get("cash_comp") is not None and pos.get("cash_comp_credible") and pos["cash_comp"] <= ceiling:
        offer, method = pos["cash_comp"], "Credible cash/as-is comps"
    else:
        offer, method = math.floor(OPEN_PCT * ceiling / OPEN_ROUND) * OPEN_ROUND, "90% of ceiling"
    if payoff is not None and offer < payoff:
        offer, method = payoff, f"{method}, raised to the lien payoff"
    if ask is not None and ask < offer:
        offer, method = ask, f"{method}, capped at seller's ask"
    if ask is not None and ask > ceiling:
        res["reasons"].append(f"Seller's ask ${ask:,.0f} is above the ceiling. An ask is not a verified "
                              "minimum, so the below-ask offer is still drafted.")

    price = offer + tier["gross"]
    sens = sensitivity(uw, price)
    risks = [{"code": f"low-{k}-confidence",
              "detail": f"{k.upper()} confidence LOW: {uw[f'{k}_confidence_basis']}"}
             for k in ("arv", "rehab") if conf[k] == "LOW"]
    risks += [{"code": s["code"], "detail": f"{s['scenario']} leaves the end buyer "
               f"${-s['cushion']:,} short of the required profit."} for s in sens if s["verdict"] == "FAIL"]
    if sens[0]["verdict"] == "PASS-THIN":
        risks.append({"code": "thin-base", "detail": f"Base case cushion is only ${sens[0]['cushion']:,}."})
    if res["assumed"]:
        risks.append({"code": "assumed-costs",
                      "detail": "Provisional defaults, not verified market costs: " + "; ".join(res["assumed"])})
    if uw.get("points_pct"):
        risks.append({"code": "unmodeled-costs", "detail": res["cost_notes"][0]})

    return {**res, "status": REVIEW if risks else READY, "risks": risks, "offer": round(offer),
            "offer_method": method, "sensitivity": sens, "breakeven": breakeven(uw, price),
            "letter_allowed": True}


# ---------- approval ----------

def offer_version(case: dict, result: dict) -> str:
    """Hash of everything the approval covers: inputs, terms, price, formula."""
    body = {k: case.get(k) for k in ("property", "seller", "underwriting", "seller_position", "fee_tier", "terms")}
    body.update(price=result.get("offer"), formula=FORMULA_VERSION)
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]


def risk_codes(res: dict) -> list[str]:
    return sorted(r["code"] for r in res.get("risks", []))


def approve(case: dict, out: Path, version: str, price: int, approved_by: str,
            acknowledged: list[str] | tuple = ()) -> dict:
    """Record Kaycie's approval. `approved_by` is a label for the record, not authentication."""
    res = underwrite(case)
    if not res["letter_allowed"] or _missing_terms(case):
        raise SystemExit(f"Cannot approve: status {res['status']}, missing terms {_missing_terms(case)}")
    current = offer_version(case, res)
    if version != current:
        raise SystemExit(f"Version {version} does not match the current offer version {current}.")
    if int(price) != res["offer"]:
        raise SystemExit(f"Price ${int(price):,} does not match the drafted offer ${res['offer']:,}.")
    if sorted(set(acknowledged)) != risk_codes(res):
        raise SystemExit(f"{res['status']}: acknowledge exactly these risks with --acknowledge "
                         f"{','.join(risk_codes(res)) or '(none)'}")
    rec = {"offer_version": current, "price": res["offer"], "terms": case["terms"], "status": res["status"],
           "acknowledged_risks": res["risks"], "approved_by": approved_by,
           "approved_at": dt.datetime.now().isoformat(timespec="seconds")}
    (out / "approval.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return rec


def check_approval(case: dict, out: Path) -> dict:
    res = underwrite(case)
    path = out / "approval.json"
    if not path.exists():
        raise SystemExit("No approval on file. Review the DRAFT and run `approve` first.")
    rec = json.loads(path.read_text(encoding="utf-8"))
    if (rec["offer_version"] != offer_version(case, res) or rec["price"] != res.get("offer")
            or rec["terms"] != case.get("terms") or not res["letter_allowed"]
            or sorted(r["code"] for r in rec.get("acknowledged_risks", [])) != risk_codes(res)):
        raise SystemExit("The case changed since approval (price, terms, inputs or risks). Reapproval required.")
    return res


# ---------- seller-facing letter ----------

FORBIDDEN = ("arv", "after repair", "comp", "comps", "comparable", "rehab", "renovat", "repair",
             "profit", "margin", "fee", "ezrei", "ceiling", "end buyer", "end-buyer", "confidence",
             "flip", "wholetail", "mao", "negotiat", "walk-away", "spread", "underwrit", "investor",
             "equity", "risk", "stress", "cushion", "assum", "assign", "%")


def _missing_terms(case: dict) -> list[str]:
    t = case.get("terms") or {}
    return [f"terms.{k}" for k in TERMS_REQUIRED if t.get(k) in (None, "")]


def letter_lines(case: dict, res: dict) -> list[str]:
    """Seller-facing text, built only from verified names/address, the price and the terms."""
    p, t = case["property"], case["terms"]
    names = " and ".join(case["seller"]["names"])
    addr = f"{p['address']}, {p['city']}, {p['state']} {p['zip']}"
    lines = [dt.date.today().strftime("%B %d, %Y").replace(" 0", " "), "", names, "",
             f"Re: {addr}", "", f"Dear {names},", "",
             f"Thank you for talking with us about {p['address']}. This letter sets out the terms on which "
             f"{t['buyer_name']} would like to buy the property.", "",
             f"Purchase price: ${res['offer']:,}",
             "Condition: purchased as-is. You do not need to clean out or fix anything.",
             f"Closing: on or before {t['closing_days']} days after a signed purchase agreement.",
             f"Earnest money: ${int(t['earnest_money']):,}"]
    if t.get("inspection_days"):
        lines.append(f"Inspection period: {t['inspection_days']} days.")
    if t.get("buyer_pays_closing_costs"):
        lines.append("Closing costs: buyer pays the standard closing costs.")
    lines += [f"This proposal is open until {t['expires_on']}.", "",
              "This letter is a nonbinding statement of interest. It is not a contract and does not "
              "obligate you or us. A binding agreement exists only once both parties sign a written "
              "purchase agreement.", "", "Sincerely,", "", t["signer_name"], t["buyer_name"],
              t["contact_phone"]]
    return lines


def leak_check(text: str, case: dict, res: dict) -> list[str]:
    """Return every internal term or internal dollar figure found in seller-facing text."""
    low, hits = text.lower(), []
    for w in FORBIDDEN:
        if (w in low) if not w.isalpha() else re.search(rf"\b{re.escape(w)}", low):
            hits.append(w)
    allowed = {res["offer"], int(case["terms"]["earnest_money"])}
    for amt in re.findall(r"\$\s?([\d,]+)", text):
        if int(amt.replace(",", "")) not in allowed:
            hits.append(f"${amt}")
    uw = case["underwriting"]
    internal = [uw["arv"], uw["rehab"], uw["holding_total"], uw["selling_total"], res.get("end_buyer_max"),
                res.get("seller_ceiling"), res.get("required_profit"), res["fee_tier"]["gross"],
                res["fee_tier"]["net"], (res.get("reference") or {}).get("mao_75")]
    internal += [s["cushion"] for s in res.get("sensitivity", [])]
    for v in {abs(round(x)) for x in internal if x}:
        if v not in allowed and re.search(rf"(?<![\d,]){v:,}(?![\d,])|(?<!\d){v}(?!\d)", text):
            hits.append(f"internal figure {v:,}")
    return hits


def write_letter_pdf(case: dict, res: dict, path: Path, draft: bool, version: str) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    lines = letter_lines(case, res)
    hits = leak_check("\n".join(lines), case, res)
    if hits:
        raise SystemExit(f"Seller letter blocked, internal content found: {hits}")

    def stamp(c, _doc):
        c.saveState()
        c.setFont("Helvetica", 7)
        c.drawString(inch, 0.5 * inch, f"Ref {version[:8]}")
        if draft:
            c.setFont("Helvetica-Bold", 14)
            c.setFillColorRGB(0.8, 0, 0)
            c.drawString(inch, 10.5 * inch, "DRAFT - FOR INTERNAL REVIEW - DO NOT SEND")
            c.setFont("Helvetica-Bold", 90)
            c.setFillColorRGB(0.85, 0.85, 0.85)
            c.translate(4.25 * inch, 5.5 * inch)
            c.rotate(45)
            c.drawCentredString(0, 0, "DRAFT")
        c.restoreState()

    body = getSampleStyleSheet()["BodyText"]
    flow = [Paragraph(ln, body) if ln else Spacer(1, 8) for ln in lines]
    SimpleDocTemplate(str(path), pagesize=letter, topMargin=1.1 * inch).build(
        flow, onFirstPage=stamp, onLaterPages=stamp)


# ---------- internal packet ----------

def legal_flags(case: dict) -> list[str]:
    state = (case.get("property") or {}).get("state") or "the state"
    flags = [f.format(state=state) for f in LEGAL_FLAGS]
    if state in STATE_LEGAL_FLAGS:
        flags.append(STATE_LEGAL_FLAGS[state])
    days = (case.get("terms") or {}).get("inspection_days")
    if days and days != SOP_DUE_DILIGENCE_DAYS:
        flags.append(f"The Assignment SOP uses a {SOP_DUE_DILIGENCE_DAYS}-day due diligence period; these "
                     f"terms say {days} days. Align them before the letter goes out.")
    return flags


def packet_md(case: dict, res: dict, version: str | None) -> str:
    uw, p, tier = case["underwriting"], case.get("property") or {}, res["fee_tier"]
    m = lambda v: f"{'-' if v < 0 else ''}${abs(v):,.0f}" if isinstance(v, (int, float)) else "-"
    out = [f"# INTERNAL - Wholesale approval packet - {p.get('address', '?')}", "",
           "Not seller-facing. Contains internal underwriting.", "",
           f"**Decision: {res['status']}**", ""]
    out += [f"- {r}" for r in res["reasons"]]
    if res.get("missing"):
        out += ["", "Missing:"] + [f"- {x}" for x in res["missing"]]
    if res.get("risks"):
        out += ["", "## Risks to acknowledge before approval", "",
                "A passing base case is not an unqualified recommendation. Approval needs each code below.", ""]
        out += [f"- `{r['code']}`: {r['detail']}" for r in res["risks"]]
    if res["assumed"]:
        out += ["", "## Provisional defaults (NOT verified market costs)", ""]
        out += [f"- {a}" for a in res["assumed"]]
    out += ["", "## Inputs", "",
            f"ARV {m(uw.get('arv'))} | Rehab {m(uw.get('rehab'))} | Hold {uw.get('hold_months')} mo "
            f"| r {uw['annual_rate']:.0%}/yr | t {uw['title_rate']:.2%}",
            f"Holding {m(uw.get('holding_total'))} | Buying fixed {m(uw['buying_fixed'])} | "
            f"Selling {m(uw.get('selling_total'))}", "",
            f"ARV confidence: {uw.get('arv_confidence') or 'NOT RECORDED'} - "
            f"{uw.get('arv_confidence_basis') or 'no evidence recorded'}",
            f"Rehab confidence: {uw.get('rehab_confidence') or 'NOT RECORDED'} - "
            f"{uw.get('rehab_confidence_basis') or 'no evidence recorded'}"]
    if "end_buyer_max" in res:
        out += ["", f"## Wholesale ({res['formula']})", "",
                f"Required end-buyer profit: {m(res['required_profit'])} (greater of $20,000 or 10% of ARV; "
                "a screening floor, not proof a buyer will take it)",
                f"End-Buyer Max: {m(res['end_buyer_max'])}",
                f"Fee tier: {tier['label']} - gross {m(tier['gross'])}, Bluetail share after EZREIClosings "
                f"{m(tier['net'])} (configurable: fee_tier)",
                f"Seller Ceiling: {m(res['seller_ceiling'])}", "", "Costs not in the formula:"]
        out += [f"- {n}" for n in res["cost_notes"]]
    if res.get("offer") is not None:
        out += ["", f"**Opening offer: {m(res['offer'])}** ({res['offer_method']})", "",
                "| Stress test at offer + fee | Cushion | Verdict |", "|---|---|---|"]
        out += [f"| {s['scenario']} | {m(s['cushion'])} | {s['verdict']} |" for s in res["sensitivity"]]
        b = res["breakeven"]
        pct = lambda v, base: "beyond the tested range" if v is None else f"{v:.1%} (to {m(base)})"
        out += ["", "Break-even, one assumption at a time:",
                f"- ARV can fall {pct(b['arv_drop_pct'], uw['arv'] * (1 - (b['arv_drop_pct'] or 0)))}",
                f"- Rehab can rise {pct(b['rehab_rise_pct'], uw['rehab'] * (1 + (b['rehab_rise_pct'] or 0)))}",
                "- Hold can run " + ("beyond the tested range" if b["extra_months"] is None
                                     else f"{b['extra_months']:.1f} months longer")]
    if res.get("reference"):
        out += ["", "## Reference only (not used to set the offer)", ""]
        out += [f"- {k}: {m(v)}" for k, v in res["reference"].items()]
    pos = case.get("seller_position") or {}
    out += ["", "## Seller position", "",
            f"- Asking price: {m(pos.get('ask'))} (what the seller said; not a minimum)",
            f"- Verified minimum: {m(pos.get('verified_minimum'))} ({pos.get('verified_minimum_source') or '-'})",
            f"- Lien payoff: {m(pos.get('payoff'))} ({pos.get('payoff_source') or '-'})",
            f"- Cash/as-is comps: {m(pos.get('cash_comp'))}"
            + (" (credible)" if pos.get("cash_comp_credible") else "")]
    out += ["", "## For attorney / title review", ""] + [f"- {f}" for f in legal_flags(case)]
    if version:
        ack = f" --acknowledge {','.join(risk_codes(res))}" if res["risks"] else ""
        out += ["", "## Approval", "", f"Offer version: `{version}`", "",
                "Kaycie runs this herself. --approved-by is a label, not authentication, and approving "
                "sends nothing. To approve this exact price and these terms"
                + (", acknowledging every risk above" if ack else "") + ":", "",
                f"    python src/wholesale_offer.py approve CASE --out DIR --version {version} "
                f"--price {res['offer']} --approved-by Kaycie{ack}"]
    return "\n".join(out) + "\n"


def draft(case: dict, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    res = underwrite(case)
    missing_terms = _missing_terms(case) if res["letter_allowed"] else []
    if missing_terms:
        res = {**res, "letter_allowed": False, "missing": missing_terms,
               "reasons": res["reasons"] + ["Letter terms are incomplete; no letter drafted."]}
    version = offer_version(case, res) if res["letter_allowed"] else None
    (out / "packet.md").write_text(packet_md(case, res, version), encoding="utf-8")
    (out / "underwriting.json").write_text(json.dumps({**res, "offer_version": version}, indent=2),
                                           encoding="utf-8")
    for stale in ("offer_letter_DRAFT.pdf", "offer_letter_FINAL.pdf"):
        (out / stale).unlink(missing_ok=True)
    if res["letter_allowed"]:
        write_letter_pdf(case, res, out / "offer_letter_DRAFT.pdf", draft=True, version=version)
    return {**res, "offer_version": version}


def final(case: dict, out: Path) -> Path:
    res = check_approval(case, out)
    path = out / "offer_letter_FINAL.pdf"
    write_letter_pdf(case, res, path, draft=False, version=offer_version(case, res))
    return path


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("draft", "approve", "final"))
    ap.add_argument("case", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--version")
    ap.add_argument("--price", type=int)
    ap.add_argument("--approved-by")
    ap.add_argument("--acknowledge", default="", help="comma-separated risk codes listed in the packet")
    a = ap.parse_args(argv)
    case = load_case(a.case)
    if a.cmd == "draft":
        r = draft(case, a.out)
        print(f"{r['status']}  offer={r.get('offer')}  risks={risk_codes(r)}  version={r['offer_version']}  -> {a.out}")
    elif a.cmd == "approve":
        if not (a.version and a.price and a.approved_by):
            raise SystemExit("approve needs --version, --price and --approved-by")
        ack = [c.strip() for c in a.acknowledge.split(",") if c.strip()]
        print(json.dumps(approve(case, a.out, a.version, a.price, a.approved_by, ack), indent=2))
    else:
        print(final(case, a.out))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
