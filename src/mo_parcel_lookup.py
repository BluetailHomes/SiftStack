"""MO property-address lookup for probate decedents.

Same structural gap as NM (see nm_parcel_lookup.py's module docstring)
and CLAUDE.md's "0/24 property addresses" note: probate notices name the
decedent and PR/executor but never state the property address, since
probate is an estate proceeding, not a trustee sale. Confirmed live for
Missouri 2026-08-09 (CLAUDE.md's "High-Potential Lead Filter" scoping
follow-up): a real scheduled MO run showed "Probate with address: 0/6"
and "Property lookup complete: 0 found, 0 not found, 8 skipped" — MO's
gap is currently WORSE than NM's was pre-tier (0% vs NM's 0/24), since MO
has never had any lookup tier at all.

Scoped 2026-08-09 across all 4 live MO counties (Jackson, Clay, Platte,
Cass) — only Clay is implemented here. Findings from that scoping pass,
kept for context on why the other 3 aren't in MO_COUNTY_SOURCES yet:
  - Jackson: the top web-search hit for a "Jackson County parcel REST
    API" (gis.mijackson.org) turned out to be Jackson County, MICHIGAN
    (confirmed via a live query returning O_STATE: 'MI', city 'Jackson,
    MI 49201') — a reminder that "Jackson County" is not unique to
    Missouri and search-result county/state matching needs to be
    verified against real query data, not just a plausible-looking
    domain name. Jackson County, MO's real GIS server
    (jcgis.jacksongov.org) is legitimate, but its public ArcGIS parcel
    layers found so far (ParcelsAscendRelate, LotsAndDimensions) carry
    geometry/tax-district data only, no owner name or mailing address
    fields. Owner data likely lives behind a separate assessor portal
    (publicaccess.jacksongov.org) — not yet confirmed whether that has a
    usable API.
  - Platte: runs on a Schneider Corp/Beacon-style assessor portal
    (beacon.schneidercorp.com) — a direct unauthenticated request
    returned a flat HTTP 403, suggesting it needs a browser session
    rather than exposing a public REST query like Clay's.
  - Cass: similar shape — a missouriassessors.com search form
    (cass.missouriassessors.com/search.php) with no obvious API endpoint
    found in the page source.
  Both would likely need the same kind of browser-automation approach
  as Knox's Tier 3 people-search fallback rather than a clean ArcGIS
  `/query` call. Not built here — flagged as follow-on scoping work.

Clay County: confirmed live 2026-08-09, public unauthenticated ArcGIS
FeatureServer (services7.arcgis.com — hosted, not a county-run server,
unlike Jackson's), supports `WHERE UPPER(current_owner) LIKE
UPPER('%NAME%')`. Verified against real Kansas City, MO addresses
(owner_zip 64155 — one of config.TARGET_ZIPS' actual Clay County
entries).

One real data-quality gap found during scoping, handled below: Clay's
situs (property) fields carry a house number and street name
(situs_num, situs_st_name, etc.) but `situs_city` and `situs_zip` were
NULL across every sample checked (20/20 in the scoping pass) — unlike
NM's OSE service, which had a single concatenated, mostly-complete
SitusAddressAll field. Rather than guess a city, `_apply_parcel_to_
notice()` deliberately leaves notice.city set from `owner_city` (real,
populated per-parcel data — the PR/owner's mailing city is usually
close to the property, though not guaranteed) as a best-effort start
point and leaves notice.zip BLANK. This is safe by design: Step 6
(Smarty address standardization) runs later in the pipeline and re-
derives city/zip from the real street address regardless — see
address_standardizer.py's `_build_lastline()`, which already tolerates
a missing zip (falls back to "city, state" as the Smarty lastline) and
`standardize_addresses()`, which overwrites notice.city/state/zip from
Smarty's own matched components. So an approximate/blank city here is
corrected downstream, not shipped as-is.
"""

import logging
import random
import re
import time

