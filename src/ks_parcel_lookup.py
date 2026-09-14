"""Johnson County KS property-address lookup for probate decedents.

Same structural gap as NM/MO (see nm_parcel_lookup.py's and
mo_parcel_lookup.py's module docstrings) and CLAUDE.md's Kansas
validation-drop-rate writeup ("Probate with address: 0/6"-shaped finding,
but for Johnson County specifically: the 2026-08-04 KS historical test
found ~49% of scraped probate notices dropped at the validation gate for
"missing address (no property, PR, or DM address)" — this module is the
lookup tier CLAUDE.md flagged as the fix for that gap ("Kansas has no
equivalent lookup yet ... the fix is building a Johnson-County-specific
address-lookup tier").

Scoped 2026-09-14 alongside Platte and Cass Counties, MO (see
mo_parcel_lookup.py — Platte/Cass are NOT built; see that scoping
writeup and this build's own findings for why). Of the three, only
Johnson County came back live-confirmed feasible.

Unlike NM's OSE service and Clay County MO's ArcGIS FeatureServer (both
a single clean unauthenticated REST `/query` call), Johnson County's
`ims.jocogov.org` (the same ASP.NET WebForms platform family scraper.py
already automates for mopublicnotices.com/newmexicopublicnotices.com)
has no ArcGIS layer with owner data. It does NOT need Playwright/browser
automation, though — confirmed live 2026-09-14 that the site's own
"Search Text" autocomplete is backed by two plain, unauthenticated
HTTP endpoints that a simple `requests` client can call directly:

1. `WebServiceProxy.asmx/ValidateStringAjax` — POST JSON
   `{"prefixText": "<text>"}`, returns a JSON array of candidate
   matches with `Name` ("LAST, FIRST MIDDLE" for individual owners),
   `Value` (an internal parcel/property token — NOT a stable KUPN;
   looks like `"QP53450000 0020"` or similar), and `Type` ("Owner",
   "Address", "Legal Description", or `null` for the site's own
   "Showing first 500 records - please narrow search" sentinel row
   when a prefix is too broad — filtered out below).
2. `ajaxreq.aspx` — POST form `id=<Value from step 1>`, returns XML
   (`<Propinfo><Table>...</Table></Propinfo>`) with the full parcel
   record: `SitAddline1` (situs street), `SitusCity`, `SitusZipCode`,
   `Owner1FullName`/`Owner2FullName` ("LAST, FIRST MIDDLE"),
   `OwnAddLine1`/`OwnAddLine2` (owner mailing address), `KUPN`
   (Kansas Unified Parcel Number), `QuickRefID`.

Confirmed live: no login/disclaimer cookie required for either endpoint
(a `requests.Session` with zero cookies got real data on both), no rate
limiting observed across several rapid sequential calls, case-
insensitive prefix matching. Real Cloudflare (`Server: cloudflare`) sits
in front of the domain but did not block plain unauthenticated POSTs to
either endpoint during scoping.

Two match-quality notes found during scoping, both handled below:
- `ValidateStringAjax` is a **prefix** match on the stored "LAST, FIRST
  MIDDLE" string, not a free-text/contains search (confirmed: searching
  a bare owner surname like "SMITH" or "HANNAN" against `ajaxreq.aspx`
  directly returns nothing — that endpoint only accepts a `Value`
  token, not a name — and `ValidateStringAjax` requires the query to
  start at "LAST"). So Tier 1/2 build the query as
  `"{SURNAME}, {FIRST}"` when a first name is available (this usually
  narrows to a handful of candidates even for a common surname) and
  fall back to a bare surname prefix (which, for a common surname, only
  surfaces the site's own first ~500 results, capped and sorted by its
  own internal order — not alphabetical relevance to our target name,
  so recall on a bare-surname fallback is best-effort only, same
  documented limitation as this module's other tiers).
- A single query can mix personal ("SMITH, CIERA RAQUEL") and business/
  trust owners ("SMITH & LOVELESS, INC. | IP70000000 0011" — note
  the embedded `" | "` + a second parcel token, used by the site for
  multi-parcel commercial accounts). `_name_match_score` naturally
  scores these low against a person's full name, but the entity name
  is stripped to its portion before the pipe before scoring so a
  coincidental token overlap in the trailing parcel-ID text can't
  inflate the score.
"""

