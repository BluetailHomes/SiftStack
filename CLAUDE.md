# CLAUDE.md — SiftStack

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**SiftStack** — Full-stack real estate investing operations platform built around DataSift.ai CRM. Covers the entire REI business lifecycle:

1. **Data Acquisition:** Web scraping public-notice sites (foreclosures, tax sales, probates — see "Markets & Data Sources" below), scanned PDF import, courthouse terminal photo import (probate, eviction, code violations, divorce), Dropbox auto-polling
2. **Enrichment Pipeline:** 10+ steps — Smarty address standardization, Zillow property data, county tax APIs (Knox County only — see below), obituary/heir research, Ancestry.com SSDI, Tracerfy skip trace, Trestle phone scoring, entity research
3. **Deal Analysis:** Comparable sales (Two-Bucket ARV), rehab estimation (4-tier room-by-room), deal analyzer (MAO/ROI/financing scenarios)
4. **Market Intelligence:** Zip code scoring, Market Finder reports, cash buyer list building, investor portfolio analysis
5. **CRM Automation:** DataSift upload, 26 TCA sequence templates, 12 niche sequential marketing presets, filter preset management, SiftMap sold property tagging
6. **Lead Management:** 4 Pillars of Motivation auto-qualification, STABM daily routine, pipeline reporting, deep prospecting (4-level framework)
7. **Operations:** Acquisition playbook generator (SOPs, scripts, checklists), Slack/Discord notifications, Google Drive upload, Apify Actor deployment

**Markets & Data Sources.** Bluetail's active markets span 8 counties across 4 states. Every module keys off the `COUNTIES` registry in `src/config.py` — see that file for the full per-county data (state, notice platform, assessor/court URLs, zip prefixes). Summary:

| County | State | Notice site | Scraper status |
|---|---|---|---|
| Jackson, Clay, Platte, Cass | MO | mopublicnotices.com | **Live** — verified working |
| Bernalillo, Sandoval | NM | newmexicopublicnotices.com | **Live** — `active=True` for both counties as of 2026-08-03. Same ASP.NET WebForms vendor as the original TN build (confirmed via shared `lrsws.co` TLS cert). Pagination-past-page-1 blocker resolved 2026-08-01; a shared-saved-search county-mislabeling bug (Sandoval notices silently mislabeled "Bernalillo") found and fixed 2026-08-03, live-confirmed against real notices. See `CountyProfile.notes` for both counties for the full history. No scheduled Apify Task yet — still runs local/manual only. |
| Oklahoma, Tulsa | OK | oklahomanotices.com (backend: opa.eclipping.org) | **Not scraper-compatible** — different vendor platform ("eclipping"), needs dedicated scraper development. Live feasibility pass done 2026-08-03 (no CAPTCHA/login, but search results are newspaper-page-level, not notice-level — needs a page-segmentation pipeline). See `docs/OK_KS_SCRAPER_FEASIBILITY.md`. |
| Johnson | KS | kansaspublicnotices.com | **Not scraper-compatible** — different vendor platform ("NewzGroup" family, shared cert with kypublicnotice.com/ndpublicnotices.com), needs dedicated scraper development. Live feasibility pass done 2026-08-03 (no CAPTCHA/login, notice-level search results with excerpts, but the underlying PDF is still a multi-notice newspaper page — smaller lift than OK since the PDF has a real text layer, no OCR needed). See `docs/OK_KS_SCRAPER_FEASIBILITY.md`. |
| Knox, Blount | TN | tnpublicnotice.com | Dormant/legacy — the original build market, kept functional but excluded from default active scraping |

None of the 8 active counties have a documented public tax-assessor API like Knox County's — `tax_enricher.py`/`property_lookup.py` degrade gracefully (skip enrichment, log rather than mislabel) for counties without a working integration. Jackson County (MO) and Bernalillo County (NM) have ArcGIS Open Data Hub parcel layers, the closest thing to a real API among the 8 — not yet wired up. See each `CountyProfile.assessor_url`/`court_records_url` in `config.py` for reference-only links (courthouse/court-record and tax-assessor sites), and each profile's `notes` field for known caveats (e.g. NM's pagination blocker, or probate notice coverage on the MO site that hasn't been confirmed live yet).

8. **REI Skill Library:** 13 Claude Co-Work skill files (`.skill`/`.plugin` ZIPs) for distribution to DataSift community via [learn.datasift.ai/claude-skills-rei](https://learn.datasift.ai/claude-skills-rei). Skills teach Claude specific REI workflows when uploaded to Co-Work sessions or Projects.

## Commands

```bash
# Setup
pip install -r requirements.txt
playwright install chromium
cp .env.example .env  # then fill in credentials

# Run
python src/main.py daily                          # new notices since last run
python src/main.py historical                     # last 12 months of data
python src/main.py daily --split                  # separate CSV per county+type
python src/main.py daily --counties Jackson       # only Jackson county
python src/main.py daily --types foreclosure,probate  # only specific types
python src/main.py daily -v                       # verbose/debug logging

# DataSift preset/sequence management
python src/main.py manage-presets --discover                      # list all presets and sequences
python src/main.py manage-presets --add-sold-exclusion            # add Sold exclusion to all presets
python src/main.py manage-presets --create-sold-sequence          # create Sold cleanup sequence
python src/main.py manage-presets --all                           # discovery + update + sequence

# SiftMap sold property tagging
python src/main.py manage-sold --months-back 12                   # tag sold properties (last 12 months)
python src/main.py manage-sold --counties Jackson --min-sale-price 5000

# Courthouse photo import (build 1.0.28+)
python src/main.py photo-import --folder ./photos --photo-county Jackson --photo-type probate
python src/main.py photo-import --folder ./photos --photo-county Jackson --photo-type eviction --skip-obituary
python src/main.py dropbox-watch                                  # auto-poll Dropbox for new photos
python src/main.py dropbox-watch --poll-interval 300 --max-polls 5  # 5-min interval, 5 cycles
python src/main.py dropbox-watch --no-delete                      # keep photos in Dropbox after processing
```

All source files are in `src/` and imports assume `src/` is the working directory. Run from project root with `python src/main.py` or set `PYTHONPATH=src`.

## Architecture

**Data flows:**
- **Web scrape:** `main.py` → `scraper.py` → `captcha_solver.py` → `notice_parser.py` + `foreclosure_filter.py` + `probate_filter.py` → enrichment → CSV
- **PDF import:** `main.py` → `pdf_importer.py` (pypdfium2 → `image_utils.py` OCR) → enrichment → CSV
- **Photo import:** `main.py` → `photo_importer.py` (OpenCV → `image_utils.py` OCR → `llm_parser.py`) → enrichment → CSV
- **Dropbox watch:** `dropbox_watcher.py` → `photo_importer.py` → enrichment → CSV (auto-polling loop)
- **Market Finder:** `extract_market_finder.py` → DataSift Market Finder (Playwright) → paginate all ZIP + neighborhood data → JSON → `generate_knox_report.py` → 7-sheet Excel

- **main.py** — CLI entry point. Parses args (`daily`/`historical`, `--split`, `--counties`, `--types`, `-v`). Filters saved searches by county/type, orchestrates scrape → dedup → export, logs run summary stats.
- **scraper.py** — Playwright browser automation. Reuses saved session cookies when possible, falls back to fresh login. Selects each saved search from the Smart Search dropdown (triggers ASP.NET postback), paginates results (50/page max), clicks each View button to open notice detail pages. Uses `last_run.json` for daily mode state, `cookies.json` for session persistence.
- **captcha_solver.py** — Solves reCAPTCHA v2 via **2Captcha API** on every notice detail page. Sends websiteURL + sitekey, gets back a `g-recaptcha-response` token, injects it, clicks "View Notice". Retries up to 3 times. This is the primary bottleneck (~10-30s per notice).
- **notice_parser.py** — Extracts structured fields from raw notice text using regex. There are NO structured HTML fields on the site — address, owner, dates are all embedded in free-text notice bodies. Defines the `NoticeData` dataclass used throughout.
- **foreclosure_filter.py** — Filters foreclosure search results to only keep real first-to-market trustee sales. Matches against observed title variations (substitute/successor trustee sales). Non-foreclosure notice types pass through unfiltered.
- **probate_filter.py** — Same include/exclude phrase pattern as `foreclosure_filter.py`, for probate. Built after NM's "probate" saved search (loose keyword matching) pulled in non-probate notices — city council agendas, civil suits, storage auctions — that would otherwise cost a real CAPTCHA solve + LLM call each. Checked both before the LLM fallback in `notice_parser.py` (saves the LLM cost) and post-parse in `scraper.py` (defense in depth). Non-probate notice types pass through unfiltered.
- **data_formatter.py** — Deduplicates by address (keeps most recent), then converts `NoticeData` list to Sift upload CSV. Split mode produces `{county}_{type}_{timestamp}.csv` files.
- **config.py** — Credentials (from `.env`), ASP.NET element selectors, saved search definitions, rate limiting constants, paths, image processing thresholds.
- **image_utils.py** — Shared OCR utilities used by both `pdf_importer.py` and `photo_importer.py`. Exports `fix_rotation()` (Tesseract OSD) and `ocr_page(image, psm)` with configurable page segmentation mode. Handles Tesseract binary detection.
- **photo_importer.py** — Courthouse phone photo import. OpenCV preprocessing chain (EXIF transpose → blur check → bilateral filter → perspective correction → Otsu threshold) → Tesseract OCR (PSM 4) → LLM parsing → NoticeData. Supports all 7 notice types.
- **dropbox_watcher.py** — Cursor-based Dropbox folder polling. Downloads new photos, resolves county + notice_type from folder path (`/Knox/eviction/photo.jpg`), processes through photo_importer, deletes from Dropbox after success. State persisted to `dropbox_state.json` + `photo_state.json`.
- **report_generator.py** — Generates per-record PDF deep prospecting reports using reportlab. Includes property summary, signing chain with phone tiers, valuation, deceased owner detection. Output to `output/reports/`.
- **extract_market_finder.py** — Playwright automation to extract ALL ZIP code + neighborhood data from DataSift Market Finder. Handles styled-component dropdowns, pagination (20 rows/page), Beamer popup dismissal. Outputs JSON. See "Market Finder Extraction Patterns" below.
- **market_analyzer.py** — ZIP code scoring engine. 6-factor weighted composite (Distress 30%, Value 20%, Equity 15%, Tax Delinquency 15%, Competition 10%, DOM 10%). Grades A/B/C/D, budget allocation across top ZIPs. Reads from scraped notice CSVs in `output/`.
- **drive_uploader.py** — Google Drive upload via service account. `upload_file()` (generic, returns webViewLink) and `upload_csv()` (CSV-specific, returns file ID).

## Site-Specific Details

The current scraper automation (`scraper.py`) targets the **ASP.NET WebForms** notice-site platform — all navigation uses `__doPostBack()` with ViewState. Session IDs are embedded in URL paths (`/(S({guid}))/`). Playwright is required because direct HTTP requests would need to manage ViewState/EventValidation manually. This is mopublicnotices.com (live, Missouri counties) and newmexicopublicnotices.com (same vendor per the "Markets & Data Sources" table above — confirmed via TLS cert SAN; login and saved-search selection confirmed live, but blocked on a pagination reliability issue — see that table and `CountyProfile.notes` for Bernalillo/Sandoval). Oklahoma's notice site runs on a different vendor platform and is **not compatible** with this automation as-is (see `docs/OK_KS_SCRAPER_FEASIBILITY.md`). Kansas has its own dedicated scraper — see below.

**Kansas (Johnson County) — dedicated scraper, `src/scraper_ks.py` (built 2026-08-04).** kansaspublicnotices.com is a plain PHP/CodeIgniter form (`/index.php/main/search`, POST) — no login, no CAPTCHA, no ASP.NET postback, so it doesn't reuse `scraper.py` at all. `main.py` dispatches on `config.NOTICE_PLATFORM == "kansaspublicnotices"` (both the CLI path's `_run_scrape_pipeline()` and the Actor path in `actor_main()`) straight to `scraper_ks.scrape_all_ks()` instead. `_preflight_check()` also skips the `NOTICE_SITE_EMAIL`/`PASSWORD`/`CAPTCHA_API_KEY` checks for this platform, since none of those apply here.

