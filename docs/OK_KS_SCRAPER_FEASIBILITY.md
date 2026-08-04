# Oklahoma & Kansas Scraper Feasibility — live findings, 2026-08-03

Investigation only — no scraper code written. This replaces the earlier, more speculative
"different vendor platform, needs dedicated scraper development" note in `CLAUDE.md`'s
Markets & Data Sources table with concrete findings from actually running searches against
both live sites.

## Bottom line

Neither platform is a drop-in fit for the existing MO/NM `scraper.py` (built around ASP.NET
WebForms postback navigation + per-notice detail pages + reCAPTCHA). Both need dedicated
scraper modules. The good news: **neither has a login wall or CAPTCHA** — a real simplification
versus MO/NM. The hard part on both is the same underlying problem: **search results point at
whole newspaper pages, not individual notices**, so a page can bundle several unrelated notices
and needs to be split apart before we can extract structured fields for just the one that matched.

Kansas is the better starting point. Oklahoma is a bigger lift.

## Oklahoma — oklahomanotices.com (backend: opa.eclipping.org, "eclipping" platform)

- **No login, no CAPTCHA.** Public search at `https://opa.eclipping.org/eebrowser/bbe/2022032812.pa/public/freesearchtest/search/index/type/legals/`.
- Search UI: search terms + date range + county filter (Oklahoma and Tulsa counties both present)
  + a publication multi-select (259 papers statewide by default, can narrow).
- **Results are page-level, not notice-level.** A live search for "probate" (1-week range,
  statewide) returned 56 hits, each one a `{publication, date, page number, download page}`
  tuple — e.g. "Claremore Daily Progress, 08/01/2026, page A007." There's no notice-level
  excerpt visible in the result row (a synopsis may load async — showed "Loading..." in this
  pass and wasn't confirmed to resolve; worth re-checking with more wait time before scoping
  real work).
- **Open question, not resolved this pass:** whether the "download page" PDF has a real
  extractable text layer (like KS, below) or is a scanned image requiring OCR. Didn't download
  one in this session (a page-scripting call was blocked by cookie/query-string data-access
  restrictions on this browsing session). This materially changes the effort estimate — check
  it first before committing to a build.
- **Implication:** every hit requires downloading a full newspaper page, splitting it into its
  individual notices, and figuring out which one is the actual match — full page-segmentation
  pipeline, no way around it.

## Kansas — kansaspublicnotices.com (Kansas Press Association, "NewzGroup"-family platform)

- **No login, no CAPTCHA.** Plain PHP/CodeIgniter app (`/index.php/main/search` routing),
  much simpler than ASP.NET WebForms — plausibly automatable via simple HTTP form submission
  rather than a full stateful Playwright session, though that's not confirmed (didn't test
  bypassing the browser entirely).
- Search UI: free-text search + a **built-in "Probate" quick-phrase preset** (matches Bluetail's
  target notice type exactly) + county filter (Johnson County present — Bluetail's target) +
  city/publication filters + date range.
- **Search results ARE notice-level at the results-page layer** — confirmed live: searching
  "probate" in Johnson County returned 2 distinct result rows, each with its own short text
  excerpt (e.g. "...IN THE DISTRICT COURT OF JOHNSON COUNTY, KANSAS PROBATE DIVISION In the
  Matter of the Estate of DANIEL E. CUNARD...") and its own "View" link.
- **But the underlying PDF is still a full newspaper page.** Downloaded one of the two "View"
  links (`kansaspublicnotices.com/KSLegals/2026/35516-2026-08-03_1002.pdf`, ~695KB, 1 page) and
  extracted its text: it contains the matched Cunard probate notice **and** a separate, unrelated
  "City of Mission Hills, Kansas — Notice of Public Hearing" on the same page. Same underlying
  segmentation problem as Oklahoma, just with a friendlier search layer on top that already
  tells us which notice on the page we actually want (via the excerpt) — real help for
  classification, doesn't eliminate the need to split the page.
- **The PDF has a real extractable text layer** — confirmed via `pdfminer.six`, clean text, no
  OCR needed. This is a meaningful advantage over a from-scratch OCR pipeline.
- **Minor quirk found along the way:** the county dropdown lists "Johnson" twice — once plain,
  once as a distinct `<option>` with a trailing space in its value (`"Johnson "`). Cosmetic in
  the UI but worth defending against in scraper code (e.g. `.strip()` on any county value pulled
  from this dropdown) — same spirit as the "Nalillo" PDF line-wrap bug found in the NM work,
  a small site-specific gotcha that's easy to miss until it silently produces a wrong result.

## Why this is a different shape of problem than MO/NM

MO and NM's `scraper.py` was built around one clean assumption: each search result opens a
dedicated notice detail page containing exactly one notice's text (after a reCAPTCHA solve).
Neither OK nor KS works that way — both are built on top of digitized newspaper archives, where
the atomic unit is a physical page, and a "Legal Notices" page routinely typesets several
unrelated notices in columns next to each other. This is inherent to how these two platforms
work, not a quirk of one vendor — expect it on any similar newspaper-archive-based public notice
site, not just these two.

## Recommended approach

1. **Build Kansas first.** Smaller lift: no OCR needed (real text layer), notice-level search
   results with excerpts to key off of, and a "Probate" preset already aligned to Bluetail's
   target type. Use this to build and validate the page-segmentation logic in production before
   touching Oklahoma.
2. **Segmentation approach:** extract full page text via `pdfminer` (reuse the existing
   `_try_extract_pdf_text()` pattern from `notice_parser.py`, which already does this for MO/NM's
   web-truncated cases), then split on notice-boundary heuristics (case numbers, "IN THE
   DISTRICT COURT OF..." headers, ALL-CAPS section starts) similar in spirit to how
   `foreclosure_filter.py`/`probate_filter.py` already classify notice text — but for
   segmentation, not just inclusion/exclusion. Given how much visual/structural variance exists
   across different papers' typesetting, plan on leaning on the existing LLM-fallback pattern
   (`llm_parser.py`) more heavily than MO/NM's regex-first approach, since regex alone is
   unlikely to reliably find notice boundaries across arbitrary newspaper layouts.
3. **Port to Oklahoma once KS's segmentation is proven.** First confirm the text-layer-vs-scanned
   question above — if OK's PDFs are scanned images, that's a materially bigger addition (full
   OCR pipeline, likely reusing `image_utils.py`'s bilateral-filter/Otsu/Tesseract chain built
   for courthouse photos, but tuned for scanned newsprint instead of phone photos of terminal
   screens — different enough that it shouldn't be assumed to work unmodified).
4. **Scope estimate:** this is genuinely new engineering, not a config change or a fix to the
   existing scraper — two new scraper modules (`scraper_eclipping.py`, `scraper_newzgroup.py` or
   similar), a shared page-segmentation module, and integration into the existing
   `enrichment_pipeline.py` / `data_formatter.py` / DataSift upload path. No code has been
   written yet; this document is the scoping pass, not a start on implementation.

## What's still unverified

- Oklahoma's PDF text-layer status (scanned image vs. real text) — highest-priority unknown,
  directly changes the effort estimate.
- Whether Oklahoma's page-level search results carry a synopsis/excerpt once fully loaded (saw
  "Loading..." placeholders, didn't confirm what they resolve to).
- Whether the Kansas search form can be automated via plain HTTP requests instead of a full
  Playwright browser session — would be a nice simplification if true, not confirmed.
- Rate limits / robots.txt / terms-of-use on both sites — not checked this pass.
