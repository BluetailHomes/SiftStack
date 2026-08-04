"""Kansas Public Notices scraper (kansaspublicnotices.com — "NewzGroup"-family
platform, Kansas Press Association).

This is a fundamentally different platform than scraper.py's ASP.NET WebForms
automation (MO/NM) — plain PHP/CodeIgniter form (`/index.php/main/search`,
POST), NO login and NO CAPTCHA (confirmed live during the 2026-08-03
feasibility pass — see docs/OK_KS_SCRAPER_FEASIBILITY.md). That's a real
simplification versus MO/NM, but it comes with a problem MO/NM never had:
search results point at whole newspaper PAGES, not individual notices — a
page can (and routinely does) bundle several unrelated notices, sometimes
even ordinary news articles, in one PDF. Every result still needs its one
matching notice isolated out of that page before it can be parsed.

Confirmed live 2026-08-03 (real Johnson County Post page,
35516-2026-08-03_1002.pdf):
  - The PDF has a real extractable text layer (pdfminer, no OCR needed).
  - A physical page can contain: multiple probate notices back to back, a
    city-council notice, AND ordinary news articles — all concatenated in
    the same page's text stream.
  - Notices are reliably closed out by a "publication dates" footer line
    (e.g. "07/20, 07/27, 08/03/2026" or a single "08/03/2026") — this is
    the boundary marker _segment_notice_from_anchor() below relies on.
  - Search results ARE sorted newest-first (confirmed: a 130-result,
    no-date-filter "probate" search in Johnson County showed pages in
    strict descending date order, 08/03/2026 down to 05/11/2026 across
    page 1) — lets daily-mode stop early once results age past since_date,
    same idea as MO/NM's early-stop, without needing full pagination.
  - The county dropdown has a real gotcha: "Johnson" appears twice — once
    with an implicit value ("Johnson"), once as a distinct <option> with a
    trailing space in its value ("Johnson "). _select_county() below picks
    the exact match defensively rather than trusting Playwright's
    label-based select, which could silently pick either one.
"""

import logging
import re
from datetime import datetime, timedelta

from playwright.async_api import async_playwright

import config
from config import SavedSearch
from notice_parser import NoticeData, extract_notice_fields, _dehyphenate
from probate_filter import is_valid_probate
from scraper import (
    delay,
    load_last_run_date,
    load_seen_ids,
    save_last_run_date,
    save_seen_ids,
)

logger = logging.getLogger(__name__)

KS_BASE_URL = config.NOTICE_PLATFORMS["kansaspublicnotices"]
KS_SEARCH_URL = f"{KS_BASE_URL}/index.php/main/search"

# The site's own "Common Phrases" dropdown includes "Probate" as a preset,
# but we submit the literal search term directly into the free-text field
# instead — one less UI dependency, and it's the term the preset itself
# would submit anyway (confirmed by inspecting the preset's effect on the
# free-text field, 2026-08-03).
_SEARCH_TERM_BY_NOTICE_TYPE = {
    "probate": "probate",
}

MAX_PAGES = 10  # safety cap on pagination depth per search

# A result "row" block in the rendered results list always has this shape
# (confirmed live 2026-08-03 across ~25 consecutive real rows, zero
# exceptions): "{Publication} {MM/DD/YYYY}\n{City}, {ST}    County:
# {County}\nView\n{excerpt...}". Capture publication date + excerpt; the
# next occurrence of this same pattern (or end of the results section)
# closes the previous block.
_RESULT_ROW_RE = re.compile(
    r"\n([A-Z][\w .&'\-]*?) (\d{2}/\d{2}/\d{4})\n"
    r"[A-Za-z .'\-]+,\s*[A-Z]{2}\s+County:\s*[A-Za-z ]+\n"
    r"View\n",
)

# Closes out one notice on a multi-notice newspaper page — either a list of
# "MM/DD" entries ending in a full "MM/DD/YYYY", or a single "MM/DD/YYYY" on
# its own. See docs/OK_KS_SCRAPER_FEASIBILITY.md and the module docstring.
_DATE_FOOTER_RE = re.compile(r"(?:\d{2}/\d{2},\s*)*\d{2}/\d{2}/\d{4}")

# The case/estate number itself (e.g. "JO-2026-PR-000818", or
# "JO-2026PR-000824" once the dehyphenation merge above removes the
# hyphen+space between the year and "PR" — the regex tolerates that with an
# optional hyphen). Deliberately does NOT require the "Case No." / "Estate
# No." label words in front of it: confirmed live 2026-08-03 that pdfminer
# can extract those label words out of visual reading order relative to the
# number itself (e.g. rendering "No. Case JO-2026PR-000818" instead of
# "Case No. JO-2026PR-000818") on some notices, which broke a
# label-inclusive version of this pattern. The number alone is still
# unique per notice on the page and doesn't have that ordering risk.
_CASE_NUMBER_RE = re.compile(r"JO-\d{4}-?\s*PR-\d+", re.IGNORECASE)