import requests

from notice_parser import NoticeData
from tax_enricher import _name_match_score

logger = logging.getLogger(__name__)

# notice.county.lower() -> per-county ArcGIS FeatureServer query URL.
# Only Clay is live today — see module docstring for Jackson/Platte/Cass
# scoping notes. Adding a county here is the only change needed to bring
# it online, provided its source uses the same current_owner/co_owner/
# situs_* field shape; a source with different field names would need
# its own small adapter rather than reusing _clay_query_url's outFields.
MO_COUNTY_SOURCES = {
    "clay": (
        "https://services7.arcgis.com/3c8lLdmDNevrTlaV/ArcGIS/rest/"
        "services/ClayCountyParcelService/FeatureServer/0/query"
    ),
}

# Fallback city used only when both situs_city AND owner_city are blank
# (rare in scoping samples, but not zero). All of Clay County's target
# ZIPs (config.TARGET_ZIPS) are Kansas City metro / Northland addresses,
# and Kansas City was the dominant owner_city value in the scoping
# sample — a reasonable last-resort default, always subject to Smarty
# correction downstream (see module docstring).
_DEFAULT_CITY = {
    "clay": "Kansas City",
}

REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 2.0
REQUEST_TIMEOUT = 15
MIN_SCORE = 0.4

_OUT_FIELDS = (
    "parcel_id,current_owner,co_owner,owner_addr_1,owner_addr_2,"
    "owner_city,owner_state,owner_zip,situs_num,situs_street_prefx,"
    "situs_st_name,situs_street_sufix,situs_city,situs_zip"
)

_SUFFIX_RE = re.compile(r"\b(?:JR|SR|II|III|IV)\b\.?", re.IGNORECASE)