The real problem this platform has — confirmed live during the 2026-08-03 feasibility pass, see `docs/OK_KS_SCRAPER_FEASIBILITY.md` — is that search results are notice-level (an excerpt + a "View" link per hit) but the linked PDF is a full physical newspaper page that can bundle several unrelated notices (probate notices, a city-council zoning notice, and even a plain news article were all found concatenated on one real downloaded page). `scraper_ks._segment_notice_from_anchor()` isolates just the one matching notice per search result rather than trying to segment the whole page — it anchors primarily on the notice's own case/estate number (e.g. `JO-2026-PR-000818`, extracted via `_CASE_NUMBER_RE`), since that's guaranteed unique on the page, with a word-window fallback (dropping leading words, since a KWIC-style excerpt's leading edge can be a dangling fragment from context outside the notice or even outside the page) for the rare case an excerpt doesn't carry a case number. Two whitespace/hyphenation gotchas found along the way, both handled in `_normalize_for_matching()`: the search excerpt comes from the site's own rendered HTML (whitespace already collapsed) while the PDF text comes fresh from `pdfminer` (original line breaks intact) — those are two different normalization pipelines over the same content, so both sides are normalized identically before matching rather than trying to reproduce the site's exact rendering; and PDF line-wrap hyphens that already went through HTML whitespace-collapsing show up as `"word- word"` (hyphen-space, no newline) rather than the `"word-\nword"` shape `notice_parser._dehyphenate()` was built for, needing a second merge pass.

`notice_parser.py` gained `extract_notice_fields()`, split out of `parse_notice_page()` on 2026-08-04 (behavior-preserving refactor, verified via the full test suite) — it's the regex+LLM-fallback field-extraction logic that used to only be reachable via a live Playwright `page` object, now usable by any scraper that already has plain notice text from elsewhere, which `scraper_ks.py` needed since kansaspublicnotices.com has no notice detail page at all to fetch.

Verified: `py_compile`, the full existing test suite, and unit-level checks against two real downloaded Johnson County Post pages — correctly isolated 2 distinct notices with zero cross-notice bleed (confirmed against a page that also contained the unrelated city-council/news-article content mentioned above), and a full `NoticeData` → `deduplicate()` → `write_csv()` round-trip with no compatibility issues. **Live-tested end-to-end 2026-08-04 in two rounds** — a 5-notice daily-mode run (which found and fixed a spurious `www.` prefix on `NOTICE_PLATFORMS["kansaspublicnotices"]` that the real TLS cert doesn't cover) and a 12-month historical run specifically to force pagination (confirmed via 98 distinct downloaded filenames and the since-date cutoff being reached, with zero cross-notice bleed on real bundled pages of 9-10 notices each). `johnson.active` is now `True` in `config.py`, same precedent as Bernalillo/Sandoval.

**KS validation-drop rate investigated and root-caused (2026-08-04) — structural, not a bug.** The 12-month historical test kept 60 notices off the site but only exported 30 after enrichment — a ~49% drop, notably higher than MO/NM's typical 15-20%. Traced via `logs/scrape_2026-08-04_164405.log`: 1 entity-owned record removed at Step 3, then 29 removed at the validation gate (`enrichment_pipeline.py`'s invalid-record filter, ~line 325), and every one of the 10 logged sample reasons was the same: `"missing address (no property, PR, or DM address)"`. This is not a parsing failure — it's that Johnson County's probate notices, unlike MO's and NM's, frequently don't carry a mailable property address in the notice text at all, the same structural gap Knox County's courthouse probate notices have (see "Probate Deep Prospecting" below), which is exactly why that Tax-API address-lookup tier exists for Knox. Kansas has no equivalent lookup yet, so these records are correctly dropped rather than uploaded with a blank address. If recovering these leads matters, the fix is building a Johnson-County-specific address-lookup tier (assessor site, people-search fallback, etc.), not a scraper change. Separately, spotted in the same log sample: at least one dropped record's extracted name field was garbage (`"Cta"`, `"Under The Provisions"` — likely boilerplate legal phrasing mis-captured as a PR/decedent name by the name-extraction regex) — worth a follow-up look, though it wouldn't have been kept either way since it also lacked an address.

**Same vendor, different reliability.** Despite mopublicnotices.com and newmexicopublicnotices.com running the same underlying ASP.NET WebForms platform, they aren't identical in practice — confirmed live 2026-07-22:
- Login form field IDs match exactly, but the saved-searches dropdown's exact element ID does not (`ctl00_as1_ddlSavedSearches` on MO vs `ctl00_ContentPlaceHolder1_as1_ddlSavedSearches` on NM — different master-page nesting). `config.SEL_SAVED_SEARCHES_DROPDOWN` now uses a suffix selector (`[id$='_ddlSavedSearches']`) that matches both.
- NM's `__doPostBack()` navigations don't reliably fire Playwright's navigation-completion signals as consistently as MO's — `scraper.py` has fallback/retry logic for the saved-search dropdown selection and per-notice grid re-query after `go_back()`, but next-page advancement past page 1 remains intermittent.
- **Correction (2026-07-27): MO pagination is not actually error-free either** — this file previously claimed "MO's own production logs show clean, error-free pagination throughout" as the reason NM was treated as the only side with a real bug. A live 2am scheduled run that night hit a genuine MO pagination failure: heavy 2Captcha overload (see the captcha_solver.py note below) left the session in a bad enough state that the next-page button click itself raised an uncaught `PwTimeout`, which propagated straight out of `run_saved_search()` *before* it reached `return notices` — silently discarding every notice already scraped earlier in that same call. Fixed same day: the page-advance retry loop in `scraper.py` now wraps the click + navigation wait in try/except and treats a raised exception the same as "click didn't advance," falling through to the existing graceful stop-and-return-what-we-have path instead of throwing. So: not an NM-only bug — MO can hit this too under the right (bad) conditions, just apparently rarer. Still worth folding into whatever fix lands for the tracked NM pagination work, since the root symptom (postback/navigation-completion unreliability under load) is the same shape on both platforms.
- **NOTICE_PLATFORM env var + separate-run model.** `config.NOTICE_PLATFORM` (default `"mopublicnotices"`) selects which site's `BASE_URL`/`LOGIN_URL`/`SMART_SEARCH_URL` a given run targets, via the `NOTICE_PLATFORMS` dict. One platform per scheduled run — mixing counties from two platforms in one `--counties` invocation drops the mismatched ones with a warning (`main.py`'s `_filter_searches_by_platform`) rather than silently scraping the wrong site. Credentials resolve per-platform first (`{PLATFORM}_EMAIL`/`{PLATFORM}_PASSWORD`, e.g. `NEWMEXICOPUBLICNOTICES_EMAIL`) before falling back to the generic `NOTICE_SITE_EMAIL`/`PASSWORD` — see `.env.example`.

**reCAPTCHA v2 is required on every single notice detail page**, even when logged in. There is no CAPTCHA on login, search, or results pages. The sitekey is hardcoded in `config.py`.

**2Captcha overload is a real, observed failure mode, not just a theoretical one.** A live 2am run on 2026-07-27 hit repeated `ERROR_NO_SLOT_AVAILABLE` plus back-to-back 90s poll timeouts on the same notice, burning ~13 minutes on just 2 notices — MAX_RETRIES=3 in `captcha_solver.py` multiplied by another 3 retries at the `scraper.py` call site meant a single stuck notice could cost up to 3 × 3 × 90s = 810s worst case. Fixed same day in `captcha_solver.py`: `ERROR_NO_SLOT_AVAILABLE` now returns a distinct sentinel so the caller backs off (`TURNSTILE_OVERLOAD_BACKOFF_SECONDS`) instead of retrying instantly, and once a call has seen any trouble signal it drops to a shorter poll timeout (`TURNSTILE_RETRY_POLL_TIMEOUT`, 30s) for the rest of that call instead of a fresh 90s each time — cuts the same-call worst case from 270s to roughly 68-150s. The multiplicative retry at the scraper.py level is unchanged (a bigger structural fix, not done yet) — see the tracked task for hardening that further.