def _notice_id_from_pdf_url(pdf_url: str) -> str:
    """Stable dedup key for a KS result — the PDF filename itself already
    encodes publication + date + sequence (e.g.
    "35516-2026-08-03_1002.pdf"), unique per physical page. Several notices
    can share one PDF; re-processing the same page just re-finds the same
    notices, so filename-level dedup is sufficient (matches the same
    "notice ID" role SEEN_IDS_FILE plays for MO/NM, just keyed differently
    since there's no per-notice detail-page URL to key off of here)."""
    return pdf_url.rstrip("/").split("/")[-1]


def _normalize_for_matching(text: str) -> str:
    """Collapse a PDF's or a browser excerpt's whitespace down to single
    spaces (after dehyphenating line-wrap hyphens first).

    The search excerpt comes from the site's own rendered HTML, which
    collapses all whitespace runs (including "word-\\nword" line wraps that
    have no literal hyphen-then-newline, just "word- word") to single
    spaces. The PDF text from pdfminer preserves the original line breaks.
    Those are two different normalization pipelines over the same
    underlying content — confirmed live 2026-08-03: a raw substring match
    between the two fails even after _dehyphenate() alone, since that only
    handles hyphen-then-newline, not hyphen-then-space or bare line breaks.
    Normalizing both sides identically (here) sidesteps the whole problem
    rather than trying to reproduce the site's exact rendering rules.
    """
    text = re.sub(r"\s+", " ", _dehyphenate(text))
    # Second dehyphenation pass for text that already went through
    # browser/HTML whitespace collapsing before we ever saw it (the search
    # excerpt) — by that point a PDF line-wrap hyphen has "word- word"
    # shape (hyphen-space, not hyphen-newline), which _dehyphenate() alone
    # doesn't catch since it requires a literal newline. Confirmed live
    # 2026-08-03: "John- son" (from "John-\nson" in the PDF) survives
    # _dehyphenate() intact once collapsed to a single line by the
    # browser, breaking the anchor match. Safe enough here — genuine
    # hyphenated compound words essentially never have a space right after
    # the hyphen in English.
    text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)
    return text.strip()


def _segment_notice_from_anchor(full_text: str, anchor: str) -> str:
    """Isolate the ONE notice matching `anchor` (a snippet of its own text,
    from the search-result excerpt) out of a full newspaper page that may
    bundle several unrelated notices.

    Deliberately does not attempt to segment the whole page into every
    notice it contains — a harder, riskier problem (see the OK/KS
    feasibility doc). We already know which notice we want; this only needs
    to find ITS boundaries: start at the end of the nearest preceding
    publication-dates footer (or the top of the page, if this is the first
    notice on it), end at the next footer found at/after the anchor.

    Returns whitespace-normalized (single-line-per-paragraph-ish) text —
    see _normalize_for_matching() — not the PDF's original line-wrapped
    shape. That's fine for downstream regex/LLM field extraction, which
    already treats runs of whitespace as equivalent (\\s+ throughout
    notice_parser.py), and is consistent with how MO/NM's own notice text
    already looks after page.inner_text() does similar browser-side
    whitespace collapsing.
    """
    text = _normalize_for_matching(full_text)
    anchor = _normalize_for_matching(anchor)

    pos = -1

    # Priority 1: a case/estate number, if the excerpt has one — every real
    # KS probate notice carries a unique "Case No. JO-2026-PR-######" or
    # "Estate No. JO-2026-PR-######" token, which can't repeat elsewhere on
    # the page. Confirmed live 2026-08-03 this is far more reliable than
    # word-window matching: one excerpt was a compound snippet spanning
    # TWO different notices (e.g. "...DANIEL R. BROOKS...Case No.
    # JO-2026-PR-000818...FRANCES LOUISE FOUSHEE...Case No.
    # JO-2026-PR-000803"), and generic word-window fragments like "IN THE
    # DISTRICT COURT OF JOHNSON COUNTY, KANSAS" repeat on nearly every
    # notice on the page — matching the wrong one silently. The case
    # number from the excerpt's OWN leading notice is specific enough to
    # land on the right one.
    case_no_match = _CASE_NUMBER_RE.search(anchor)
    if case_no_match:
        pos = text.find(case_no_match.group(0))

    if pos == -1:
        pos = text.find(anchor)

    if pos == -1:
        # The excerpt is a KWIC-style snippet and its leading edge can be a
        # fragment that isn't even on this page — confirmed live 2026-08-03:
        # one excerpt started with "said." (a dangling word from whatever
        # came immediately before the true match in the site's search
        # index), which isn't present in the single-page PDF this notice
        # actually lives on at all. A pure prefix-shrink never recovers
        # from that since every prefix still starts with the bad word — try
        # dropping a few leading words too, not just shrinking the tail.
        # Kept to 5+ word fragments (no shorter) since short/generic
        # fragments risk matching a repeated boilerplate phrase instead of
        # the actual target notice — the case-number match above is the
        # reliable path; this is a last-resort fallback for excerpts
        # without one.
        words = anchor.split()
        for drop in range(0, min(4, len(words))):
            for n in (10, 7, 5):
                if drop + n <= len(words):
                    fragment = " ".join(words[drop:drop + n])
                    pos = text.find(fragment)
                    if pos != -1:
                        break
            if pos != -1:
                break

    if pos == -1:
        logger.warning(
            "Could not locate notice anchor in downloaded page text "
            "(excerpt: %r) — using full page text as a fallback",
            anchor[:80],
        )
        return text

    footers = [(m.start(), m.end()) for m in _DATE_FOOTER_RE.finditer(text)]

    start = 0
    for fstart, fend in footers:
        if fend <= pos:
            start = fend
        else:
            break

    end = len(text)
    for fstart, fend in footers:
        if fend > pos:
            end = fend
            break

    return text[start:end].strip()