def _extract_surname_candidates(name: str) -> list[str]:
    """Best-effort surname token(s) to use for the ArcGIS LIKE-query substring.

    Same approach as nm_parcel_lookup._extract_surname_candidates — the
    ArcGIS service only supports SQL-style WHERE clauses, not free-text
    search, so we need one real substring to search on. Names are usually
    stored "FIRST [MIDDLE] LAST" in this codebase, so the last token is
    tried first; the first token is tried as a fallback for the
    occasional "LAST FIRST" source record.
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


def _build_situs_street(attrs: dict) -> str:
    """Assemble a street address from Clay's separate situs_* fields.

    Clay splits the street into num/prefix/name/suffix rather than one
    concatenated field (contrast NM's single SitusAddressAll) — join the
    non-blank parts with a space, title-cased for presentation
    consistency with the rest of the codebase.
    """
    parts = [
        attrs.get("situs_num"),
        attrs.get("situs_street_prefx"),
        attrs.get("situs_st_name"),
        attrs.get("situs_street_sufix"),
    ]
    street = " ".join(p.strip() for p in parts if p and p.strip())
    return street.title() if street else ""


def _mo_name_query(
    county: str,
    surname: str,
    full_search_name: str = "",
    min_score: float = MIN_SCORE,
) -> list[tuple[float, dict]]:
    """Query a MO county's ArcGIS parcels service by owner surname, score results.

    Args:
        county: county name, lowercase (e.g. "clay") — must be a key in
            MO_COUNTY_SOURCES.
        surname: single surname token used for the LIKE substring search.
        full_search_name: full name string to score candidates against via
            tax_enricher._name_match_score's token-overlap (order-
            independent). Defaults to `surname` if not given.
        min_score: minimum token-overlap score to keep a candidate.

    Returns:
        List of (score, parcel_dict) tuples, sorted by score descending.
        parcel_dict keys: owner, co_owner, parcel_id, situs_street,
        situs_city, situs_zip, owner_city, owner_state, owner_zip.
    """
    url = MO_COUNTY_SOURCES.get(county.lower())
    if not url:
        return []

    surname_clean = surname.strip().replace("'", "''")  # escape for the WHERE clause
    if not surname_clean:
        return []

    where = f"UPPER(current_owner) LIKE UPPER('%{surname_clean}%')"
    params = {
        "where": where,
        "outFields": _OUT_FIELDS,
        "resultRecordCount": 25,
        "f": "json",
    }

    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.debug("MO parcels query failed for '%s' (%s): %s", surname_clean, county, e)
        return []

    try:
        data = resp.json()
    except ValueError:
        return []

    if data.get("error"):
        logger.debug("MO parcels query returned an error for '%s': %s", surname_clean, data["error"])
        return []

    features = data.get("features", [])
    score_against = full_search_name or surname_clean

    scored = []
    for feat in features:
        attrs = feat.get("attributes", {})
        owner = (attrs.get("current_owner") or "").strip()
        if not owner:
            continue
        score = _name_match_score(score_against, owner)
        if score >= min_score:
            scored.append((score, {
                "owner": owner,
                "co_owner": (attrs.get("co_owner") or "").strip(),
                "parcel_id": attrs.get("parcel_id", ""),
                "situs_street": _build_situs_street(attrs),
                "situs_city": (attrs.get("situs_city") or "").strip(),
                "situs_zip": (attrs.get("situs_zip") or "").strip(),
                "owner_city": (attrs.get("owner_city") or "").strip(),
                "owner_state": (attrs.get("owner_state") or "").strip(),
                "owner_zip": (attrs.get("owner_zip") or "").strip(),
            }))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def _apply_parcel_to_notice(notice: NoticeData, parcel: dict, county: str) -> bool:
    """Apply a matched MO parcel's situs address to a notice.

    Returns True if a usable street address was applied, False otherwise
    (no situs street on this parcel — happens for vacant/unimproved
    parcels or bad source data).

    City/zip handling: see module docstring's "data-quality gap" note.
    situs_city/situs_zip are typically blank in Clay's data, so this
    prefers situs_city when present, falls back to the parcel's own
    owner_city (real per-parcel data, usually nearby), and only reaches
    for the hardcoded _DEFAULT_CITY as a last resort. notice.zip is left
    blank when situs_zip is blank — Step 6 (Smarty) re-derives it from
    the real street address downstream rather than this module guessing.
    """
    street = parcel.get("situs_street", "")
    if not street:
        return False
    notice.address = street
    notice.city = (
        parcel.get("situs_city")
        or parcel.get("owner_city")
        or _DEFAULT_CITY.get(county.lower(), "")
    )
    if parcel.get("situs_zip"):
        notice.zip = parcel["situs_zip"]
    notice.state = "MO"
    parcel_id = parcel.get("parcel_id", "")
    if parcel_id:
        notice.parcel_id = str(parcel_id)
    owner = parcel.get("owner", "")
    if owner and not notice.tax_owner_name:
        notice.tax_owner_name = owner
    return True


def _people_search_property_mo(name: str, county: str) -> str | None:
    """Tier 3 fallback: search people-search sites for a decedent's address.

    Same shape as nm_parcel_lookup._people_search_property_nm — reuses
    obituary_enricher's generic (city/state-parameterized) infra, and
    uses the correct `_fetch_page_text` name rather than the nonexistent
    `_fetch_page` tax_enricher.py's own Knox Tier 3 imports (a pre-
    existing, not-fixed-here bug — see nm_parcel_lookup.py's note).
    """
    city = _DEFAULT_CITY.get(county.lower(), "Kansas City")
    try:
        from obituary_enricher import _build_people_search_urls, _fetch_page_text

        urls = _build_people_search_urls(name, city, "MO")
        for url in urls[:3]:
            time.sleep(random.uniform(0.5, 1.0))
            text = _fetch_page_text(url)
            if not text or len(text) < 100:
                continue
            addr_pattern = re.compile(
                r"(\d+\s+[\w\s.]+(?:St|Ave|Rd|Dr|Ln|Ct|Blvd|Way|Pl|Cir|Trl|Loop|Run|Ter|Pkwy|"
                r"Nw|Ne|Sw|Se))"
                r"[,.\s]+(?:Kansas City|Liberty|Excelsior Springs|Smithville|Gladstone|"
                r"North Kansas City|Pleasant Valley)",
                re.IGNORECASE,
            )
            matches = addr_pattern.findall(text)
            if matches:
                addr = matches[0].strip()
                logger.info("    People search found address: %s", addr)
                return addr
    except Exception as e:
        logger.debug("  MO people search property lookup failed: %s", e)
    return None


def probate_property_lookup_mo(notices: list[NoticeData]) -> None:
    """Multi-tier property lookup for MO probate records (Clay County only today).

    Mirrors nm_parcel_lookup.probate_property_lookup_nm's 3-tier structure:
      Tier 1: county parcels query by decedent name
      Tier 2: county parcels query by executor/PR name (family property),
              checking both current_owner and co_owner for the decedent's
              surname — Clay's separate co_owner field gives this tier
              more precision than NM's single concatenated OwnerAll.
      Tier 3: People search for decedent's last known address

    Modifies notices in-place. No-op for counties not in
    MO_COUNTY_SOURCES (i.e. everything except Clay today) — should
    already be pre-filtered by the caller (enrichment_pipeline.py's
    Step 3c), but double-checked here defensively.
    """
    candidates = [
        n for n in notices
        if n.county.lower() in MO_COUNTY_SOURCES
        and not n.address.strip()
        and n.decedent_name.strip()
    ]
    if not candidates:
        return

    logger.info("MO Probate Property Lookup: %d candidates", len(candidates))
    found = 0

    for notice in candidates:
        county = notice.county.lower()
        decedent = notice.decedent_name.strip()
        executor = notice.owner_name.strip()
        logger.info("  Looking up property for decedent: %s (%s)", decedent, notice.county)

        # ── Tier 1: parcels query by decedent name ──
        best_match = None
        for surname in _extract_surname_candidates(decedent):
            time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
            results = _mo_name_query(county, surname, full_search_name=decedent)
            if results:
                if not best_match or results[0][0] > best_match[0]:
                    best_match = results[0]
                if results[0][0] >= 0.6:
                    break  # good enough, stop searching

        if best_match and best_match[0] >= MIN_SCORE:
            score, parcel = best_match
            logger.info(
                "  Tier 1 (County parcels): %s (owner: %s, score: %.2f)",
                parcel.get("situs_street", ""), parcel.get("owner", ""), score,
            )
            if _apply_parcel_to_notice(notice, parcel, county):
                found += 1
                continue

        # ── Tier 2: parcels query by executor name (family property) ──
        if executor:
            dec_last = decedent.split()[-1].upper() if decedent.split() else ""
            applied = False
            for surname in _extract_surname_candidates(executor)[:2]:
                time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
                results = _mo_name_query(county, surname, full_search_name=executor)
                for score, parcel in results:
                    owner_combined = f"{parcel.get('owner', '')} {parcel.get('co_owner', '')}".upper()
                    if dec_last and dec_last in owner_combined:
                        logger.info(
                            "  Tier 2 (Executor family): %s (owner: %s / co-owner: %s, score: %.2f)",
                            parcel.get("situs_street", ""), parcel.get("owner", ""),
                            parcel.get("co_owner", ""), score,
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
        people_addr = _people_search_property_mo(decedent, county)
        if people_addr:
            notice.address = people_addr
            notice.city = _DEFAULT_CITY.get(county, "Kansas City")
            notice.state = "MO"
            logger.info("  Tier 3 (People Search): %s", notice.address)
            found += 1
            continue

        logger.warning("  No property found for decedent: %s (all tiers exhausted)", decedent)

    logger.info("MO Probate Property Lookup complete: %d/%d found", found, len(candidates))