**LLM response parsing broke silently on extended-thinking responses (fixed 2026-07-28).** `src/llm_client.py`'s `_chat_anthropic`/`_chat_anthropic_async` did `response.content[0].text`, blindly assuming the first content block returned by the Anthropic API is always a text block. Once a fresh, valid `ANTHROPIC_API_KEY` was in place (see the Actor-input note above) and calls started actually reaching the API instead of failing at the 401 auth layer, a `ThinkingBlock` began preceding the text block on some responses, and indexing straight into `content[0]` raised `'ThinkingBlock' object has no attribute 'text'` — silently invisible until that point because every prior call had already failed earlier in the request lifecycle. This matters most for `obituary_enricher.py`'s non-preset LLM verification step, which calls `chat_json`/`chat_json_async` to confirm obituary-to-decedent matches; a crash there reads as "no match" and quietly rejects otherwise-valid leads. Fixed via a new `_extract_response_text(content)` helper that concatenates every block with a `.text` attribute (skipping `ThinkingBlock`, tool-use blocks, etc.) instead of indexing position 0 — both call sites now use it. Verified via `py_compile` and a mock-object unit check covering thinking-then-text, text-only, multi-text, and thinking-only-with-no-text-block cases; not yet confirmed against a real Anthropic response containing a thinking block in production.

**Same bug, two more call sites (fixed 2026-07-28).** `entity_researcher.py` (`_parse_entity_with_llm`) and `pdf_importer.py` (`parse_page_llm`) each make their own direct `anthropic.Anthropic().messages.create()` call instead of going through `llm_client.py`'s shared `chat_json`/`chat_json_async`, so they had the identical `response.content[0].text` bug and weren't covered by the `llm_client.py` fix above. Both now import and reuse `_extract_response_text()` from `llm_client.py` rather than duplicating the block-concatenation logic. If a future module adds its own direct Anthropic call, check it for this same pattern rather than assuming `llm_client.py`'s fix covers the whole codebase.

**Postback reliability hardening, round 2 (2026-07-28) — targets the "element is not visible" 60s click timeouts, not just the exception-propagation bug fixed 2026-07-27.** A live run the same day hit a new symptom on the next-page click: not an exception, but `next_btn.click()` itself spinning for the full 60-second default timeout with Playwright's actionability log showing "element is not visible" across all ~112 polling attempts — i.e. the click never failed, it just never resolved. Root cause suspected: `scraper.py`'s pagination retry loop queried `next_btn` once at the top of the page loop and reused that same handle across all 4 retry attempts; if the per-notice `go_back()` cycling on that page (up to 50 notices, each navigating away and back) triggered a full grid re-render, the original handle could end up pointing at a stale/hidden DOM node. Fixed by re-querying `next_btn` fresh on every single attempt (including the first) in both the main pagination loop and `_recover_to_search_page()`'s recovery loop, and by giving the click itself an explicit 15-second timeout instead of inheriting the context's 60-second default — a genuinely stuck click now fails fast and gets a clean re-query + retry instead of burning up to 4×60s (4+ minutes) before the loop gives up on a page. Separately, added extra pacing (`delay()` ×2 instead of ×1) specifically on the "already-processed, skip via seen_ids" `go_back()` path, to test the "skip-cache hypothesis" already noted in `config.py`'s Bernalillo profile — a heavily-reprocessed search (e.g. from repeated test reruns accumulating a large seen_ids cache) skips the natural ~15-30s CAPTCHA-solve pacing on every already-seen row, producing a much faster `go_back()` cadence than any real production run would ever see, which may itself be degrading ASP.NET ViewState/postback reliability. Verified via `py_compile` and the existing `test_scraper_login.py` suite (2 passed); **not yet live-tested against a real run** — see the tracked task for turning New Mexico on, which requires validating this fix against NM's page-1 pagination blocker before flipping `active=True`.

**New Mexico pagination blocker resolved (2026-08-01) — Bernalillo flipped to `active=True`.** A manual local run (`NOTICE_PLATFORM=newmexicopublicnotices python src/main.py daily --counties Bernalillo,Sandoval`) against build containing the pagination-retry hardening above scraped 51 raw notices across 4 full pages — the first real validated output past page 1 this county has ever produced — and exported 41 fully validated, mailable, obituary-confirmed records. It stopped gracefully on page 5's pagination recovery limit rather than losing data, so this is meaningfully better, not yet perfect. Note that `active` is documentation-only — confirmed via codebase search that it isn't read anywhere as a functional gate — so this flip has no runtime effect by itself; there is deliberately **no scheduled Apify Task for NM yet** (it needs its own Task since it runs on a different `NOTICE_PLATFORM` than the MO daily schedule). **Sandoval stays `active=False`**: the same run returned zero Sandoval records despite both counties sharing one saved search — not yet root-caused (could be genuinely nothing new, or candidates falling out somewhere in filtering/validation) — see `CountyProfile.notes` for both counties. Don't infer Sandoval works just because Bernalillo did in the same run.

**Root cause found for Sandoval's zero-records result (2026-08-01) — it's a mislabeling bug, not a rejection bug.** Only 3 notices in the run hit the "Filtered out (wrong county)" path, and none were genuine Sandoval notices (2 were Bernalillo notices mangled by a separate OCR/regex bug — see below; 1 was a correct Valencia County reject). But the raw pdfminer text extraction contains at least 2-3 unambiguous Sandoval County property references ("situated in Sandoval County," "County of Sandoval") that never appear in the wrong-county filter log — meaning they passed through silently. Traced to: `scraper.py` hard-sets `notice.county = search.county` (= "Bernalillo," since `_dedupe_by_saved_search_name()` collapses the two NM counties down to whichever `SavedSearch` entry survives — Bernalillo, per its earlier position in `config.SAVED_SEARCHES`) *before* `is_target_county(notice.raw_text, search.county)` ever runs. That check doesn't validate against `search.county` specifically — it checks whether the raw text mentions *any* county configured anywhere in `config.COUNTIES` (all 8 active + 2 dormant markets). A real Sandoval notice passes that check fine (Sandoval is a valid county in the registry) since the check never compares it back to the already-hardcoded "Bernalillo" label. Net effect: the 41 records reported as "Bernalillo" in the 2026-08-01 test almost certainly include an unknown number of real Sandoval properties wearing a Bernalillo label — the 41/0 split is misleading, not accurate. Fix (not yet made — tracked as a task): re-derive `notice.county` from the same courthouse/register's-office regex match `is_target_county()` already extracts, rather than trusting `search.county` unconditionally whenever a search covers a collapsed/shared saved search. **Separate bug found in the same investigation:** `is_target_county()`'s extraction regex (`(\w+)\s+County\s+Courthouse`) twice captured "Nalillo" instead of "Bernalillo" on two genuinely-Bernalillo notices — suspected PDF line-wrap splitting "Ber-nalillo" across lines with only the second half surviving into the matched token. Unrelated to the mislabeling bug above but found via the same log audit; also not yet fixed.

**Mislabeling fix (2026-08-03), round 1 — correct logic, but didn't fire on real notices.** `notice_parser.py` gained `resolve_notice_county(text, search_county) -> (keep, resolved_county)`, replacing the old `is_target_county()` bool-only check (kept as a thin backward-compatible wrapper). It re-derives the notice's actual county from the Register's-Office/Courthouse regex matches and, when that differs from `search.county`, `scraper.py` now overrides `notice.county`/`notice.state` instead of trusting the search label. Also fixed the "Nalillo" bug via a `_dehyphenate()` step (`-\s*\n\s*` → "") applied before matching. Verified via `py_compile`, unit-level synthetic-text checks, and the existing test suite (44 passed) — but **live-tested against the real 2026-08-01 NM notices and found NOT to fire**: re-running the same 51 notices (after clearing their `seen_ids.json` entries) still produced `Bernalillo: 36 / Sandoval: 0`, unchanged. Root cause of the miss: real NM notices phrase the property location as "situated in Sandoval County," and "County of Sandoval," — neither matches `_REGISTER_COUNTY_RE` (`Register's Office for/of X County`) or `_COURTHOUSE_COUNTY_RE` (`X County Courthouse`), so `resolve_notice_county()` fell through to its "can't determine — keep the original label" branch every time, silently preserving the mislabel. The dehyphenation half of the fix did work (0 "Nalillo" mis-captures in the retest, down from 2).

**Mislabeling fix, round 2 (2026-08-03) — added the missing phrasing patterns.** Added `_SITUATED_COUNTY_RE` (`situated in X County`) and `_COUNTY_OF_RE` (`County of X`) to catch the phrasing real NM notices actually use. To avoid a new false-positive risk — "County of X" also appears in notarization/acknowledgment blocks naming where the document was *signed*, not where the property sits — `resolve_notice_county()` now splits its matches into `narrow_counties` (Register's-Office/Courthouse only) and `broad_counties` (narrow + the two new patterns): the **reject** decision (property confirmed in a non-target county) still runs off `narrow_counties` only, unchanged from round 1; the **relabel** decision runs off `broad_counties`, so a stray notary-block county mention can at worst make the relabel ambiguous (falls back to keeping the original label — same as the round-1 miss, never a wrong relabel) but can never cause a false reject. Verified via `py_compile`, the full test suite (44 passed), and expanded synthetic-text checks including the notary-block edge case (a genuinely-Bernalillo notice with an incidental "County of Sandoval" notary line correctly stays labeled Bernalillo rather than flipping).

**Round 2 confirmed live (2026-08-03).** Re-ran the same seen_ids-reset process against real NM notices (`--since 2026-07-23`; first attempt hit an unrelated transient "Results grid lost" session hiccup and returned 0, a known NM reliability issue — retry succeeded cleanly). Result: `County re-labeled` fired 11 times on real Sandoval notices (matched via courthouse-venue phrasing like "...Probate Court, Sandoval...", not only the "situated in"/"County of" examples the patterns were written for — the broader detection caught phrasing variety beyond what was explicitly tested). Final breakdown: 58 total records, Bernalillo 47 / Sandoval 11 (this run covered 2 extra days of postings vs. the original 2026-08-01 test, so absolute counts are higher, but the shape is right — real Sandoval representation instead of zero). Still 0 "Nalillo" mis-captures and exactly 1 wrong-county reject (Valencia, ID 97677), consistent across all three live runs. Both halves of the fix (dehyphenation + county relabeling) are confirmed working on real data.

## Saved Searches

Defined in `config.py` as `SAVED_SEARCHES`, built from the `COUNTIES` registry. Each entry maps to an exact dropdown option name that must exist on the Smart Search dashboard before scraping. The 4 live Missouri counties (Jackson, Clay, Platte, Cass) each have their own saved search (probate), one search per county. New Mexico's `SAVED_SEARCHES` entries exist too (Bernalillo, Sandoval both mapped to the account's pre-existing `"probate"` saved search) — but the county itself is still `active=False` pending the pagination fix (see "Markets & Data Sources"). NM's is a different shape than MO's: a single saved search already covers both counties in one query, rather than one search per county — `main.py`'s `_dedupe_by_saved_search_name()` collapses the resulting duplicate `SavedSearch` entries so the site-side search only runs once when both counties are requested together. Oklahoma/Kansas aren't in `SAVED_SEARCHES` at all yet since the platform isn't scraper-compatible. Knox/Blount (TN) saved searches remain defined but inactive (dormant market).

Filterable via `--counties` and `--types` CLI args (comma-separated, or omit for all).

## Key Domain Rules

- **Foreclosure filtering is critical.** Not all notices from "Foreclosure" saved searches are actual foreclosures. The scraper parses each notice's full text and only includes ones with trustee sale language. See `INCLUDE_PHRASES` / `EXCLUDE_PHRASES` in `foreclosure_filter.py`.
- **Probate owner_name** should be the Personal Representative/Executor/Administrator — not the deceased.
- **Owner names** in foreclosure notices typically appear after "executed by" in the deed of trust language.
- **Rate limiting:** 2-3 second random delays between requests, 3 retries per page.
- **Address dedup:** Same property can appear in multiple notices; `data_formatter.deduplicate()` keeps the most recent.

## Output

CSV files land in `output/` (gitignored). Logs go to `logs/` with timestamped filenames. Sift columns: `date_added, address, city, state, zip, owner_name, notice_type, county, source_url`.

## Apify Deployment

The project runs as an **Apify Actor** in the cloud. When `APIFY_IS_AT_HOME` or `APIFY_TOKEN` is set, `main.py` uses the Actor SDK instead of CLI args.

```bash
# Install Apify CLI
npm install -g apify-cli