import logging
import random
import re
import time

import requests

from notice_parser import NoticeData
from tax_enricher import _name_match_score

logger = logging.getLogger(__name__)

VALIDATE_STRING_URL = "https://ims.jocogov.org/locationservices/WebServiceProxy.asmx/ValidateStringAjax"
AJAX_REQ_URL = "https://ims.jocogov.org/locationservices/ajaxreq.aspx"

_DEFAULT_CITY = "Olathe"  # Johnson County seat; only used if situs/owner city both blank

REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 2.0
REQUEST_TIMEOUT = 15
MIN_SCORE = 0.4
MAX_CANDIDATES_SCORED = 30  # cap work done scoring a broad bare-surname fallback

_SUFFIX_RE = re.compile(r"\b(?:JR|SR|II|III|IV)\b\.?", re.IGNORECASE)


def _extract_name_parts(name: str) -> tuple[str, str]:
    """Split a "FIRST [MIDDLE] LAST" name into (surname, first_name).

    Mirrors nm_parcel_lookup._extract_surname_candidates /
    mo_parcel_lookup._extract_surname_candidates in spirit, but this
    module also needs the first name (not just surname candidates) to
    build Johnson County's "LAST, FIRST" prefix query. Returns
    ("", "") for an empty/unparseable name.
    """
    clean = _SUFFIX_RE.sub("", name).strip()
    clean = re.sub(r"[.,]", "", clean).strip()
    parts = clean.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[-1], parts[0]


def _clean_candidate_name(raw_name: str) -> str:
    """Strip a business/multi-parcel suffix like " | IP70000000 0011" if present."""
    return raw_name.split(" | ")[0].strip()


def _score_name(name: str) -> str:
    """Strip commas before token-overlap scoring.

    Johnson County stores owner names as "LAST, FIRST MIDDLE" — same
    comma-vs-no-comma format inconsistency nm_parcel_lookup.py found
    between Bernalillo and Sandoval. _name_match_score splits on
    whitespace only, so an unstripped "MADL," token would never
    token-match a bare "MADL" from the search name. Commas are stripped
    only for the scoring copy — the raw comma-formatted name is kept
    for storage (tax_owner_name).
    """
    return name.replace(",", "")


