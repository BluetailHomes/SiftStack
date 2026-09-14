"""Cass County, MO property-address lookup for probate decedents.

Same structural gap as Clay/NM/Johnson (see mo_parcel_lookup.py's module
docstring) — probate notices name the decedent and PR/executor but never
state the property address. mo_parcel_lookup.py's own scoping notes
(2026-08-09) flagged Cass as "a missouriassessors.com search form
(cass.missouriassessors.com/search.php) with no obvious API endpoint found
in the page source" and left it unbuilt.

Confirmed live 2026-09-14: there IS a usable endpoint, it just isn't a
clean REST API — `cass.missouriassessors.com/results.php` (a Vanguard
Appraisals "VCS Real Estate Website" product) accepts a plain GET with
`mode=basic&history=-1&ideed=<SURNAME>` (deed-holder substring search) and
returns a real HTML results table (parcel number, deed holder name,
address, city) — no login, no CAPTCHA. The site's own search FORM makes
this easy to miss: `ideed`'s `list="deedHolder_array"` attribute binds it
to a giant embedded `<datalist>` enumerating literally every owner name in
the county (5.5MB on its own) for browser autocomplete — a direct
`results.php?ideed=NAME` request without the required `mode`/`history`
hidden-field values returns that same giant datalist-bearing search PAGE
again, not filtered results, which looks like "no results" at a glance if
you don't also send those two fields.

**robots.txt explicitly disallows `/results.php` for `User-agent: *`** —
confirmed during scoping, and this was flagged to Kaycie as a policy
question before building. Decision (2026-09-14): build it anyway — this is
public, unauthenticated county assessor data with no login/CAPTCHA/ToS
click-through, in the same category of public-record lookup this codebase
already automates for Knox (tax_enricher.py), NM (nm_parcel_lookup.py),
Clay (mo_parcel_lookup.py), and Johnson (ks_parcel_lookup.py) — none of
which are REST APIs published for third-party consumption either, they're
just public search forms. If Cass's policy stance changes or this proves
disruptive to their server, this module should be revisited.

Kept as its own module (not folded into mo_parcel_lookup.py) since the two
counties' underlying data shapes are unrelated — Clay is a clean ArcGIS
FeatureServer JSON query with separate owner/co_owner/situs_* fields; Cass
is an HTML table scrape with one combined "deed holder" string per parcel
and no separate co-owner field — reusing mo_parcel_lookup.py's
ArcGIS-specific `_mo_name_query()` doesn't fit here, and duplicating this
module's small amount of unique parsing logic into that one would make
both harder to follow. Wired into enrichment_pipeline.py's Step 3c as its
own independent branch, alongside Knox/NM/Clay/Johnson — see that file.
"""

import logging
import random
import re
import time

import requests

from notice_parser import NoticeData
from tax_enricher import _name_match_score

logger = logging.getLogger(__name__)

CASS_RESULTS_URL = "https://cass.missouriassessors.com/results.php"
REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 2.0
REQUEST_TIMEOUT = 15
MIN_SCORE = 0.4

_SUFFIX_RE = re.compile(r"\b(?:JR|SR|II|III|IV)\b\.?", re.IGNORECASE)

# One result row's worth of fields, in the order they appear in
# id="resultsWrapper"'s <tr> markup — confirmed live 2026-09-14. re.DOTALL
# since each field's own value can (rarely) span whitespace/newlines in the
# source HTML; kept non-greedy throughout to stop at the next tag.
_ROW_RE = re.compile(
    r'<a href="parcel\.php\?gid=(\d+)">([^<]+)</a>.*?'
    r'class="mobileHide">(.*?)<br\s*/?>\s*</td>.*?'
    r'<td>\s*(.*?)\s*<br\s*/?>\s*(.*?)\s*</td>',
    re.DOTALL,
)