# Local test (reads input.json, simulates Actor environment)
apify run --purge

# Deploy to Apify platform
apify login
apify push

# On Apify Console: set up daily schedule and configure secrets in Actor input
```

### Actor Input (configured in Apify Console or `input.json`)
- `mode`: "daily" or "historical"
- `notice_platform`: which public-notice site this Task runs against — `mopublicnotices`, `newmexicopublicnotices`, `kansaspublicnotices`, `oklahomanotices`, or `tnpublicnotice`. Leave blank to keep the Actor's default (`mopublicnotices`). **Added 2026-08-05, see "Multi-platform Task setup" below — required reading before creating an NM or KS scheduled Task.**
- `counties` / `types`: arrays to filter saved searches (empty = all — `types` defaults to empty; the 4 live MO counties are all probate, not foreclosure, despite the Actor's legacy name)
- `notice_site_username`, `notice_site_password`, `captcha_api_key`: secrets (required by the input schema for every Task, even though `scraper_ks.py` doesn't actually use them for `kansaspublicnotices` — put a placeholder value on a KS Task to satisfy Console's required-field check) — `tn_username`/`tn_password` still work as a fallback alias for the first two, kept for backward compatibility
- Every enrichment/CRM credential tested end-to-end 2026-07-22/23 has its own Actor input field: `anthropic_api_key`, `smarty_auth_id`/`smarty_auth_token`, `openwebninja_api_key`, `serper_api_key`, `firecrawl_api_key`, `tracerfy_api_key`, `trestle_api_key`, `ancestry_email`/`ancestry_password`, `datasift_email`/`datasift_password`, `slack_webhook_url` — all optional, all degrade gracefully if left blank (see `.actor/input_schema.json` for the full field list and descriptions)
- `google_drive_folder_id`, `google_service_account_key`: optional Google Drive upload

### Multi-platform Task setup (added 2026-08-05)

Running MO, NM, and KS as three separately-scheduled Apify Tasks against the one shared Actor turned out to hit a real gap, found while setting up the NM Task live in Apify Console: **Apify Console has no UI for setting a custom raw environment variable on a Task, or even on the Actor itself** — checked exhaustively (Task Input's "Run options" panel, and the Actor's Settings tab: Options, Actor Standby, Actor Permissions, Integrations — none of them expose one). Every other credential in this codebase (DataSift login, Smarty, Slack, etc.) already flowed through Actor Input, which `actor_main()` maps into `config.*`/`os.environ` at runtime — but `NOTICE_PLATFORM` was the one holdout still resolved from a bare `os.getenv()` at `config.py` **module import time**, before Actor Input is ever read. With no way to set that raw variable per Task, an NM or KS Task would have silently run against MO (the default) forever.

Fixed by making `config.py`'s platform resolution callable at runtime instead of import-time-only: `config.apply_notice_platform(platform: str | None = None)` (re)computes `NOTICE_PLATFORM`, `BASE_URL`, `LOGIN_URL`, `SMART_SEARCH_URL`, and `NOTICE_SITE_EMAIL`/`PASSWORD`. Called once automatically at module import with no argument — reads `NOTICE_PLATFORM` from the process environment exactly as the old code did, so the **CLI path is completely unaffected** (it still sets a real shell env var before Python starts). `main.py`'s `actor_main()` now calls `config.apply_notice_platform(actor_input.get("notice_platform"))` immediately after reading Task Input, before any other code touches `config.NOTICE_PLATFORM` or credentials — an unset/blank `notice_platform` input field is a no-op (keeps whatever the Actor's default already was), so existing MO Tasks that predate this field keep working with zero changes required. New `notice_platform` Input field added to `.actor/input_schema.json` (enum dropdown, optional, blank = Actor default).

Verified via `py_compile`, the full test suite (44 passed), and direct unit checks of `apply_notice_platform()`: default-import backward compatibility, runtime override to NM and KS, empty-string/`None` re-reading the current env (rather than erroring or freezing stale state), whitespace/case tolerance, and — importantly — that an invalid platform raises `ValueError` *without* corrupting the previously-resolved state (the failed call left `config.NOTICE_PLATFORM`/`BASE_URL` exactly as they were before the bad call, not half-updated).

**First live Apify run of the new NM Task (2026-08-05) caught a second, related bug in `scraper.py`.** `NOTICE_PLATFORM set from Task Input: newmexicopublicnotices` logged correctly and preflight passed, but the actual login attempt hit `https://www.mopublicnotices.com/authenticate.aspx` — MO's URL, not NM's — and failed with "Invalid Email Address or Password" (NM's credentials, correctly resolved, submitted against MO's login form). Root cause: `scraper.py` did `from config import (BASE_URL, LOGIN_URL, SMART_SEARCH_URL, ...)` — a direct name import, which snapshots the value once at `scraper.py`'s own import time (when `main.py` first imports it, long before `actor_main()` ever calls `apply_notice_platform()`) into `scraper.py`'s own namespace as an independent binding. Reassigning `config.LOGIN_URL` later has no effect on that already-bound local name. `config.NOTICE_SITE_EMAIL`/`PASSWORD` never had this problem because they were already accessed as `config.NOTICE_SITE_EMAIL` (a live attribute lookup) rather than direct-imported — which is exactly why the credentials were correct while the URL wasn't. Fixed by removing `BASE_URL`/`LOGIN_URL`/`SMART_SEARCH_URL` from `scraper.py`'s direct-import block and switching all 5 call sites (`login()`'s log line + `page.goto`, `_get_session_base_url()`, `_try_relogin()`, `_is_session_valid()`) to `config.BASE_URL`/`config.LOGIN_URL`/`config.SMART_SEARCH_URL`. `scraper_ks.py` was already unaffected — it computes its URL from a fixed `config.NOTICE_PLATFORMS["kansaspublicnotices"]` lookup, not the "current platform" value. Confirmed no other `src/*.py` file direct-imports these three names. Verified via `py_compile`, the full test suite (44 passed), and a targeted reproduction: import `scraper.py` first (mirroring `main.py`'s real import order), call `apply_notice_platform("newmexicopublicnotices")` afterward, confirm `config.LOGIN_URL` now resolves to NM's URL — reproduces the exact failure sequence from the live run and confirms the fix.

**Re-test confirmed live (2026-08-05), build 1.0.6 — NM Task fully closes the loop end to end.** Re-triggered the same Task manually. Log confirms all 5 things checked for: `NOTICE_PLATFORM set from Task Input: newmexicopublicnotices`, preflight passed, `Logging in to https://www.newmexicopublicnotices.com/authenticate.aspx` (the exact URL that failed pre-fix), `Login successful — on Smart Search dashboard`, and real notices for both counties. County re-labeling (the NM mislabeling fix from earlier this session) also fired correctly multiple times on real data in the same run (e.g. "County re-labeled: search='Bernalillo' but notice text names 'Sandoval' ... Re-labeling notice county: Bernalillo -> Sandoval"). Final pipeline summary: 24 total records (19 Bernalillo / 5 Sandoval), 24/24 mailable, 24/24 obituary-confirmed deceased, 24/24 decision-maker identified, Tracerfy/Trestle/PDF-report steps all ran, DataSift CSVs (24 DMs, 24 Heirs) written to KVS, Slack notified, "Done — 24 notices exported (53.4 min)", exit code 0.

Two things noticed in this run, both investigated and closed out 2026-08-05:

