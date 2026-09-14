"""Oklahoma public notices scraper (oklahomanotices.com — actually backed by
opa.eclipping.org, a "newsmemory"/eclipping platform used by many US
newspaper chains for their legal-notice archives).

Confirmed live 2026-09-14 (see docs/OK_KS_SCRAPER_FEASIBILITY.md for the
2026-08-03 scoping pass this build resolves every open question from):

- **No login, no CAPTCHA.** The public search UI at oklahomanotices.com
  posts directly to opa.eclipping.org's own endpoints — no session/auth
  needed for any of it.
- **The PDF has a real extractable text layer** (pdfminer, no OCR needed) —
  confirmed on two real downloaded pages, one with a "_w-socr" filename
  suffix (server-side OCR already baked in) and one without (born-digital
  page). Both extract cleanly. This was the single biggest open question
  from the feasibility doc and it resolves the same way KS did.
- **There is no server-side "county" search filter at all.** The site's
  county dropdown (`nmum_pubb_group_filter`) doesn't get sent to the search
  itself — selecting a county is purely a client-side convenience that
  narrows which individual PUBLICATIONS get checked in a separate
  multi-select, and only the checked `pubblication_ids[]` are what the
  server actually searches. There is a real, separate AJAX endpoint
  (`dropdown/pubb/get-pubbs-by-county`) that returns the publication IDs
  covering a given county — this module calls it directly rather than
  hardcoding a publication list, since that list is presumably maintained
  server-side and could change over time.
- **Publication-level filtering is NOT the same as notice-level venue.**
  Confirmed live: a "probate" search scoped to Oklahoma+Tulsa County's own
  local publications (Tulsa World, Tulsa Beacon, etc.) still returned a
  genuine Sebastian County, ARKANSAS probate notice — regional papers
  publish out-of-state/out-of-county legal notices too. Every candidate
  notice's actual court venue must be verified from its own text
  (_resolve_notice_venue()) before being trusted as a real Oklahoma/Tulsa
  County notice — same class of problem CLAUDE.md already documents for
  NM's Bernalillo/Sandoval mislabeling, just a state-level version of it.
- **Search results are page-level, not notice-level** (unlike KS, which at
  least gives a per-notice excerpt). A "probate" hit is just
  {publication, date, page, download-page-PDF-url} — no excerpt to anchor
  on. So unlike scraper_ks.py's anchor-based single-notice isolation, this
  module fully segments the page into every notice-shaped candidate
  (_segment_page_into_candidates()) and evaluates each one independently.
- **A per-notice "Published in the {Publication}, {City}, {County} County,
  {State}, {dates}" footer line reliably closes out each notice** — the
  same "look for the footer, not the header" idea as KS's date-footer
  segmentation, just a richer footer shape. Confirmed on real Tulsa County
  probate notices (Clifford F. Bean, Mary Iola Haxel Brown estates) sitting
  back-to-back on one physical page along with unrelated civil suits, a
  forfeiture notice, and newspaper ad filler — the footer boundary held up
  across all of them. NOTE: this footer names the newspaper's OWN
  publication county (e.g. "Tulsa County, Oklahoma" for the Tulsa World),
  not necessarily the notice's subject county — it is a segmentation
  boundary marker only, never a substitute for _resolve_notice_venue().
- **The search-range dropdown's VALUE, not the date_from/date_to text
  fields, actually controls the query** — confirmed live: sending an
  explicit custom date_from/date_to alongside dropdown value "365"
  produced results spanning roughly the true prior 365 days regardless of
  what date_from/date_to said, meaning those two fields are effectively
  read-only display fields tied to the dropdown, not independent inputs
  (they're marked `readonly` in the page's own HTML). So this module picks
  the smallest preset value that safely covers the desired window
  (1/3/7/30/60/90/180/365 — see _date_range_preset_for_days()) and then
  applies its OWN client-side date filter against each hit's own listed
  date, rather than trusting the server to honor an arbitrary custom range.
- **Real volume, once scoped to the right 33 local publications:** 602 raw
  "probate" keyword hits across Oklahoma+Tulsa County publications over one
  year (pre-segmentation, pre-venue-check, pre-probate-validation — the
  real yield after all three is materially lower, not yet measured in
  production, but the raw volume alone confirms real day-to-day activity
  worth automating).
"""

