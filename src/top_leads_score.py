"""Top Leads score: list overlap + distress evidence, equity, P1 neighborhood and price band.

Kaycie approved these points as drafted (2026-11-12); they stand in for DataSift's AI score, which is
null on every record (no AI plan). Read-only: takes a property detail from
GET /api/internal/property/{uuid}/ and returns a number, never writes anything.

Each distress signal counts once, however many tags / user lists / DataSift system lists carry it.
Unknown equity scores 0 and is kept (never drop a lead on missing data).
"""
from __future__ import annotations

import re

import config

# signal -> (points, pattern matched against tags, user list titles and DataSift system lists)
SIGNALS = {
    "foreclosure":    (5, r"pre-?foreclosure|notice of default|lis pendens|trustee sale|foreclos"),
    "probate":        (4, r"probate|pre-probate|deceased"),
    "obituary":       (4, r"\bobituary\b|\bobit "),
    "tax_delinquent": (4, r"tax delinquen"),
    "vacant":         (3, r"vacant"),
    "code_or_lien":   (3, r"code violation|judgment lien|mechanic|hoa lien|zombie"),
    "estate_sale":    (2, r"estate sale"),
    "tired_landlord": (2, r"tired landlord"),
    "absentee":       (1, r"absentee|out-of-state"),
    "senior":         (1, r"senior"),
    "low_credit":     (1, r"bad credit|low credit"),
    "low_income":     (1, r"low income"),
}
STALE_OBIT_POINTS = 2        # only "Obit 6mo+" evidence (no "Obit under 3mo" / "Obit 3-6mo" tag)
STACK_BONUS = 2              # two or more signals worth 3+ points
P1_NBHD_POINTS = 2
PRICE_BAND = (80_000, 150_000)


def _titles(items) -> list[str]:
    return [t if isinstance(t, str) else (t.get("title") or t.get("name") or "") for t in (items or [])]


def score(d: dict, county: str = "Jackson") -> dict:
    """{'score', 'signals', 'equity_points', 'nbhd_points', 'band_points', 'stack_bonus'} for one detail."""
    tags = _titles(d.get("tags"))
    texts = tags + _titles(d.get("lists")) + _titles(d.get("system_lists"))
    pts = {}
    for name, (p, pat) in SIGNALS.items():
        if any(re.search(pat, t.lower()) for t in texts):
            pts[name] = p
    if "obituary" in pts and "Obit 6mo+" in tags and not {"Obit under 3mo", "Obit 3-6mo"} & set(tags):
        pts["obituary"] = STALE_OBIT_POINTS
    stack = STACK_BONUS if sum(1 for n in pts if SIGNALS[n][0] >= 3) >= 2 else 0

    try:
        eq = float(d["equity_percent"]) if d.get("equity_percent") not in (None, "") else None
    except (TypeError, ValueError):
        eq = None
    if "Free & Clear" in texts or (eq is not None and eq >= 50):
        eqp = 3
    elif eq is not None and eq >= 30:
        eqp = 1
    elif eq is None:
        eqp = 0
    else:
        eqp = -2

    nbhds = {n.lower() for n in config.TARGET_NEIGHBORHOODS.get(county, ())}
    nbp = P1_NBHD_POINTS if (d.get("neighborhood") or "").lower() in nbhds else 0
    try:
        val = float(d["estimate_value"]) if d.get("estimate_value") not in (None, "") else None
    except (TypeError, ValueError):
        val = None
    band = 1 if val is not None and PRICE_BAND[0] <= val <= PRICE_BAND[1] else 0

    return {"score": sum(pts.values()) + stack + eqp + nbp + band,
            "signals": sorted(pts, key=lambda n: -pts[n]), "stack_bonus": stack,
            "equity_points": eqp, "nbhd_points": nbp, "band_points": band}
