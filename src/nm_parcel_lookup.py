"""NM property-address lookup for probate decedents (Bernalillo & Sandoval).

For NM probate notices, the notice text contains the PR's mailing address
but NOT the decedent's property address — the same structural gap
documented in CLAUDE.md ("0/24 property addresses — confirmed structural,
not a bug"): probate notices announce an estate proceeding, not a
trustee-sale, and have no legal reason to state a property address. This
module fills that gap using the same 3-tier shape as Knox County's
tax_enricher.py (decedent name -> executor/PR family property -> people
search), but against a different underlying data source: New Mexico's
Office of the State Engineer (OSE) hosts a statewide ArcGIS parcels REST
service (sourced from NM Taxation & Revenue Dept PTD, which aggregates
every county's own CAMA system) rather than a county-run tax API. One
service, two county-specific layers, covers both Bernalillo and Sandoval.

Confirmed live during scoping (2026-08-06): public, unauthenticated,
standard ArcGIS REST `/query` endpoint, supports
`WHERE UPPER(Owner1) LIKE UPPER('%NAME%')`. ~300ms response time,
MaxRecordCount 5000/query (no pagination needed for a name search). Data
is an annual county tax-roll snapshot (2024 TaxYear; Bernalillo's layer
last refreshed ~Feb 2025 per its CamaCurrentTo field) — not real-time,
same staleness class as Knox's own Tax API, so this is a "usually
right, occasionally stale" source rather than a guarantee.

Two caveats found during scoping, both handled below:
- Bernalillo's `Owner1` is formatted "Last First Middle" (no comma);
  Sandoval's is "Last, First Middle" (comma) — a source-data format
  inconsistency between the two counties' underlying CAMA feeds. Commas
  are stripped before token-overlap scoring so this doesn't affect
  matching.
- A small Sandoval sample had blank `SitusAddressAll` despite having
  real owner/geometry data — situs-address coverage may be less complete
  for Sandoval than Bernalillo. `_parse_situs_address()` returns None for
  a blank/unparseable value so Tiers 2/3 remain in play rather than
  writing a garbage address.
"""

import logging
import random
import re
import time

import requests

from notice_parser import NoticeData
from tax_enricher import _name_match_score

logger = logging.getLogger(__name__)

OSE_PARCELS_BASE = (
    "https://gis.ose.nm.gov/server_s/rest/services/Parcels/County_Parcels_2025/MapServer"
)

# notice.county.lower() -> OSE MapServer layer ID. Both layers live in the
# same statewide service (33 NM counties total) — see module docstring.
NM_COUNTY_LAYERS = {
    "bernalillo": 0,
    "sandoval": 23,
}

# Default city used when Tier 1/2 can't supply one (Tier 3 people-search
# fallback, or a situs address that didn't include a city).
_DEFAULT_CITY = {
    "bernalillo": "Albuquerque",
    "sandoval": "Rio Rancho",
}

REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 2.0
REQUEST_TIMEOUT = 15
MIN_SCORE = 0.4

_OUT_FIELDS = "Owner1,OwnerAll,SitusAddressAll,AccountNumber,ParcelOID"

# OSE's SitusAddressAll is a single concatenated field: "{street}  {city} {zip}"
# — two-or-more spaces before the city, one space before the trailing 5-digit
# ZIP. Confirmed against live samples during scoping, e.g.:
#   "5704 Tinnin Rd Nw  Los Ranchos De Albuquerque 87107"
#   "2941 Madison St Ne  Albuquerque 87110"
_SITUS_RE = re.compile(r"^(?P<street>.+?)\s{2,}(?P<city>.+?)\s+(?P<zip>\d{5})$")

_SUFFIX_RE = re.compile(r"\b(?:JR|SR|II|III|IV)\b\.?", re.IGNORECASE)


def _parse_situs_address(raw: str) -> dict | None:
    """Split OSE's concatenated SitusAddressAll into street/city/zip.

    Returns None if raw is blank/whitespace-only (a real, observed case for
    some Sandoval parcels) or doesn't match the expected shape, or has no
    leading house number (e.g. a bare lot description — not a mailable
    street address).
    """
    if not raw or not raw.strip():
        return None
    m = _SITUS_RE.match(raw.strip())
    if not m:
        return None
    street = m.group("street").strip()
    if not re.match(r"^\d", street):
        return None
    return {
        "street": street.title(),
        "city": m.group("city").strip().title(),
        "zip": m.group("zip"),
    }