import hashlib
import logging
import re
from datetime import datetime, timedelta

import requests

import config
from config import SavedSearch
from notice_parser import NoticeData, _dehyphenate, extract_notice_fields
from probate_filter import is_valid_probate
from scraper import load_last_run_date, load_seen_ids, save_last_run_date, save_seen_ids

logger = logging.getLogger(__name__)

OK_BASE = "https://opa.eclipping.org/eebrowser/bbe/2022032812.pa/public"
OK_SEARCH_URL = f"{OK_BASE}/freesearchtest/search/search/type/legals/format/html"
OK_RESULTS_URL_TMPL = (
    f"{OK_BASE}/freesearchtest/search/get-search-results/format/json/session_id/{{session_id}}/type/legals/isMobile/0"
)
OK_PUBS_BY_COUNTY_URL = f"{OK_BASE}/dropdown/pubb/get-pubbs-by-county/format/html/type/legals"

# The site's own county multi-select ("Choose one of the following
# counties...", id="nmum_pubb_group_filter") option values — confirmed live
# 2026-09-14 by inspecting the search form's HTML. These are NOT sent to the
# search itself; they're only used to fetch the publication list for that
# county (see _get_publication_ids_for_county()).
_COUNTY_PUBB_GROUP_ID = {
    "oklahoma": "309",
    "tulsa": "326",
}

_SEARCH_TERM_BY_NOTICE_TYPE = {
    "probate": "probate",
}

# Available date-range dropdown presets, ascending (days). The dropdown's
# VALUE is what the server actually honors — see module docstring — so a
# desired lookback window picks the smallest preset that still covers it.
_DATE_RANGE_PRESETS = [1, 3, 7, 30, 60, 90, 180, 365]

MAX_RESULT_PAGES = 20  # safety cap — 100 hits/page, so 2000 hits max per search
RESULTS_PAGE_SIZE = 100

# Closes out one notice on a multi-notice newspaper page. Confirmed live
# 2026-09-14 on real Tulsa County probate pages — see module docstring.
# Deliberately doesn't try to parse the trailing date list's exact grammar
# (real samples mix "February 4,11,2025" and "February 4, 11, 18, 2025"
# shapes, i.e. month + a comma-separated day list with inconsistent
# spacing) — non-greedy ".{0,200}?" just runs up to the first 4-digit
# year-looking token after "Published in the", which is reliably the
# footer's own trailing publication year since every day number before it
# is only 1-2 digits.
_PUBLISHED_FOOTER_RE = re.compile(
    r"Published\s+in\s*the\s+.{0,200}?\b(?:19|20)\d{2}\b",
    re.IGNORECASE,
)

# Identifies a notice's actual court venue — e.g. "IN THE DISTRICT COURT OF
# TULSA COUNTY STATE OF OKLAHOMA" or "IN THE CIRCUIT COURT OF SEBASTIAN
# COUNTY, ARKANSAS". Deliberately permissive on the connector between
# "COUNTY" and the state name (comma, "STATE OF", or nothing) since real
# samples use all three shapes — see module docstring and
# _resolve_notice_venue()'s docstring for why a captured word here is only
# trusted as a state if it's a recognized US state name.
_VENUE_RE = re.compile(
    r"COURT\s+(?:OF|WITHIN\s+AND\s+FOR)\s+([A-Z][A-Za-z]+)\s+COUNTY"
    r"(?:\s*,)?\s*(?:STATE\s+OF\s+)?([A-Z][A-Za-z]+)?",
)

_US_STATE_NAMES = {
    "ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS", "CALIFORNIA", "COLORADO",
    "CONNECTICUT", "DELAWARE", "FLORIDA", "GEORGIA", "HAWAII", "IDAHO",
    "ILLINOIS", "INDIANA", "IOWA", "KANSAS", "KENTUCKY", "LOUISIANA",
    "MAINE", "MARYLAND", "MASSACHUSETTS", "MICHIGAN", "MINNESOTA",
    "MISSISSIPPI", "MISSOURI", "MONTANA", "NEBRASKA", "NEVADA",
    "NEWHAMPSHIRE", "NEWJERSEY", "NEWMEXICO", "NEWYORK", "NORTHCAROLINA",
    "NORTHDAKOTA", "OHIO", "OKLAHOMA", "OREGON", "PENNSYLVANIA",
    "RHODEISLAND", "SOUTHCAROLINA", "SOUTHDAKOTA", "TENNESSEE", "TEXAS",
    "UTAH", "VERMONT", "VIRGINIA", "WASHINGTON", "WESTVIRGINIA",
    "WISCONSIN", "WYOMING",
}