def _validate_string_query(prefix_text: str) -> list[dict]:
    """Query ValidateStringAjax for owner-name candidates matching a prefix.

    Returns a list of {"name": ..., "value": ...} dicts for Type=="Owner"
    rows only (skips "Address"/"Legal Description" rows and the site's
    own "Showing first N records" sentinel, which has Type=None).
    """
    prefix_clean = prefix_text.strip()
    if not prefix_clean or len(prefix_clean) < 2:
        return []

    try:
        resp = requests.post(
            VALIDATE_STRING_URL,
            json={"prefixText": prefix_clean},
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.debug("KS ValidateStringAjax query failed for '%s': %s", prefix_clean, e)
        return []

    try:
        data = resp.json()
    except ValueError:
        return []

    items = data.get("d", [])
    results = []
    for item in items:
        if item.get("Type") != "Owner":
            continue
        value = item.get("Value") or ""
        name = item.get("Name") or ""
        if not value or not name:
            continue
        results.append({"name": _clean_candidate_name(name), "value": value})
    return results


def _ajaxreq_lookup(value: str) -> list[dict]:
    """Fetch full parcel record(s) for a ValidateStringAjax `Value` token.

    Returns a list of parcel dicts (usually one, but ajaxreq.aspx can
    return multiple <Table> rows for a shared street-name/address-style
    lookup — kept general even though the Value tokens this module
    passes in are specific parcel IDs that should return exactly one).
    """
    value_clean = value.strip()
    if not value_clean:
        return []

    try:
        resp = requests.post(
            AJAX_REQ_URL,
            data={"id": value_clean},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.debug("KS ajaxreq.aspx query failed for '%s': %s", value_clean, e)
        return []

    text = resp.text or ""
    if "<Table>" not in text:
        return []

    parcels = []
    for block in re.findall(r"<Table>(.*?)</Table>", text, re.DOTALL):
        def field(tag: str) -> str:
            m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.DOTALL)
            return (m.group(1).strip() if m else "")

        owner1 = field("Owner1FullName")
        if not owner1:
            continue
        parcels.append({
            "owner1": owner1,
            "owner2": field("Owner2FullName"),
            "situs_street": field("SitAddline1"),
            "situs_city": field("SitusCity"),
            "situs_zip": field("SitusZipCode"),
            "kupn": field("KUPN"),
            "quick_ref_id": field("QuickRefID"),
        })
    return parcels


def _ks_name_search(surname: str, first_name: str = "", full_search_name: str = "",
                     min_score: float = MIN_SCORE) -> list[tuple[float, dict]]:
    """Search Johnson County by name, resolve each candidate to a full parcel, score them.

    Tries the narrow "SURNAME, FIRST" prefix first (usually a handful of
    hits even for a common surname); falls back to a bare surname prefix
    if that returns nothing (broader recall, but see module docstring's
    "best-effort only" caveat for common surnames — capped at
    MAX_CANDIDATES_SCORED to avoid resolving hundreds of candidates one
    ajaxreq.aspx call at a time).

    Returns a list of (score, parcel_dict) tuples sorted by score
    descending. parcel_dict keys: owner, parcel_address (situs street),
    situs_city, situs_zip, parcel_id (KUPN), quick_ref_id.
    """
    if not surname:
        return []

    candidates = []
    if first_name:
        candidates = _validate_string_query(f"{surname}, {first_name}")
    if not candidates:
        candidates = _validate_string_query(surname)

    if not candidates:
        return []

    score_against = _score_name(full_search_name or f"{first_name} {surname}".strip())
    scored = []
    for cand in candidates[:MAX_CANDIDATES_SCORED]:
        score = _name_match_score(score_against, _score_name(cand["name"]))
        if score < min_score:
            continue
        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
        parcels = _ajaxreq_lookup(cand["value"])
        for parcel in parcels:
            owner_combined = _score_name(f"{parcel['owner1']} {parcel['owner2']}".strip())
            parcel_score = max(score, _name_match_score(score_against, owner_combined))
            scored.append((parcel_score, {
                "owner": parcel["owner1"],
                "owner2": parcel["owner2"],
                "parcel_address": parcel["situs_street"],
                "situs_city": parcel["situs_city"],
                "situs_zip": parcel["situs_zip"],
                "parcel_id": parcel["kupn"] or parcel["quick_ref_id"],
                "quick_ref_id": parcel["quick_ref_id"],
            }))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def _apply_parcel_to_notice(notice: NoticeData, parcel: dict) -> bool:
    """Apply a matched Johnson County parcel's situs address to a notice.

    Returns True if a usable street address was applied, False
    otherwise (blank situs street — e.g. personal property or a bad
    source record).
    """
    street = parcel.get("parcel_address", "").strip()
    if not street:
        return False
    notice.address = street.title()
    notice.city = (parcel.get("situs_city") or "").strip().title() or _DEFAULT_CITY
    zip_code = (parcel.get("situs_zip") or "").strip()
    if zip_code:
        notice.zip = zip_code
    notice.state = "KS"
    parcel_id = parcel.get("parcel_id", "")
    if parcel_id:
        notice.parcel_id = str(parcel_id)
    owner = parcel.get("owner", "")
    if owner and not notice.tax_owner_name:
        notice.tax_owner_name = owner
    return True


def _people_search_property_ks(name: str) -> str | None:
    """Tier 3 fallback: search people-search sites for a decedent's address.

    Same shape as nm_parcel_lookup._people_search_property_nm /
    mo_parcel_lookup._people_search_property_mo — reuses
    obituary_enricher's generic (city/state-parameterized) infra, and
    uses the correct `_fetch_page_text` name rather than the nonexistent
    `_fetch_page` tax_enricher.py's own Knox Tier 3 imports (a pre-
    existing, not-fixed-here bug — see nm_parcel_lookup.py's note).
    """
    try:
        from obituary_enricher import _build_people_search_urls, _fetch_page_text

        urls = _build_people_search_urls(name, _DEFAULT_CITY, "KS")
        for url in urls[:3]:
            time.sleep(random.uniform(0.5, 1.0))
            text = _fetch_page_text(url)
            if not text or len(text) < 100:
                continue
            addr_pattern = re.compile(
                r"(\d+\s+[\w\s.]+(?:St|Ave|Rd|Dr|Ln|Ct|Blvd|Way|Pl|Cir|Trl|Loop|Run|Ter|Pkwy|"
                r"Nw|Ne|Sw|Se))"
                r"[,.\s]+(?:Olathe|Overland Park|Shawnee|Lenexa|Leawood|Gardner|Spring Hill|"
                r"De Soto|Mission|Prairie Village)",
                re.IGNORECASE,
            )
            matches = addr_pattern.findall(text)
            if matches:
                addr = matches[0].strip()
                logger.info("    People search found address: %s", addr)
                return addr
    except Exception as e:
        logger.debug("  KS people search property lookup failed: %s", e)
    return None


def probate_property_lookup_ks(notices: list[NoticeData]) -> None:
    """Multi-tier property lookup for Johnson County KS probate records.

    Mirrors nm_parcel_lookup.probate_property_lookup_nm's /
    mo_parcel_lookup.probate_property_lookup_mo's 3-tier structure:
      Tier 1: county parcels query by decedent name
      Tier 2: county parcels query by executor/PR name (family property)
      Tier 3: People search for decedent's last known address

    Modifies notices in-place. No-op for counties other than johnson —
    should already be pre-filtered by the caller (enrichment_pipeline.
    py's Step 3c), but double-checked here defensively.
    """
    candidates = [
        n for n in notices
        if n.county.lower() == "johnson"
        and not n.address.strip()
        and n.decedent_name.strip()
    ]
    if not candidates:
        return

    logger.info("KS Probate Property Lookup: %d candidates", len(candidates))
    found = 0

    for notice in candidates:
        decedent = notice.decedent_name.strip()
        executor = notice.owner_name.strip()
        logger.info("  Looking up property for decedent: %s (%s)", decedent, notice.county)

        # ── Tier 1: parcels query by decedent name ──
        dec_surname, dec_first = _extract_name_parts(decedent)
        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
        results = _ks_name_search(dec_surname, dec_first, full_search_name=decedent)

        if results and results[0][0] >= MIN_SCORE:
            score, parcel = results[0]
            logger.info(
                "  Tier 1 (County parcels): %s (owner: %s, score: %.2f)",
                parcel.get("parcel_address", ""), parcel.get("owner", ""), score,
            )
            if _apply_parcel_to_notice(notice, parcel):
                found += 1
                continue

        # ── Tier 2: parcels query by executor name (family property) ──
        if executor:
            exec_surname, exec_first = _extract_name_parts(executor)
            dec_last = dec_surname.upper()
            time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
            exec_results = _ks_name_search(exec_surname, exec_first, full_search_name=executor)
            applied = False
            for score, parcel in exec_results:
                owner_combined = f"{parcel.get('owner', '')} {parcel.get('owner2', '')}".upper()
                if dec_last and dec_last in owner_combined:
                    logger.info(
                        "  Tier 2 (Executor family): %s (owner: %s / %s, score: %.2f)",
                        parcel.get("parcel_address", ""), parcel.get("owner", ""),
                        parcel.get("owner2", ""), score,
                    )
                    if _apply_parcel_to_notice(notice, parcel):
                        applied = True
                    break
            if applied:
                found += 1
                continue

        # ── Tier 3: People search ──
        logger.info("  Tier 3: People search for %s", decedent)
        people_addr = _people_search_property_ks(decedent)
        if people_addr:
            notice.address = people_addr
            notice.city = _DEFAULT_CITY
            notice.state = "KS"
            logger.info("  Tier 3 (People Search): %s", notice.address)
            found += 1
            continue

        logger.warning("  No property found for decedent: %s (all tiers exhausted)", decedent)

    logger.info("KS Probate Property Lookup complete: %d/%d found", found, len(candidates))