async def _select_county(page, county: str) -> bool:
    """Select the county option in the #counties dropdown by exact
    (trimmed) match on both value and visible text.

    Defends against the site's own duplicate-"Johnson"-option gotcha (one
    plain, one with a trailing space in its value — confirmed live
    2026-08-03) — Playwright's label-based select_option() isn't guaranteed
    to pick the non-trailing-space one, so this does the match in JS
    explicitly instead of trusting either matching strategy blindly.
    """
    picked = await page.evaluate(
        """(county) => {
            const sel = document.querySelector('#counties');
            if (!sel) return false;
            for (const opt of sel.options) {
                if (opt.value.trim() === county && opt.textContent.trim() === county) {
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }
            }
            return false;
        }""",
        county,
    )
    if not picked:
        logger.warning("Could not find an exact county option for %r in #counties dropdown", county)
    return picked


def _parse_result_rows(results_text: str) -> list[dict]:
    """Split the results-list text into per-row dicts: {pub_date, excerpt}.

    Positionally paired with the PDF hrefs collected separately in the same
    DOM order — see scrape_all_ks(). Both come from the same rendered page,
    so DOM order and this regex-based split order line up.
    """
    matches = list(_RESULT_ROW_RE.finditer(results_text))
    rows = []
    for i, m in enumerate(matches):
        pub_date_raw = m.group(2)  # MM/DD/YYYY
        excerpt_start = m.end()
        excerpt_end = matches[i + 1].start() if i + 1 < len(matches) else len(results_text)
        excerpt = results_text[excerpt_start:excerpt_end].strip()
        try:
            pub_date = datetime.strptime(pub_date_raw, "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            pub_date = ""
        rows.append({"pub_date": pub_date, "excerpt": excerpt})
    return rows


async def _get_view_hrefs(page) -> list[str]:
    """Ordered list of every "View" link's href on the current results page."""
    return await page.evaluate(
        """() => Array.from(document.querySelectorAll('a'))
            .filter(a => a.textContent.trim() === 'View')
            .map(a => a.getAttribute('href'))
            .filter(Boolean)"""
    )


async def _has_next_page(page) -> bool:
    return await page.evaluate(
        """() => {
            const links = Array.from(document.querySelectorAll('a'));
            const next = links.find(a => a.textContent.trim() === '>');
            return !!(next && next.getAttribute('href'));
        }"""
    )


async def _click_next_page(page) -> bool:
    clicked = await page.evaluate(
        """() => {
            const links = Array.from(document.querySelectorAll('a'));
            const next = links.find(a => a.textContent.trim() === '>');
            if (next) { next.click(); return true; }
            return false;
        }"""
    )
    if clicked:
        await page.wait_for_load_state("networkidle")
    return clicked


async def scrape_all_ks(
    mode: str = "daily",
    searches: list[SavedSearch] | None = None,
    since_date_override: str | None = None,
    llm_api_key: str | None = None,
    max_notices: int = 0,
    seen_ids: dict[str, str] | None = None,
    headless: bool = True,
) -> list[NoticeData]:
    """Main entry point — mirrors scraper.scrape_all()'s signature/return
    shape so main.py can dispatch to either one based on
    config.NOTICE_PLATFORM without the caller needing platform-specific
    logic. See _run_scrape_pipeline() in main.py.
    """
    if searches is None:
        searches = []

    if seen_ids is None:
        seen_ids = load_seen_ids()
    logger.info("KS: cross-run dedup: %d previously-seen notice IDs loaded", len(seen_ids))

    since_date: str | None = None
    if since_date_override:
        since_date = since_date_override
        logger.info("KS: using since_date override: %s", since_date)
    elif mode == "daily":
        since_date = load_last_run_date()
        if since_date:
            logger.info("KS: daily mode: pulling notices since %s", since_date)
        else:
            since_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            logger.info("KS: daily mode: no previous run found, pulling last 7 days (%s)", since_date)
    elif mode == "historical":
        since_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        logger.info("KS: historical mode: pulling last 12 months (since %s)", since_date)

    all_notices: list[NoticeData] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            for search in searches:
                search_term = _SEARCH_TERM_BY_NOTICE_TYPE.get(search.notice_type)
                if not search_term:
                    logger.warning(
                        "KS: no search-term mapping for notice_type=%r (county=%r) — skipping",
                        search.notice_type, search.county,
                    )
                    continue

                logger.info(
                    "KS: searching county=%s notice_type=%s term=%r (since %s)",
                    search.county, search.notice_type, search_term, since_date,
                )

                await page.goto(KS_BASE_URL, wait_until="domcontentloaded")
                await page.fill("#searchTerm", search_term)
                county_selected = await _select_county(page, search.county)
                if not county_selected:
                    logger.error(
                        "KS: failed to select county=%r — skipping this search rather than "
                        "risking an unfiltered statewide result set",
                        search.county,
                    )
                    continue

                await page.click("form button[type=submit], form input[type=submit]")
                await page.wait_for_load_state("networkidle")

                page_num = 1
                stop_early = False
                while page_num <= MAX_PAGES and not stop_early:
                    results_text = await page.inner_text("body")
                    rows = _parse_result_rows(results_text)
                    hrefs = await _get_view_hrefs(page)

                    if len(rows) != len(hrefs):
                        logger.warning(
                            "KS: result row count (%d) != View link count (%d) on page %d — "
                            "pairing only the overlapping prefix, rest of this page skipped",
                            len(rows), len(hrefs), page_num,
                        )

                    for row, pdf_url in zip(rows, hrefs):
                        if since_date and row["pub_date"] and row["pub_date"] < since_date:
                            logger.info(
                                "KS: hit a result older than since_date (%s < %s) — "
                                "results are newest-first, stopping this search early",
                                row["pub_date"], since_date,
                            )
                            stop_early = True
                            break

                        notice_id = _notice_id_from_pdf_url(pdf_url)
                        if notice_id in seen_ids:
                            logger.debug("KS: skipping already-seen %s", notice_id)
                            continue

                        if max_notices and len(all_notices) >= max_notices:
                            logger.info("KS: hit max_notices=%d — stopping", max_notices)
                            stop_early = True
                            break

                        try:
                            resp = await context.request.get(pdf_url)
                            if resp.status != 200:
                                logger.warning("KS: PDF download failed (HTTP %d): %s", resp.status, pdf_url)
                                continue
                            pdf_bytes = await resp.body()
                        except Exception:
                            logger.exception("KS: PDF download raised an error: %s", pdf_url)
                            continue

                        try:
                            from io import BytesIO
                            from pdfminer.high_level import extract_text as pdfminer_extract
                            full_page_text = pdfminer_extract(BytesIO(pdf_bytes))
                        except Exception:
                            logger.exception("KS: PDF text extraction failed: %s", pdf_url)
                            continue

                        notice_text = _segment_notice_from_anchor(full_page_text, row["excerpt"])

                        notice = NoticeData(
                            county=search.county,
                            notice_type=search.notice_type,
                            source_url=pdf_url,
                            state=config.state_for_county(search.county),
                            raw_text=notice_text,
                            date_added=row["pub_date"],
                        )
                        notice = await extract_notice_fields(notice, llm_api_key)

                        if notice.notice_type == "probate" and not is_valid_probate(notice):
                            logger.debug("KS: filtered out (not a real probate notice): %s", pdf_url)
                            seen_ids[notice_id] = notice.date_added or datetime.now().strftime("%Y-%m-%d")
                            continue

                        all_notices.append(notice)
                        seen_ids[notice_id] = notice.date_added or datetime.now().strftime("%Y-%m-%d")
                        logger.debug("KS: kept notice: %s", pdf_url)

                        await delay()

                    if stop_early:
                        break

                    if not await _has_next_page(page):
                        break
                    if not await _click_next_page(page):
                        break
                    page_num += 1

        finally:
            await browser.close()

    save_seen_ids(seen_ids)
    if mode in ("daily", "historical"):
        save_last_run_date()

    logger.info("KS: scrape complete — %d notices kept", len(all_notices))
    return all_notices