def _extract_surname_candidates(name: str) -> list[str]:
    """Best-effort surname token(s) to use for the OSE LIKE-query substring.

    The OSE service only supports SQL-style WHERE clauses, not free-text
    search, so we need one real substring to search on (unlike Knox's Tax
    API, which accepts a full name string and does its own matching
    internally). Codebase convention (see tax_enricher._format_name_for_
    search) is that decedent/owner names are usually stored
    "FIRST [MIDDLE] LAST", so the last token is tried first; the first
    token is tried as a fallback for the occasional "LAST FIRST" source
    record.
    """
    clean = _SUFFIX_RE.sub("", name).strip()
    clean = re.sub(r"[.,]", "", clean).strip()
    parts = clean.split()
    if not parts:
        return []
    if len(parts) == 1:
        return [parts[0]]
    candidates = [parts[-1]]
    if parts[0].upper() != parts[-1].upper():
        candidates.append(parts[0])
    return candidates


def _ose_name_query(
    county: str,
    surname: str,
    full_search_name: str = "",
    min_score: float = MIN_SCORE,
) -> list[tuple[float, dict]]:
    """Query the OSE parcels service for a county by owner surname, score results.

    Args:
        county: "bernalillo" or "sandoval" (case-insensitive)
        surname: single surname token used for the LIKE substring search
        full_search_name: full name string to score candidates against via
            tax_enricher._name_match_score's token-overlap (order-
            independent, so this can be the original "FIRST LAST" name).
            Defaults to `surname` if not given.
        min_score: minimum token-overlap score to keep a candidate

    Returns:
        List of (score, parcel_dict) tuples, sorted by score descending.
        parcel_dict keys: owner, parcel_address (raw SitusAddressAll),
        account_number, parcel_oid.
    """
    layer = NM_COUNTY_LAYERS.get(county.lower())
    if layer is None:
        return []

    surname_clean = surname.strip().replace("'", "''")  # escape for the WHERE clause
    if not surname_clean:
        return []

    where = f"UPPER(Owner1) LIKE UPPER('%{surname_clean}%')"
    params = {
        "where": where,
        "outFields": _OUT_FIELDS,
        "resultRecordCount": 25,
        "f": "json",
    }
    url = f"{OSE_PARCELS_BASE}/{layer}/query"

    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.debug("OSE parcels query failed for '%s' (%s): %s", surname_clean, county, e)
        return []

    try:
        data = resp.json()
    except ValueError:
        return []

    if data.get("error"):
        logger.debug("OSE parcels query returned an error for '%s': %s", surname_clean, data["error"])
        return []

    features = data.get("features", [])
    score_against = full_search_name or surname_clean

    scored = []
    for feat in features:
        attrs = feat.get("attributes", {})
        owner = (attrs.get("Owner1") or "").strip()
        if not owner:
            continue
        # Strip Sandoval's "Last, First" comma before scoring — Bernalillo
        # has no comma in Owner1, so this is a no-op there.
        owner_for_scoring = owner.replace(",", "")
        score = _name_match_score(score_against, owner_for_scoring)
        if score >= min_score:
            scored.append((score, {
                "owner": owner,
                "parcel_address": attrs.get("SitusAddressAll", ""),
                "account_number": attrs.get("AccountNumber", ""),
                "parcel_oid": attrs.get("ParcelOID", ""),
            }))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def _apply_parcel_to_notice(notice: NoticeData, parcel: dict, county: str) -> bool:
    """Apply a matched OSE parcel's situs address to a notice.

    Returns True if a usable address was applied, False otherwise (e.g.
    the parcel's situs address was blank or unparseable — see
    _parse_situs_address).
    """
    situs = _parse_situs_address(parcel.get("parcel_address", ""))
    if not situs:
        return False
    notice.address = situs["street"]
    notice.city = situs["city"]
    notice.zip = situs["zip"]
    notice.state = "NM"
    account = parcel.get("account_number", "")
    if account:
        notice.parcel_id = str(account)
    owner = parcel.get("owner", "")
    if owner and not notice.tax_owner_name:
        notice.tax_owner_name = owner
    return True