def _extract_surname_candidates(name: str) -> list[str]:
    """Same approach as mo_parcel_lookup._extract_surname_candidates —
    Cass's search only supports a substring match, not free text, so we
    need one real token to search on. Duplicated rather than imported
    since this module is deliberately self-contained (see module
    docstring); kept in sync with that function's logic on purpose.
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


def _cass_name_query(surname: str, full_search_name: str = "") -> list[tuple[float, dict]]:
    """Query Cass County's deed-holder search by surname substring, score results.

    Returns list of (score, parcel_dict) tuples sorted by score descending.
    parcel_dict keys: owner (the full deed-holder string — may combine
    multiple names, e.g. "ALLEN & CHERYL SMITH TR"), parcel_id, situs_street,
    situs_city.
    """
    surname_clean = surname.strip()
    if not surname_clean:
        return []

    params = {
        "mode": "basic", "history": "-1",
        "ipin": "", "idba": "", "ideed": surname_clean, "ihnum": "",
        "iaddr": "", "ilegal": "", "iacre1": "", "iacre2": "", "iphoto": "0",
    }

    try:
        resp = requests.get(
            CASS_RESULTS_URL, params=params, timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.debug("Cass parcel search failed for '%s': %s", surname_clean, e)
        return []

    score_against = full_search_name or surname_clean
    scored = []
    for gid, parcel_id, owner_html, street, city in _ROW_RE.findall(resp.text):
        owner = re.sub(r"&amp;", "&", owner_html).strip()
        if not owner:
            continue
        score = _name_match_score(score_against, owner)
        if score >= MIN_SCORE:
            scored.append((score, {
                "owner": owner,
                "parcel_id": parcel_id.strip(),
                "situs_street": re.sub(r"\s+", " ", street).strip().title(),
                "situs_city": re.sub(r"\s+", " ", city).strip().title(),
            }))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def _apply_parcel_to_notice(notice: NoticeData, parcel: dict) -> bool:
    """Apply a matched Cass parcel's situs address to a notice.

    Returns True if a usable street address was applied. Unlike Clay,
    Cass's results always carry a city alongside the street (confirmed
    live — the "Address / City" column is a single required pair) so
    there's no blank-city fallback chain needed here.
    """
    street = parcel.get("situs_street", "")
    if not street:
        return False
    notice.address = street
    notice.city = parcel.get("situs_city", "")
    notice.state = "MO"
    parcel_id = parcel.get("parcel_id", "")
    if parcel_id:
        notice.parcel_id = parcel_id
    owner = parcel.get("owner", "")
    if owner and not notice.tax_owner_name:
        notice.tax_owner_name = owner
    return True


def _people_search_property_cass(name: str) -> str | None:
    """Tier 3 fallback — same shape as mo_parcel_lookup._people_search_property_mo,
    duplicated (not imported) for the same self-containment reason given in
    the module docstring. Uses `_fetch_page_text` (not the nonexistent
    `_fetch_page`) per that module's existing precedent.
    """
    try:
        from obituary_enricher import _build_people_search_urls, _fetch_page_text

        urls = _build_people_search_urls(name, "Belton", "MO")
        for url in urls[:3]:
            time.sleep(random.uniform(0.5, 1.0))
            text = _fetch_page_text(url)
            if not text or len(text) < 100:
                continue
            addr_pattern = re.compile(
                r"(\d+\s+[\w\s.]+(?:St|Ave|Rd|Dr|Ln|Ct|Blvd|Way|Pl|Cir|Trl|Loop|Run|Ter|Pkwy|"
                r"Nw|Ne|Sw|Se))"
                r"[,.\s]+(?:Belton|Harrisonville|Raymore|Pleasant Hill|Peculiar|Archie|"
                r"Garden City|Cleveland|Lake Winnebago)",
                re.IGNORECASE,
            )
            matches = addr_pattern.findall(text)
            if matches:
                addr = matches[0].strip()
                logger.info("    People search found address: %s", addr)
                return addr
    except Exception as e:
        logger.debug("  Cass people search property lookup failed: %s", e)
    return None


def probate_property_lookup_cass(notices: list[NoticeData]) -> None:
    """Multi-tier property lookup for Cass County, MO probate records.

    Same 3-tier structure as mo_parcel_lookup.probate_property_lookup_mo:
      Tier 1: deed-holder search by decedent name
      Tier 2: deed-holder search by executor/PR name (family property) —
              checked by looking for the decedent's surname anywhere in
              the matched deed-holder string, since Cass has no separate
              co-owner field (its deed-holder value already combines
              multiple names into one string, e.g. "BELT, BARBARA JEAN &
              LESTER D SMITH")
      Tier 3: People search for decedent's last known address

    Modifies notices in-place. No-op for non-Cass notices — should already
    be pre-filtered by the caller (enrichment_pipeline.py's Step 3c), but
    double-checked here defensively.
    """
    candidates = [
        n for n in notices
        if n.county.lower() == "cass"
        and not n.address.strip()
        and n.decedent_name.strip()
    ]
    if not candidates:
        return

    logger.info("Cass Probate Property Lookup: %d candidates", len(candidates))
    found = 0

    for notice in candidates:
        decedent = notice.decedent_name.strip()
        executor = notice.owner_name.strip()
        logger.info("  Looking up property for decedent: %s (Cass)", decedent)

        # ── Tier 1: deed-holder search by decedent name ──
        best_match = None
        for surname in _extract_surname_candidates(decedent):
            time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
            results = _cass_name_query(surname, full_search_name=decedent)
            if results:
                if not best_match or results[0][0] > best_match[0]:
                    best_match = results[0]
                if results[0][0] >= 0.6:
                    break  # good enough, stop searching

        if best_match and best_match[0] >= MIN_SCORE:
            score, parcel = best_match
            logger.info(
                "  Tier 1 (Deed holder search): %s (owner: %s, score: %.2f)",
                parcel.get("situs_street", ""), parcel.get("owner", ""), score,
            )
            if _apply_parcel_to_notice(notice, parcel):
                found += 1
                continue

        # ── Tier 2: deed-holder search by executor name (family property) ──
        if executor:
            dec_last = decedent.split()[-1].upper() if decedent.split() else ""
            applied = False
            for surname in _extract_surname_candidates(executor)[:2]:
                time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
                results = _cass_name_query(surname, full_search_name=executor)
                for score, parcel in results:
                    if dec_last and dec_last in parcel.get("owner", "").upper():
                        logger.info(
                            "  Tier 2 (Executor family): %s (owner: %s, score: %.2f)",
                            parcel.get("situs_street", ""), parcel.get("owner", ""), score,
                        )
                        if _apply_parcel_to_notice(notice, parcel):
                            applied = True
                        break
                if applied:
                    break
            if applied:
                found += 1
                continue

        # ── Tier 3: People search ──
        logger.info("  Tier 3: People search for %s", decedent)
        people_addr = _people_search_property_cass(decedent)
        if people_addr:
            notice.address = people_addr
            notice.city = "Belton"
            notice.state = "MO"
            logger.info("  Tier 3 (People Search): %s", notice.address)
            found += 1
            continue

        logger.warning("  No property found for decedent: %s (all tiers exhausted)", decedent)

    logger.info("Cass Probate Property Lookup complete: %d/%d found", found, len(candidates))