# Oklahoma probate case numbers seen live: "PB-2025-0035" (Tulsa County).
# Not assumed universal across every OK county/court, so absence of a match
# just means dedup falls back to a text hash — see _notice_dedup_key().
_CASE_NUMBER_RE = re.compile(r"\bPB-\d{2,4}-\d+\b", re.IGNORECASE)


def _date_range_preset_for_days(days: int) -> int:
    """Smallest available dropdown preset that covers at least `days` back."""
    for preset in _DATE_RANGE_PRESETS:
        if preset >= days:
            return preset
    return _DATE_RANGE_PRESETS[-1]


def _get_publication_ids_for_county(session: requests.Session, county: str) -> list[str]:
    """Live-query which publications cover a given OK county.

    Not hardcoded: confirmed live 2026-09-14 the site resolves this
    server-side via its own endpoint, which is presumably kept in sync with
    real publication coverage — hardcoding a snapshot risks silently going
    stale.
    """
    group_id = _COUNTY_PUBB_GROUP_ID.get(county.lower())
    if not group_id:
        logger.error("OK: no pubb_group_id mapping for county=%r", county)
        return []

    resp = session.post(
        OK_PUBS_BY_COUNTY_URL,
        data={"pubb_group_id[]": [group_id], "exclude_pubbs_in_pubb_group_ids": ""},
        timeout=30,
    )
    resp.raise_for_status()
    ids = re.findall(r'name="pubblication_ids\[\]"\s+value="(\d+)"', resp.text)
    logger.info("OK: %d publications cover %s County", len(ids), county)
    return ids


def _run_search(
    session: requests.Session, term: str, pub_ids: list[str], days_back: int,
) -> str | None:
    """Submit a search and return its session_id (used to page through
    results), or None if the search itself failed.
    """
    preset = _date_range_preset_for_days(days_back)
    today = datetime.now()
    data = [
        ("terms", term),
        ("testing_clipping_search_date_range_clone", str(preset)),
        ("date_from", (today - timedelta(days=preset)).strftime("%m-%d-%Y")),
        ("date_to", today.strftime("%m-%d-%Y")),
    ]
    for pid in pub_ids:
        data.append(("pubblication_ids[]", pid))

    try:
        resp = session.post(
            OK_SEARCH_URL, data=data,
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=90,
        )
        resp.raise_for_status()
    except Exception:
        logger.exception("OK: search request failed (term=%r, preset=%d days)", term, preset)
        return None

    m = re.search(r"session_id/([a-f0-9]+)/", resp.text)
    if not m:
        logger.error("OK: search response had no session_id — cannot page results")
        return None
    return m.group(1)