def _people_search_property_nm(name: str, county: str) -> str | None:
    """Tier 3 fallback: search people-search sites for a decedent's address.

    Reuses obituary_enricher's existing (already-generic) infra, which
    takes city/state as parameters rather than hardcoding a market.
    tax_enricher.py's own Knox version of this tier (_people_search_
    property) imports a `_fetch_page` helper that doesn't actually exist
    in obituary_enricher.py (only `_fetch_page_text` does) — a
    pre-existing bug that silently no-ops Knox's Tier 3 today. Not fixing
    that here (Knox is dormant/legacy, out of scope for this build), but
    using the correct function name in this new module rather than
    copying the bug forward. Worth a follow-up fix in tax_enricher.py.
    """
    city = _DEFAULT_CITY.get(county.lower(), "Albuquerque")
    try:
        from obituary_enricher import _build_people_search_urls, _fetch_page_text

        urls = _build_people_search_urls(name, city, "NM")
        for url in urls[:3]:
            time.sleep(random.uniform(0.5, 1.0))
            text = _fetch_page_text(url)
            if not text or len(text) < 100:
                continue
            addr_pattern = re.compile(
                r"(\d+\s+[\w\s.]+(?:St|Ave|Rd|Dr|Ln|Ct|Blvd|Way|Pl|Cir|Trl|Loop|Run|Ter|Pkwy|"
                r"Nw|Ne|Sw|Se))"
                r"[,.\s]+(?:Albuquerque|Rio Rancho|Corrales|Los Ranchos|Bernalillo|Placitas|"
                r"Cuba|Jemez)",
                re.IGNORECASE,
            )
            matches = addr_pattern.findall(text)
            if matches:
                addr = matches[0].strip()
                logger.info("    People search found address: %s", addr)
                return addr
    except Exception as e:
        logger.debug("  NM people search property lookup failed: %s", e)
    return None


def probate_property_lookup_nm(notices: list[NoticeData]) -> None:
    """Multi-tier property lookup for NM (Bernalillo/Sandoval) probate records.

    Mirrors tax_enricher._probate_property_lookup's 3-tier structure:
      Tier 1: OSE parcels query by decedent name
      Tier 2: OSE parcels query by executor/PR name (family property)
      Tier 3: People search for decedent's last known address

    Modifies notices in-place. No-op for counties other than
    bernalillo/sandoval — should already be pre-filtered by the caller
    (enrichment_pipeline.py's Step 3c), but double-checked here
    defensively since this function may also be called directly (e.g.
    from a test or a future downstream backfill script).
    """
    candidates = [
        n for n in notices
        if n.county.lower() in NM_COUNTY_LAYERS
        and not n.address.strip()
        and n.decedent_name.strip()
    ]
    if not candidates:
        return

    logger.info("NM Probate Property Lookup: %d candidates", len(candidates))
    found = 0

    for notice in candidates:
        county = notice.county.lower()
        decedent = notice.decedent_name.strip()
        executor = notice.owner_name.strip()
        logger.info("  Looking up property for decedent: %s (%s)", decedent, notice.county)

        # ── Tier 1: OSE parcels query by decedent name ──
        best_match = None
        for surname in _extract_surname_candidates(decedent):
            time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
            results = _ose_name_query(county, surname, full_search_name=decedent)
            if results:
                if not best_match or results[0][0] > best_match[0]:
                    best_match = results[0]
                if results[0][0] >= 0.6:
                    break  # good enough, stop searching

        if best_match and best_match[0] >= MIN_SCORE:
            score, parcel = best_match
            logger.info(
                "  Tier 1 (OSE parcels): %s (owner: %s, score: %.2f)",
                parcel.get("parcel_address", ""), parcel.get("owner", ""), score,
            )
            if _apply_parcel_to_notice(notice, parcel, county):
                found += 1
                continue

        # ── Tier 2: OSE parcels query by executor name (family property) ──
        if executor:
            dec_last = decedent.split()[-1].upper() if decedent.split() else ""
            applied = False
            for surname in _extract_surname_candidates(executor)[:2]:
                time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
                results = _ose_name_query(county, surname, full_search_name=executor)
                for score, parcel in results:
                    owner = parcel.get("owner", "")
                    if dec_last and dec_last in owner.upper():
                        logger.info(
                            "  Tier 2 (Executor family): %s (owner: %s, score: %.2f)",
                            parcel.get("parcel_address", ""), owner, score,
                        )
                        if _apply_parcel_to_notice(notice, parcel, county):
                            applied = True
                        break
                if applied:
                    break
            if applied:
                found += 1
                continue

        # ── Tier 3: People search ──
        logger.info("  Tier 3: People search for %s", decedent)
        people_addr = _people_search_property_nm(decedent, county)
        if people_addr:
            notice.address = people_addr
            notice.city = _DEFAULT_CITY.get(county, "Albuquerque")
            notice.state = "NM"
            logger.info("  Tier 3 (People Search): %s", notice.address)
            found += 1
            continue

        logger.warning("  No property found for decedent: %s (all tiers exhausted)", decedent)

    logger.info("NM Probate Property Lookup complete: %d/%d found", found, len(candidates))