**Output tab showing 0 — root-caused as a real bug, fixed.** `push_batch()` in `main.py` (the `Actor.push_data()` callback wired to `scrape_all()`'s `on_batch` param) was deduping against the wrong set. It checked `nid in seen_ids` — but `seen_ids` is the cross-run KVS dedup cache, and `_scrape_results_page()` already writes every notice's ID into that exact same dict (`seen_ids[notice_id] = ...`) the moment it's parsed, *before* the batch ever reaches `push_batch()`. So every notice looked "already seen" from `push_batch()`'s point of view and got `continue`d past — `unique` was always empty, `Actor.push_data()` was never actually called, and `Actor.log.info("Pushed...")` never fired. Confirmed this wasn't NM-specific: every MO run in its full history (builds 1.0.2 through 1.0.5, all the way back to when the schedule started) shows "Results: 0" in Console despite being real successful nightly runs with populated CSVs. There's a second bug bundled in the same code that never actually got reached because of the first one: `seen_ids` starts life as `set()` but gets reassigned to a `dict` (loaded from KVS) before `push_batch()` ever runs, and the old code called `seen_ids.add(nid)` — `dict` has no `.add()`, so fixing the dedup logic alone would have immediately traded one silent bug for a loud crash. Fixed by giving `push_batch()` its own separate `pushed_ids: set[str] = set()`, scoped to "already pushed to the dataset this run" — completely independent of the cross-run KVS cache, so it no longer treats brand-new notices as duplicates, and `.add()` is now called on an actual `set`. Verified via `py_compile`, the full test suite (44 passed), and a direct reproduction of both the original bug (confirms `unique` was always empty) and the fix (confirms real notices now get pushed, and a genuinely-repeated batch is still correctly deduped). Does not touch the real deliverable path (KVS CSVs) at all — this only affects the Console's Output/Results tab, a monitoring-only side channel. **Not yet re-verified against a real Apify run** — next scheduled or manual trigger should show a non-zero Results count for the first time.

**0/24 property addresses — confirmed structural, not a bug or a one-batch coincidence.** Every one of the ~30 notices processed in the live run got a PR/executor address filled by the LLM extractor (near-100% hit rate, visible as repeated `LLM filled PR address: ...` log lines) but zero got a property address — `address_standardizer` logged "No notices with addresses to standardize" and `property_enricher` logged "No notices with addresses to enrich" for the whole batch. Checked `notice_parser.py`'s property-address extraction (`_PROP_INDICATOR`, ~line 191): it's a comprehensive set of legal-notice phrasings (`commonly known as`, `property located at`, `bearing the address of`, etc.) — but those are foreclosure/trustee-sale phrasings, used when a notice is announcing the sale of a specific property. Probate notices are a different legal document entirely — they announce an estate proceeding (decedent, PR, court case number, creditor-claim instructions), and have no legal reason to ever state the property's address. The extraction logic isn't missing anything; the text itself structurally doesn't contain it. This is the same shape as Kansas's finding earlier this session and the original reason Knox County's Tax-API property-lookup tier exists — not an NM-specific anomaly, very likely true of MO's and every other market's probate notices too (untested elsewhere, but the underlying reason — probate notices vs. foreclosure notices being different document types — applies everywhere equally). Records are still 100% mailable via the PR/DM address, so this isn't blocking anything. **Backfilling property addresses, if wanted, is a downstream enrichment decision, not an Actor change** — Bernalillo/Sandoval have no known free public tax-assessor API the way Knox does (would need research first), and the `probate-property-finder` Cowork skill already exists for exactly this gap (assessor/CAD site + deed record lookup from a decedent name and county, no address required as input). Decision needed from Kaycie: (a) scope a NM-specific automated lookup tier like Knox's — real research + build project, not a quick fix; (b) use `probate-property-finder` as an on-demand downstream step for NM records that need one — no new engineering; or (c) leave as-is — DM-only mailing already works, 100% mailable rate unaffected.

NM's scheduled-Task path is now considered verified — ready to move on to actually scheduling it (see "Multi-platform Task setup" above for the still-outstanding piece: no cron schedule attached yet, and NM is intentionally still excluded from `APIFY_TASKS` pending this confirmation, which just landed).

**NM Property-Address Lookup Tier (built 2026-08-06) — Kaycie chose option (a) above.** `src/nm_parcel_lookup.py` is a new module implementing the same 3-tier shape as Knox's `tax_enricher._probate_property_lookup()` (decedent name → executor/PR family property → people search), but against a different data source: New Mexico's Office of the State Engineer (OSE) hosts a statewide ArcGIS parcels REST service (`gis.ose.nm.gov/server_s/rest/services/Parcels/County_Parcels_2025/MapServer`, sourced from NM Taxation & Revenue Dept PTD aggregating every county's own CAMA data) with a dedicated layer per county — layer 0 = Bernalillo, layer 23 = Sandoval — so one service covers both target counties (and, if useful later, all 33 NM counties). Confirmed live during scoping: public, unauthenticated, standard `/query` endpoint supporting `WHERE UPPER(Owner1) LIKE UPPER('%NAME%')`, ~300ms response time, 5000-record page size (no pagination needed for a name search). This is actually a simpler integration than Knox's own Tax API (no bespoke endpoint shape, well-documented ArcGIS query syntax, no auth flow).

Two data-format quirks found and handled: Bernalillo's `Owner1` field is "Last First Middle" (no comma) while Sandoval's is "Last, First Middle" (comma) — `_ose_name_query()` strips commas before scoring so this doesn't affect the token-overlap match (reuses `tax_enricher._name_match_score`, unchanged, already comma/order-agnostic). And a small Sandoval sample had blank `SitusAddressAll` despite real owner data — `_parse_situs_address()` returns `None` for a blank/unparseable value rather than writing a garbage address, which is exactly why Tiers 2/3 still exist as fallback rather than assuming Tier 1 always succeeds.

Wired into `enrichment_pipeline.py`'s existing Step 3c (previously Knox-only) — the block was split into two independent candidate lists/try-blocks, one routing `county.lower() == "knox"` to `tax_enricher._probate_property_lookup`, the other routing `county.lower() in ("bernalillo", "sandoval")` to the new `nm_parcel_lookup.probate_property_lookup_nm`. Same placement decision as Knox: a downstream enrichment step, not baked into the Apify Actor run itself.

While building this, spotted (but did not fix — Knox is dormant/legacy, out of scope) a pre-existing bug in Knox's own Tier 3: `tax_enricher._people_search_property` imports a `_fetch_page` helper from `obituary_enricher.py` that doesn't actually exist there (only `_fetch_page_text` does), so Knox's people-search fallback has silently no-op'd on every call, caught by a bare `except Exception` that logs at debug level. The new NM module (`_people_search_property_nm`) uses the correct `_fetch_page_text` name rather than copying the bug forward. Worth a follow-up one-line fix in `tax_enricher.py` if Knox ever gets real usage again.

Verified via `py_compile`, 23 new unit tests in `tests/test_nm_parcel_lookup.py` (situs-address parsing incl. blank/no-house-number/multi-word-city edge cases, surname extraction incl. suffix stripping, mocked-HTTP scoring/sorting/comma-normalization/error-handling for the OSE query, and end-to-end Tier 1/2/3 fallthrough with mocked HTTP), and the full existing test suite (130 passed, no regressions).

**Live-tested end-to-end 2026-08-06, build 1.0.8 — confirmed working on real Bernalillo probate records.** Manually triggered the NM Task (supervised, `upload_datasift` untouched — the Actor path doesn't upload to DataSift directly regardless of that flag, see "DataSift Upload Is a Two-Stage Pipeline" below). 13 probate notices scraped, all Bernalillo (pagination stopped on page 1 — a known, already-documented NM reliability issue, not new). Step 3c found property addresses for **9/13 (69%)**: Tier 1 (direct OSE match) hit 6 — Keryte, Yilek, Maddox, Frank, Salley, Stokes; Tier 2 (executor family) hit 2 — Jaramillo, Lucero; Tier 3 (people search) hit 1 of 5 attempts — Anaya. 4 not found — 2 genuine misses (Medina, Lopez) and 2 caused by a separate upstream name-garbling bug (see below), not by the lookup tier itself. This run also gave the first live confirmation that the `push_batch` dedup fix (build 1.0.7, see "Output tab showing 0" above) actually works: Console Output tab showed 13 results for the first time ever, matching the real record count.

**Found and fixed the same day: a decedent-name PDF line-wrap hyphenation bug, surfaced by this live test.** 2 of the 4 property-lookup misses (Diana Lynn Irwin, Roberta Anderson) turned out to have nothing to do with the NM parcel service — `nm_parcel_lookup` was correctly searching for garbled names ("Diana Lynn Ir- Win", "Roberta An- Derson") because that's what `notice.decedent_name` already contained by the time Step 3c ran. Root cause: `DECEDENT_NAME_RE`'s captured character class allows an inline hyphen, and the PDF text extraction step had already collapsed the original "IR-\nWIN" line-wrap into "IR- WIN" (hyphen-space, no newline) before the regex ever saw it — the exact same underlying artifact `_dehyphenate()` fixes for county names (`_LINE_WRAP_HYPHEN_RE`, added 2026-08-01) and that CLAUDE.md's Kansas section flagged as a "hyphen-space, no newline" gotcha (2026-08-04), just showing up in a third place: decedent names specifically. `_dehyphenate()` itself couldn't catch it — its regex requires a literal `\n`, which was already gone. Confirmed the LLM fallback actually got these names right on the same raw text (`llm_parser: LLM extracted: decedent='DIANA LYNN IRWIN'...`) — but since the regex path had already produced a non-empty (just wrong) value, the LLM backfill logic (which only fills empty fields) never got a chance to correct it.

Fixed via a new `_NAME_LINE_WRAP_HYPHEN_RE` regex (`-\s+`) applied inside `_clean_name()`, merging a hyphen immediately followed by whitespace before title-casing splits the fragments into separate "words". Scoped narrowly on purpose: a genuine hyphenated name (e.g. "Smith-Jones") has no whitespace around the hyphen in source text, so this signal shouldn't touch real compound surnames — verified via a dedicated regression test (`probate-genuine-hyphenated-surname`) alongside two tests reproducing the exact Irwin/Anderson garbling.

While adding that regression test, found a second, unrelated pre-existing bug in the same area: `PROBATE_NAME_RE` (the PR/executor name pattern) never had a hyphen or apostrophe in its capture character class at all (unlike `DECEDENT_NAME_RE`, which already had both) — so any PR name containing either (e.g. "Smith-Jones", "O'Brien") silently failed to match and left `owner_name` empty, no error, no log line. Fixed by aligning `PROBATE_NAME_RE`'s character class to match `DECEDENT_NAME_RE`'s. Both fixes verified via `py_compile`, the expanded edge-case suite (`tests/test_parser_edge_cases.py`, run directly per its own header — not pytest-collected, same as before — 181/181 passed, up from 178), `tests/test_parser.py` (all passed), and the full pytest suite (130 passed, no regressions). **Not yet re-verified against a live run** — the fix is regression-tested but the next real NM/MO/KS scrape will be the first live confirmation that Irwin/Anderson-style names come through clean.

### Known Gaps in the Actor Path (as of 2026-07-24)
- **Preflight checks**: `_preflight_check()` (2Captcha balance, site connectivity, missing-credential warnings) now runs on the Actor path too, same as CLI — added 2026-07-24 after discovering the Actor path only had a hand-rolled partial credential check with no balance/connectivity signal. A failed preflight now calls `Actor.fail()` with a clear status message and fires the same Slack alert as the CLI path.
- **Dedup state does not carry over between the CLI and the Actor.** The CLI path tracks seen notice IDs in local `seen_ids.json`; the Actor path tracks them in Apify's Key-Value Store (`seen_notice_ids`). Running the CLI manually against the same counties/window a scheduled Actor run also covers does not mark anything as seen for the Actor, and vice versa — not a data-loss risk, but a "0 new notices" result from one path says nothing about the other.
  - **Decision (2026-08-04): document + operating rule, not a code unification.** A real fix (e.g. the CLI reading/writing Apify's KVS over its API) would mean giving every local/manual run network access to Apify credentials, adding latency to routine local testing, and handling the case where the Apify API itself is unreachable — a real cost for a divergence that, on its own, only causes redundant re-scraping (annoying, not harmful) since a fresh scrape of an already-uploaded notice just produces the same record again, not corrupted data.
  - **The place this stops being harmless is `--upload-datasift`.** Two independent uploads of the same notice (one via a manual CLI run, one via the scheduled Actor's stage-2 pipeline) create two separate DataSift records for the same property rather than one — duplicate leads in the CRM, not just wasted scrape time.
  - **Operating rule:** don't run `python src/main.py daily --upload-datasift` (or `historical --upload-datasift`) manually against counties/date ranges already covered by a scheduled Apify Task (currently: the MO daily schedule) without first checking whether that Task already ran for the same window — check the Apify Console's run history, or ask whoever's monitoring Slack, before kicking off a manual upload run. Manual runs *without* `--upload-datasift` (e.g. for local testing, like the NM live-test runs earlier this session) are always safe regardless of overlap — they never touch DataSift.

### DataSift Upload Is a Two-Stage Pipeline for Scheduled Actor Runs (added 2026-07-26)

The Actor path intentionally does **not** upload to DataSift itself (see upstream commit `e5e2d509`, "Replace automated DataSift upload with manual CSV download links" — headless Playwright inside Apify's Actor container was found unreliable for the upload wizard's SPA timing). Instead it writes DataSift-formatted CSVs (already fully enriched — Smarty, Zillow, tax, obituary/DM, Tracerfy, Trestle all ran) to the Apify Key-Value Store and posts download links to Slack for manual upload.

For the scheduled 2:00 AM `America/Phoenix` run — meant to give a 2-hour head start before Rahaf's 4:00 AM start — a manual step defeats the purpose, since nobody is awake at 2am to do it. **Stage 2 closes that gap without touching the Apify container:**

- **`src/stage2_datasift_upload.py`** — queries the Apify API for the last `SUCCEEDED` run, pulls the `datasift_*.csv` records straight out of that run's KVS, and calls the same `upload_to_datasift`/`upload_datasift_split` Playwright automation the CLI's `--upload-datasift` flag uses — no re-enrichment, since the CSVs pulled from KVS are already final. Refuses to run if the last SUCCEEDED run is more than `MAX_RUN_AGE_HOURS` (6h) old, to avoid silently re-uploading a stale prior-day run under a wrong-dated list name.
- **Multi-platform (2026-08-05).** The original version scoped its "last SUCCEEDED run" lookup to the shared Actor ID (`tn-public-notice-scraper`, `DLE4KmqvBSxrxGUab`) — correct with exactly one scheduled thing hitting that Actor, but silently wrong the moment a second platform (NM, then KS) got its own scheduled Apify Task on the same Actor: it would grab whichever platform's run happened to finish last, not necessarily the one the caller wanted. Fixed by scoping lookups to a Task ID instead (`GET /v2/actor-tasks/{taskId}/runs/last`), driven by a new `APIFY_TASKS` env var (`"MO:taskId,NM:taskId,KS:taskId"`) — the script now loops over every platform listed there independently, catching one platform's failure without blocking the others, and tags every log line, Slack notification, and DataSift list name with its platform label (`"SiftStack 2026-08-05 NM - DMs"`) so same-day lists from different platforms never collide. Backward compatible: if `APIFY_TASKS` is unset, falls back to the original single-actor behavior via `APIFY_ACTOR_ID` unchanged. **Rollout gate: only platforms actually listed in `APIFY_TASKS` get auto-uploaded** — per the 2026-08-04 decision, NM and KS should stay out of that secret until each has completed at least one clean, unattended scheduled run on scrape-only mode; until then they stay on manual Slack-links upload, same as MO before stage2 existed. Verified via `py_compile` and direct unit checks of the new parsing function (unset/legacy fallback, three-platform, malformed-entry-skipped, whitespace-tolerant, empty-string-as-unset) — **not yet live-tested against a real multi-Task Apify account**, since that requires Task IDs that don't exist until the NM/KS Tasks are created in Apify Console (tracked separately).
- **`.github/workflows/stage2-datasift-upload.yml`** — runs this script unattended on a plain GitHub Actions Ubuntu runner (a different environment than Apify's Actor container — the whole point of the split) at `30 10 * * *` UTC = 3:30 AM `America/Phoenix`, comfortably after MO/NM/KS's staggered 2:00/2:20/2:40 AM Apify Task runs and 30 min before Rahaf's 4am start. Needs repo secrets: `APIFY_TOKEN`, `APIFY_TASKS` (the multi-platform list, once NM/KS are ready), `APIFY_ACTOR_ID` (optional legacy fallback, defaults to the ID above), `DATASIFT_EMAIL`, `DATASIFT_PASSWORD`, `SLACK_WEBHOOK_URL` (optional).
- **Known limitation, not yet solved:** no idempotency check against DataSift itself. If the GitHub Actions job is manually re-run after a partial failure, it will create a second DataSift list with the same `list_name` and re-upload, rather than detecting the first attempt already landed. Low risk given the workflow only runs once nightly on a schedule, but worth knowing before using `workflow_dispatch` to retry a failed run. This applies per-platform now, not just to the single MO run.
- **Partially live-tested 2026-07-26 (MO, single-platform).** The Apify-API half (find last SUCCEEDED run, freshness guard, list + download `datasift_*.csv` from KVS) was run for real against the production Actor and produced two valid 4-row CSVs matching that run's actual output. The Playwright upload half could not be exercised in that test environment — Chromium's headless shell failed to launch there (`libXdamage.so.1` missing, no root to `apt install` it), which is a sandbox limitation, not a signal about GitHub Actions: `ubuntu-latest` runners have full apt access and the workflow uses `playwright install --with-deps chromium`, the same dependency-resolution path the working local CLI upload already relies on. Still not yet validated against a real end-to-end cycle for MO, and not at all yet for NM/KS's new multi-platform path — treat the first few nights as a supervised rollout, and consider triggering the workflow manually via `workflow_dispatch` once secrets are set up to get a real signal before trusting the cron.

### Actor Output
- **Dataset**: structured records pushed via `Actor.push_data()`
- **Key-value store**: `output.csv` backup
- **Google Drive** (optional): CSV + summary text file uploaded via service account

### Key Files
- `.actor/actor.json` — Actor manifest (name, version, Dockerfile path)
- `.actor/input_schema.json` — Input fields + validation for Apify Console UI
- `Dockerfile` — Based on `apify/actor-python-playwright:3.12`
- `src/drive_uploader.py` — Google Drive upload via base64-encoded service account key
- `input.json` — Local test input (gitignored, contains credentials)

## Courthouse Photo Pipeline (build 1.0.28+)

Courthouse terminal photos → OCR → LLM parse → enrichment → DataSift. Runner takes phone photos at county terminals, uploads to Dropbox organized as `{county}/{notice_type}/`, system auto-processes. `dropbox_watcher.py` already resolves county/notice_type generically from the folder path (not restricted to Knox/Blount — see its module docstring), so the photo-import mechanics work for any of the 8 active counties. **The Probate Deep Prospecting property-address lookup below is currently Knox-only** (built on the Knox Tax API, which has no equivalent free tier for the other counties) — for any other county, that lookup is skipped gracefully rather than guessing.

### Notice Types (7 total)
- `foreclosure`, `tax_sale`, `tax_delinquent`, `probate` — existing from web scraper
- `eviction` — plaintiff = landlord (target contact), defendant = tenant
- `code_violation` — owner of record, violation type, compliance deadline
- `divorce` — petitioner + respondent, property from schedule page

### Critical OCR Patterns (hard-won from live testing)

**Moire pattern from terminal screens is the #1 OCR killer.** Standard Tesseract preprocessing (adaptive threshold, CLAHE) produces garbage on courthouse terminal photos. The fix:
- **Bilateral filter** (`cv2.bilateralFilter(gray, 15, 75, 75)`) removes moire while preserving text edges
- **Otsu threshold** (`cv2.THRESH_BINARY + cv2.THRESH_OTSU`) after bilateral — auto-determines optimal binary threshold
- **PSM 4** (single column variable text) for terminal screens — NOT PSM 6 (single uniform block) which was the research recommendation but fails in practice
- **Do NOT use `fix_rotation()` (Tesseract OSD) on phone photos** — EXIF transpose handles rotation. OSD on raw phone images often fails and the 270° fallback rotates correct images sideways

### Probate Deep Prospecting (from courthouse terminals) — Knox County only

Courthouse probate records have decedent name + PR/executor name but NO property address. Multi-tier lookup fills the gap. **This entire lookup is scoped to Knox County** (`enrichment_pipeline.py` filters to `county.lower() == "knox"` before calling it) — none of the 8 active OK/MO/KS/NM counties have an equivalent free tax-API tier, so this doesn't run for them yet. See `config.COUNTIES[county].assessor_url` for each county's reference-only assessor site.

**Property Address Lookup** (Step 3c in enrichment pipeline):
1. **Tier 1: Knox Tax API name search** — search `/parcels/{decedent_name}`, score by token overlap (FIRST MIDDLE LAST → LAST FIRST MIDDLE), accept >= 0.4 match. Tries multiple name variations (with/without suffix, LAST FIRST format, first+last only).
2. **Tier 2: Executor family search** — search Knox Tax API by executor name, look for properties where decedent's last name appears in owner field (family property transferred to executor).
3. **Tier 3: People search** — search TruePeopleSearch/FastPeopleSearch for decedent's last known Knox County address.

**Probate Preset** (obituary enricher):
- Triggers when court record has PR name + decedent name (no address required) — prevents wrong obituary from overriding court-named executor
- Sets DM = the named PR/executor directly, skips obituary search entirely
- Then runs DM address lookup (Knox Tax API → People Search → Tracerfy)

**DOD Sanity Check** (obituary enricher):
- Rejects obituary matches where DOD is > 3 years before the notice filing date (`MAX_DOD_GAP_YEARS = 3`)
- Prevents matching a 2014 obituary to a 2025 court filing (wrong person with same name)
- Applied to both full-page and snippet matches

### Dropbox Folder Structure

`{county}` matches any county name in `config.COUNTIES` (not restricted to Knox/Blount) — the pattern below shows the 4 live Missouri counties as an example alongside the dormant original market:
```
{DROPBOX_ROOT_FOLDER}/
├── Jackson/
│   ├── eviction/
│   ├── code_violation/
│   ├── divorce/
│   ├── foreclosure/
│   ├── tax_sale/
│   └── probate/
├── Clay/
│   └── (same subfolders)
├── Platte/
│   └── (same subfolders)
├── Cass/
│   └── (same subfolders)
├── Knox/                    (dormant/legacy market)
│   └── (same subfolders)
└── Blount/                  (dormant/legacy market)
    └── (same subfolders)
```

### Environment Variables
- `DROPBOX_APP_KEY` — Dropbox OAuth2 app key
- `DROPBOX_APP_SECRET` — Dropbox OAuth2 app secret
- `DROPBOX_REFRESH_TOKEN` — Dropbox offline refresh token (auto-rotates access tokens)
- `DROPBOX_POLL_INTERVAL` — seconds between polls (default 900 = 15 min)
- `DROPBOX_ROOT_FOLDER` — root folder path in Dropbox (e.g., "Bluetail Courthouse Photos")

### Dependencies (added to requirements.txt)
- `opencv-python-headless>=4.13.0` — image preprocessing (headless = no GUI, saves 26MB in Docker)
- `numpy>=1.26.0` — required by OpenCV
- `dropbox>=12.0.2` — Dropbox SDK (minimum for post-Jan-2026 API compatibility)

## DataSift.ai (REISift) Integration

DataSift.ai (formerly REISift) is the CRM where scraped records land for niche sequential marketing campaigns. There is **no REST API** — upload is via Playwright browser automation of the web UI.

**Domain:** `app.reisift.io` (NOT `app.datasift.ai`). API at `apiv2.reisift.io`.

### Key Files
- `src/datasift_formatter.py` — Transforms `NoticeData` → DataSift CSV (41 columns)
- `src/datasift_uploader.py` — Playwright login + upload wizard + enrich + skip trace + preset management + sequence builder + SiftMap sold workflow
- `test_datasift_upload.py` — Headed browser test (upload + enrich + skip trace)
- `test_manage_presets.py` — Headed browser test (preset discovery + sold exclusion + sequence creation)
- `test_manage_sold.py` — Headed browser test (SiftMap sold property tagging)

### CSV Column Structure (41 columns)
- **Core auto-mapped (11):** Property Street/City/State/ZIP, Owner First/Last Name, Mailing Street/City/State/ZIP, Tags
- **Lists + Notes (2):** Lists (for niche sequential), Notes (contextual per notice type)
- **Built-in fields (13):** Estimated Value, MSL Status, Last Sale Date/Price, Equity Percentage, Tax Deliquent Value, Tax Delinquent Year, Tax Auction Date, Foreclosure Date, Probate Open Date, Personal Representative, Parcel ID, Structure Type, Year Built, Living SqFt, Bedrooms, Bathrooms, Lot (Acres)
- **Custom fields (15):** Notice Type, County, Date Added, Owner Deceased, Date of Death, Decedent Name, Decision Maker, DM Relationship, DM Confidence, DM 2/3 Name/Relationship, Obituary URL, Source URL

### Niche Sequential Marketing
DataSift's niche sequential system uses filter presets to guide records through SMS → Call → Mail → Deep Prospecting phases. Two preset folders: "00 Niche Sequential Marketing" (12 presets, courthouse data) and "01. Bulk Sequential Marketing" (9 presets, bulk data). All 21 presets exclude Sold status (build 1.0.23). A "Sold Property Cleanup" sequence in the Transactions folder auto-fires on "Sold" tag to change status, remove from lists, clear tasks, and clear assignee.

- **"Courthouse Data" tag:** Every record gets this tag — signals first-to-market county data (prioritized over bulk data in filter presets)
- **Lists column:** Maps `notice_type` → DataSift list name (`foreclosure` → "Foreclosure", `probate` → "Probate", `tax_sale` → "Tax Sale", `tax_delinquent` → "Tax Delinquent", `eviction` → "Eviction", `code_violation` → "Code Violation", `divorce` → "Divorce"). DataSift auto-creates lists from CSV.
- **Tags:** Courthouse Data, notice_type, county, YYYY-MM date, deceased/living, DM confidence level, has_auction, tax_delinquent, photo_import (for photo-sourced records)

### Upload Wizard (5 Steps)
1. **Setup:** Click "Upload File" sidebar → "Add Data" → dropdown "Uploading a new list not in DataSift yet" → enter list name → organization questions
2. **Tags:** Skip through (tags are in CSV column)
3. **Upload File:** Set file on `input[type="file"]`
4. **Map Columns:** Core address fields auto-map; Tags, Lists, and enrichment columns may need manual mapping
5. **Review + Finish Upload:** Click "Finish Upload" — processing happens in background

### Column Mapping Notes
- Only core address fields (Property Street, City, State, ZIP) reliably auto-map
- Tags, Lists, Estimated Value, and enrichment columns often stay unmapped in step 4
- Notes and MSL Status sometimes auto-map
- Custom fields (the "TN Public Notice" custom-field group in your live DataSift account) require drag-and-drop mapping — this is a group name configured in the DataSift UI itself, not something this codebase sets; rename it in your DataSift account if you want it to reflect the current markets

### Contact Logic
- **Deceased owners:** Contact = decision maker (first/last name + mailing address from DM)
- **Living owners:** Contact = property owner (owner mailing address, falls back to property address)

### Post-Upload: Enrich + Skip Trace

After CSV upload, the pipeline automatically runs two DataSift actions via Playwright:

1. **Enrich Property Information** (Manage → Enrich Data): Adds SiftMap property data (beds, baths, Zestimate, sqft, sale history) to uploaded records. "Enrich Owners" and "Swap Owners" are OFF — protects our PR/DM contact mapping.
2. **Skip Trace** (Send To → Skip Trace): Pulls phone numbers (up to 5 per owner) + emails via unlimited plan ($97/mo). Adds auto-tag `skip_traced_YYYY-MM`.

Both run in background — tracked in Activity tab. Both are ON by default when `--upload-datasift` is set.

### CLI Flags
```bash
python src/main.py daily --upload-datasift        # upload + enrich + skip trace
python src/main.py daily --upload-datasift --no-enrich       # upload only, skip enrichment
python src/main.py daily --upload-datasift --no-skip-trace   # upload + enrich, skip skip trace
python src/main.py daily --notify-slack            # send run summary to Slack/Discord
```

### Environment Variables
- `DATASIFT_EMAIL` — DataSift login email
- `DATASIFT_PASSWORD` — DataSift login password
- `SLACK_WEBHOOK_URL` — Slack/Discord webhook for run summaries

### Login Selectors (SPA quirks)
- Hidden checkboxes (Remember me, Terms) — click `<label>` elements, not `<input>`
- Use `wait_until="domcontentloaded"` (not `networkidle` — SPA keeps WebSocket connections open)
- Cookie validation: check for `/dashboard` or `/records` in URL (5s wait for SPA redirect)

### DataSift UI Automation Patterns

Hard-won patterns from build 1.0.22-1.0.23 (SiftMap, preset management, sequence builder). Follow these to avoid repeating past mistakes.

**Styled-Components (no native HTML controls)**
- No native `<select>` elements — all dropdowns are `[class*="Selectstyles__Select"]` containers
- `[class*="SelectValue"]` = current value display; `[class*="SelectOptionContainer"]` = dropdown options
- Multiple Select dropdowns exist per panel (Lists, Tags, Property Status) — always target the **LAST visible one**
- Use `x > 450` bounds check in all JS queries to avoid matching sidebar elements (sidebar is 0-400px)
- React state updates require native setter + event dispatch, not just `.value = ...`:
  ```js
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, 'new value');
  input.dispatchEvent(new Event('input', {bubbles: true}));
  input.dispatchEvent(new Event('change', {bubbles: true}));
  ```

**Panel Scrolling (Playwright scroll fails)**
- Filter panel is a scrollable `<div>`, NOT the viewport — `scroll_into_view_if_needed()` does nothing
- Use JS: `el.scrollIntoView({behavior: 'instant', block: 'center'})` instead
- Filter Presets section is at the BOTTOM of the filter panel — must scroll container down to reveal
- After scrollIntoView, element y-positions may be negative — don't filter by `y > 0` for the target element

**React DnD (Sequence Builder)**
- Cards have `draggable="false"` — Playwright's native drag won't work
- Must use slow mouse drag: `mouse.move()` → `mouse.down()` → 20 incremental steps (50ms each) → `mouse.up()`
- Add 500ms pauses between down/move/up phases
- "Add new Action +" button required for 2nd+ actions; first action uses initial drop zone
- Sidebar cards can scroll out of view when main area scrolls — scroll BOTH source and target into view before drag

**Pointer Interception (common blockers)**
- Beamer NPS survey iframe (`#npsIframeContainer`) blocks ALL pointer events globally — remove from DOM via `_dismiss_popups()`
- `RecordsFiltersstyles__RecordsFiltersSection` elements intercept clicks — use `page.evaluate()` JS click or `force=True`
- When Playwright click fails with "outside of viewport" or "intercept": switch to `page.evaluate(el => el.click())`
- SiftMap PropertyDetails panel blocks sidebar checkboxes — remove from DOM before interactions

**Preset Management Workflow**
- Flow: open filter panel → scroll to bottom → expand "Filter Presets" → expand folder → click preset → modify → Save (not Save New) → confirm overwrite
- Folder names have case variations ("00 Niche" vs "00 NICHE") — use `.toUpperCase()` comparison
- Preset names follow pattern `^\d{2}\.` (e.g., "00. Needs Skipped")
- 2 folders: "00 Niche Sequential Marketing" (12 presets), "01. Bulk Sequential Marketing" (9 presets)
- All 21 presets have Property Status "Do not include" → "Sold" (build 1.0.23)

**Sequence Builder Workflow**
- Flow: `/sequences` → Create → title + folder → drag trigger → condition → actions tab → drag actions → configure → save
- Duplicate name handling: detect error toast "different sequence title", retry with " V2" suffix
- Actions tab: navigate via "Set the Following Actions" button or URL (`/sequences/new/actions`)
- Autocomplete inputs: after each selection, `fill("")` + Escape to dismiss dropdown before next entry
- "Sold Property Cleanup" sequence exists in Transactions folder (build 1.0.23): Trigger (Property Tags Added) → Condition (Sold) → Actions (Status→Sold, Remove Lists, Clear Tasks, Clear Assignee)

**SiftMap Automation**
- Search by county-level FIPS code via direct URL (`?location={json}` with `searchType: "county"`, county name, state, and FIPS code) — far more reliable than interacting with the search UI. FIPS codes for all 8 active counties + Knox/Blount are in `datasift_uploader.py`'s `COUNTY_FIPS` dict, sourced from the FCC's authoritative state+county FIPS reference. State/title are derived from `config.state_for_county()`, not hardcoded to TN.
- PropertyDetails panel auto-opens on search — remove from DOM before other interactions
- "Add Records to Account" modal: toggle OFF "Do not replace owners", add tags, dismiss dropdown by clicking heading (NOT Escape — clears tags)
- Known limitation: SiftMap filters (price, date) set values visually but don't trigger React re-query. Only sidebar-visible properties (~3-5) get added per run

**Market Finder Extraction Patterns (build 1.0.29+)**

Hard-won patterns from building `extract_market_finder.py`. The Market Finder UI differs significantly from the rest of DataSift.

- **NO HTML `<table>` element** — data table is entirely div-based: `Tablestyles__TableContainer` → `TableRow` → `TableCell` (styled-components). Searching for `<table>` or `<tr>/<td>` finds nothing.
- **PAGINATION, not infinite scroll** — table shows 20 rows per page with "1-20 of N" text and `PaginationInnerContainer` with prev/next `<button>` elements. Must click through ALL pages to get complete data. Knox County has 48 ZIPs (3 pages) and 120+ neighborhoods (7 pages).
- **State/County selection uses `InputMultiSearch`** — NOT styled-component Select dropdowns. Inputs have placeholders: `"Select States"`, `"Select Counties"`, `"Select ZIP Codes"`. Click input → type name → click dropdown result item (`[class*="Item"]:has-text("...")`).
- **ZIP/Neighborhood toggle is a styled Select dropdown** — at the top bar with `Selectstyles__SelectValue` showing current view. Check the displayed text BEFORE clicking — if already on the correct view, clicking toggles AWAY from it. Only click to switch if the displayed text doesn't match the desired view.
- **Beamer push modal (`#beamerPushModal`)** — appears on fresh login, blocks ALL pointer events. Different from the NPS survey (`#npsIframeContainer`). Both must be removed from DOM before any click interactions. Always call dismiss with `force=True` as fallback.
- **Page body scrolling required** — pagination controls are at `y=1867`, below the viewport (`clientH=824`). Must scroll `AdminPage__AdminPageBody` container down before pagination buttons are accessible.
- **Summary panel on right side** — shows county-level aggregates: Median Home Value, Homes on Market, Mo. Investor Transactions, Homes Sold Last Month, Market Rent, Gross Rental Yield, Homeownership Rate. Extract via regex on page text.

```bash
# Extract all Market Finder data for a county
python src/extract_market_finder.py --state "Missouri" --county "Jackson" -v
python src/extract_market_finder.py --state "Missouri" --county "Jackson,Clay" --headless

# Output: JSON file in output/market_finder_{state}_{county}_{timestamp}.json
```

## REI Skill Library (13 Skills)

Distribution-ready Claude Co-Work skill files at `Skills for REI/improved/`. Each `.skill` is a ZIP containing `SKILL.md` + `references/` folder. Plugins (`.plugin`) also include `commands/` and `.claude-plugin/plugin.json`.

### Skill Inventory

| # | File | Division | Score | What It Does |
|---|------|----------|-------|-------------|
| 1 | `sift-market-research.skill` | Market Intel | 9.6 | Market Finder reports, zip code scoring (6 weights verified against `market_analyzer.py`), 7-sheet Excel output |
| 2 | `first-market-county-data.skill` | Market Intel | 9.7 | County clerk data extraction for all 7 notice types, FOIA templates, marketing windows |
| 3 | `buyer-prospector.skill` | Market Intel | 9.6 | Cash buyer list from 84K+ records, LLC/trust/corp research, 50-state SOS URLs |
| 4 | `real-estate-comping.skill` | Deal Analysis | 9.7 | Two-Bucket ARV, disclosure/non-disclosure routing (12 states), adjustments verified against `comp_analyzer.py` |
| 5 | `rehab-estimator.skill` | Deal Analysis | 9.8 | 912-line skill, complete Repair Cheat Sheet verified against real contractor SOW, 4-tier system |
| 6 | `deal-analyzer.plugin` | Deal Analysis | 9.6 | Combined comp+rehab pipeline, MAO (75%/70% rules), multi-loan financing, exit strategy comparison |
| 7 | `deep-prospecting.skill` | Deal Analysis | 9.6 | 4-level research depth (L1-L4), heir verification loop, DOD sanity check (3yr), 3-site skip trace waterfall |
| 8 | `probate-property-finder.skill` | Deal Analysis | 9.7 | Property lookup for probate decedents, 3-tier search (Tax API→Executor→People search), confidence scoring |
| 9 | `phone-validator.skill` | Operations | 9.8 | Trestle API scoring, 5-tier dial priority, 3 tier strategies, litigator risk check, 4.75x connect rate |
| 10 | `sequential-presets.skill` | Operations | 9.5 | 12 niche + 9 bulk filter presets, Pendulum Theory (SMS→Call→Mail→DP), DataSift UI implementation steps |
| 11 | `sift-sequences.skill` | CRM | 9.5 | 26 TCA sequence templates (verified against `sequence_templates.py`), UI walkthrough, HOT A01-A16 chains |
| 12 | `sift-operations.plugin` | CRM | 9.3 | CRM operations encyclopedia, STABM routine, lead pipeline (9 statuses), task presets, team roles |
| 13 | `playbook-creator.skill` | Operations | 9.5 | Playbook/SOP generator from transcripts, 7-node chart limit, 5th grade reading level, Word doc output |

### Cross-Skill Verified Consistency

These values are identical across all skills that reference them:
- **Phone tiers:** 81-100 (Dial First), 61-80 (Dial Second), 41-60 (Dial Third), 21-40 (Dial Fourth), 0-20 (Drop)
- **Preset folders:** "00 Niche Sequential Marketing" (12 presets), "01. Bulk Sequential Marketing" (9 presets)
- **Sequence count:** 26 TCA templates across 5 folders (Lead Management 6, Acquisitions 6, Transactions 6, Deep Prospecting 4, Default 4)
- **Comp adjustments:** Bedroom $5,000, Bathroom $7,500, $/sqft $85, Age $500/yr (from `comp_analyzer.py`)
- **Financing defaults:** HML 12%, conventional 7%, 2 points, 2.5% closing (from `deal_analyzer.py`)
- **DOD sanity:** MAX_DOD_GAP_YEARS = 3 (from `obituary_enricher.py`)
- **Notice types:** 7 total (foreclosure, tax_sale, tax_delinquent, probate, eviction, code_violation, divorce)

### Key Corrections Made During Optimization (April 2026)
- **Hardcoded credentials removed** from sift-market-research (had email/password in SKILL.md)
- **Bedroom adjustment corrected** from $10K to $5K in real-estate-comping (matched to `comp_analyzer.py`)
- **HML points corrected** from 0% to 2% in deal-analyzer (matched to `deal_analyzer.py DEFAULT_HARD_MONEY_POINTS`)
- **Linux paths fixed** in sequential-presets (was `/home/ubuntu/skills/...`, now relative)
- **Preset names aligned** across 3 skills to match `niche_sequential.py` source code
- **Transfer tax labeled** as Tennessee-specific in deal-analyzer with state reference table for top 10 states
- **"Substantial renovation" defined** in real-estate-comping: kitchen + 1 bath minimum (~$15K spend)

### Skill File Structure
```
skill-name.skill (ZIP containing):
├── SKILL.md              # Main skill instructions
├── references/            # Domain knowledge files
│   ├── *.md              # Reference documents
│   └── *.pdf             # SOPs, guides
└── scripts/              # Optional automation scripts
    └── *.py / *.js

plugin-name.plugin (ZIP containing):
├── .claude-plugin/
│   └── plugin.json       # Plugin manifest
├── commands/             # Slash commands
│   └── *.md
├── skills/
│   └── skill-name/
│       ├── SKILL.md
│       └── references/
└── README.md
```