def _fetch_result_page(session: requests.Session, session_id: str, start: int) -> dict:
    resp = session.post(
        OK_RESULTS_URL_TMPL.format(session_id=session_id),
        data={
            "sEcho": 1, "iColumns": 3, "sColumns": "",
            "iDisplayStart": start, "iDisplayLength": RESULTS_PAGE_SIZE,
            "sSearch": "", "bRegex": "false",
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()


def _parse_hit(row: list) -> dict | None:
    """Extract {publication, date, pdf_url} from one aaData row."""
    try:
        publication = re.search(r"<strong>(.*?)</strong>", row[0]).group(1)
        date_raw = re.search(r"<strong>date</strong>:\s*([\d/]+)", row[1]).group(1)
        href_match = re.search(r'href="([^"]+bbe_prod_request_action=getPdf[^"]+)"', row[1])
        if not href_match:
            return None
        pdf_url = href_match.group(1).replace("&amp;", "&")
        if pdf_url.startswith("//"):
            pdf_url = "https:" + pdf_url
        pub_date = datetime.strptime(date_raw, "%m/%d/%Y").strftime("%Y-%m-%d")
        return {"publication": publication, "pub_date": pub_date, "pdf_url": pdf_url}
    except Exception:
        logger.debug("OK: could not parse a result row — skipping it")
        return None


def _normalize_page_text(text: str) -> str:
    """Collapse a pdfminer-extracted page's line breaks/whitespace down to
    single spaces, after dehyphenating real line-wrap hyphens first — same
    two-step idea as scraper_ks.py's _normalize_for_matching(), needed here
    for the same reason: _PUBLISHED_FOOTER_RE and _VENUE_RE both span what
    were originally several separate lines in the source PDF (e.g.
    "Published in the Tulsa World,\\nTulsa, Tulsa County, Oklahoma,\\n
    February 4,11,2025"), and plain "." in a regex without re.DOTALL never
    matches "\\n" — confirmed via a real live sample where the un-normalized
    footer regex silently matched nothing at all.
    """
    return re.sub(r"\s+", " ", _dehyphenate(text)).strip()


def _segment_page_into_candidates(full_text: str) -> list[str]:
    """Split a full newspaper-page text into per-notice candidate chunks,
    using the "Published in the..." footer as the boundary marker between
    notices. See module docstring for why this differs from scraper_ks.py's
    anchor-based single-notice isolation.

    Expects already-normalized text (see _normalize_page_text()) — callers
    should normalize once up front rather than per-segment.
    """
    footers = list(_PUBLISHED_FOOTER_RE.finditer(full_text))
    if not footers:
        return [full_text]

    chunks = []
    start = 0
    for m in footers:
        chunks.append(full_text[start:m.end()])
        start = m.end()
    if start < len(full_text):
        chunks.append(full_text[start:])
    return chunks


def _resolve_notice_venue(segment_text: str) -> tuple[str | None, str | None]:
    """Best-effort (county, state) this notice's court caption actually
    names — both title-cased, or None if not found/not confidently a state.

    A captured word after "COUNTY" is only trusted as a state name if it's
    in _US_STATE_NAMES — real samples show that slot is sometimes a stray
    word ("PROBATE", a case caption continuation) rather than a real state,
    and a false "unknown state" is far safer than a false reject.
    """
    m = _VENUE_RE.search(segment_text)
    if not m:
        return None, None
    county = m.group(1).title() if m.group(1) else None
    state_word = (m.group(2) or "").upper()
    state = state_word.title() if state_word in _US_STATE_NAMES else None
    return county, state


def _notice_dedup_key(pdf_url: str, segment_text: str) -> str:
    case_match = _CASE_NUMBER_RE.search(segment_text)
    if case_match:
        return case_match.group(0).upper()
    # No case number found — fall back to a stable hash of the page +
    # segment content so re-processing the same page doesn't re-add the
    # same notice, even without a case number to key off of.
    digest = hashlib.sha1(segment_text.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{pdf_url.rstrip('/').split('/')[-1]}:{digest}"


async def scrape_all_ok(
    mode: str = "daily",
    searches: list[SavedSearch] | None = None,
    since_date_override: str | None = None,
    llm_api_key: str | None = None,
    max_notices: int = 0,
    seen_ids: dict[str, str] | None = None,
) -> list[NoticeData]:
    """Main entry point — mirrors scraper.scrape_all()/scraper_ks.scrape_all_ks()'s
    signature/return shape so main.py can dispatch to it without
    platform-specific caller logic. See _run_scrape_pipeline() in main.py.

    Uses plain HTTP requests, not Playwright — confirmed live 2026-09-14
    this platform's search/results/PDF endpoints need no browser session at
    all (see module docstring), a real simplification over scraper.py and
    scraper_ks.py.
    """
    if searches is None:
        searches = []
    if seen_ids is None:
        seen_ids = load_seen_ids()
    logger.info("OK: cross-run dedup: %d previously-seen notice IDs loaded", len(seen_ids))

    if since_date_override:
        since_date = since_date_override
        logger.info("OK: using since_date override: %s", since_date)
    elif mode == "daily":
        since_date = load_last_run_date()
        if since_date:
            logger.info("OK: daily mode: pulling notices since %s", since_date)
        else:
            since_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            logger.info("OK: daily mode: no previous run found, pulling last 7 days (%s)", since_date)
    else:  # historical
        since_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        logger.info("OK: historical mode: pulling last 12 months (since %s)", since_date)

    days_back = max(1, (datetime.now() - datetime.strptime(since_date, "%Y-%m-%d")).days)

    all_notices: list[NoticeData] = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    for search in searches:
        search_term = _SEARCH_TERM_BY_NOTICE_TYPE.get(search.notice_type)
        if not search_term:
            logger.warning(
                "OK: no search-term mapping for notice_type=%r (county=%r) — skipping",
                search.notice_type, search.county,
            )
            continue

        pub_ids = _get_publication_ids_for_county(session, search.county)
        if not pub_ids:
            logger.error("OK: no publications found for county=%r — skipping this search", search.county)
            continue

        logger.info(
            "OK: searching county=%s notice_type=%s term=%r (since %s, %d publications)",
            search.county, search.notice_type, search_term, since_date, len(pub_ids),
        )

        session_id = _run_search(session, search_term, pub_ids, days_back)
        if not session_id:
            continue

        start = 0
        total_records = None
        stop = False
        while not stop and (total_records is None or start < total_records) and start < MAX_RESULT_PAGES * RESULTS_PAGE_SIZE:
            try:
                page_data = _fetch_result_page(session, session_id, start)
            except Exception:
                logger.exception("OK: failed to fetch results page at start=%d", start)
                break

            total_records = int(page_data.get("iTotalRecords", 0) or 0)
            rows = page_data.get("aaData", [])
            if start == 0:
                logger.info("OK: %d total raw hits for %s County", total_records, search.county)
            if not rows:
                break

            for row in rows:
                if max_notices and len(all_notices) >= max_notices:
                    logger.info("OK: hit max_notices=%d — stopping", max_notices)
                    stop = True
                    break

                hit = _parse_hit(row)
                if not hit:
                    continue
                if hit["pub_date"] < since_date:
                    continue

                try:
                    pdf_resp = session.get(hit["pdf_url"], timeout=60)
                    pdf_resp.raise_for_status()
                except Exception:
                    logger.warning("OK: PDF download failed: %s", hit["pdf_url"])
                    continue

                try:
                    from io import BytesIO
                    from pdfminer.high_level import extract_text as pdfminer_extract
                    full_page_text = _normalize_page_text(pdfminer_extract(BytesIO(pdf_resp.content)))
                except Exception:
                    logger.exception("OK: PDF text extraction failed: %s", hit["pdf_url"])
                    continue

                for segment in _segment_page_into_candidates(full_page_text):
                    if "COURT" not in segment.upper():
                        continue
                    if not any(kw in segment.upper() for kw in ("ESTATE OF", "PROBATE", "DECEASED")):
                        continue

                    venue_county, venue_state = _resolve_notice_venue(segment)
                    if venue_county is None:
                        logger.debug(
                            "OK: probate-shaped segment with no resolvable venue — skipping "
                            "(page: %s)", hit["pdf_url"],
                        )
                        continue
                    if venue_county.lower() != search.county.lower():
                        logger.debug(
                            "OK: skipping segment — venue county %r != target %r (page: %s)",
                            venue_county, search.county, hit["pdf_url"],
                        )
                        continue
                    if venue_state is not None and venue_state.lower() != "oklahoma":
                        logger.info(
                            "OK: rejected out-of-state notice — venue county %r matched but "
                            "state is %r, not Oklahoma (page: %s)",
                            venue_county, venue_state, hit["pdf_url"],
                        )
                        continue

                    notice_id = _notice_dedup_key(hit["pdf_url"], segment)
                    if notice_id in seen_ids:
                        continue

                    notice = NoticeData(
                        county=search.county,
                        notice_type=search.notice_type,
                        source_url=hit["pdf_url"],
                        state=config.state_for_county(search.county),
                        raw_text=segment.strip(),
                        date_added=hit["pub_date"],
                    )
                    notice = await extract_notice_fields(notice, llm_api_key)

                    if notice.notice_type == "probate" and not is_valid_probate(notice):
                        seen_ids[notice_id] = notice.date_added or datetime.now().strftime("%Y-%m-%d")
                        continue

                    all_notices.append(notice)
                    seen_ids[notice_id] = notice.date_added or datetime.now().strftime("%Y-%m-%d")

                if stop:
                    break

            start += RESULTS_PAGE_SIZE

    save_seen_ids(seen_ids)
    if mode in ("daily", "historical"):
        save_last_run_date()

    logger.info("OK: scrape complete — %d notices kept", len(all_notices))
    return all_notices
