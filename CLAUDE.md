# CLAUDE.md — SiftStack

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository. It is kept short on purpose (it loads every turn). Full history and build notes live in `docs/claude/` — see "Detailed docs" at the bottom, and read the relevant file before working in that area.

## Project Overview

**SiftStack** — real estate investing operations platform for Bluetail Homes, built around the DataSift.ai (REISift) CRM: public-notice scraping and county data import → enrichment (Smarty, Zillow, parcel lookups, obituary/heir research, Tracerfy, Trestle) → deal analysis → DataSift upload and CRM automation (Jackson P1 texts, drips, SMS responder, KPI reporting).

## Current status (as of 2026-10-27)

- **Pre-Tulum sprint (deadline Fri 2026-10-30):** `docs/pre-tulum-tracker.md` is the source of truth for what's in progress, done, and next. Read it at the start of a sprint session and update it before ending.

- **Scraper production is PAUSED** (since 2026-09-29): all 4 Apify schedules (MO `irDDOAu0oVG1kdQvr`, NM `f5eGSQ0If5J50xrZx`, KS `c7OJT1cG8PzJayuwq`, OK `ABj82kJNqoI1kmN9C`) are `isEnabled: false`, and the `stage2-datasift-upload.yml` workflow is disabled. Nothing was deleted. To resume: `PUT /v2/schedules/{id}` `{"isEnabled": true}` and re-enable the workflow (`POST /repos/BluetailHomes/SiftStack/actions/workflows/321011465/enable`). Details: `docs/claude/markets-and-status.md`.
- **Active focus: Jackson County, MO only** — 16 native DataSift SiftMap "Priority 1" presets built off the Doors-Per-Deal export, an Obituary track, and 4 targeted county-record pulls. Plan: `docs/claude/jackson-build-sheet.md`. Daily automation for Rahaf's P1 calling (am-start, afternoon voicemail texts, SMS responder, line/call watch, drip exit guard, KPI scorecard, weekly top-up): `docs/claude/pm-text-automation.md`.
- **Markets:** MO (Jackson, Clay, Platte, Cass), KS (Johnson), NM (Bernalillo, Sandoval), OK (Oklahoma, Tulsa); TN (Knox, Blount) is dormant/legacy. Every module keys off `COUNTIES` in `src/config.py`.

## Operating rules (these change what you do)

- **DataSift: prefer the Open API** (`REISIFT_API_KEY` in `.env`, API at `apiv2.reisift.io`, app at `app.reisift.io`) for new automation; check each endpoint exists before relying on it. Playwright UI automation is the older path — its hard-won patterns are in `docs/claude/datasift-integration.md`.
- **Dry-run gates:** the P1 jobs send nothing unless their repo Actions variable is `"true"`: `PM_TEXTS_LIVE`, `EXTRA_TEXTS_LIVE`, `AM_START_LIVE`, `TOPUP_LIVE`. Never flip these without Kaycie's say-so.
- **Scheduling:** P1 jobs are triggered by the Cloudflare Worker in `dispatcher/` (GitHub's own cron ran hours late). **pm-texts must never get a GitHub cron back** (double-text risk). Cloudflare crons use day NAMES (`MON-FRI`), not numbers (1 = Sunday there). Free plan allows 5 cron triggers and all 5 are used. Worker secret `GITHUB_TOKEN` (fine-grained PAT) **expires Oct 2027** — renew with `npx.cmd wrangler secret put GITHUB_TOKEN` from `dispatcher/`.
- **Texting:** weekdays only, 9 AM–7 PM Central; per-line cap 20 texts/day.
- **No duplicate DataSift uploads:** don't run `--upload-datasift` manually against counties/dates a scheduled Apify Task already covers without checking its run history first (CLI `seen_ids.json` and the Actor's KVS dedup don't share state). Runs without `--upload-datasift` are always safe.
- **Never** drop a lead on missing data: no property address → pass through the ZIP gate; unknown equity → keep and tag `equity_unknown`. Never substitute the PR's mailing ZIP for a property ZIP.
- **Skills:** use DataSift's official skills (`sift-operations`, `sequential-presets`, etc.). Do not merge, restore, replace, or modify any `bluetail-*` skill without confirming its canonical source with Kaycie. See `docs/claude/skill-library.md`.
- **Local `main.py` quirk:** with a real `APIFY_TOKEN` in `.env`, `main.py` runs in Actor mode — CLI subcommands like `manage-presets` won't run locally.
- **Direct Anthropic calls** must use `llm_client._extract_response_text()`, not `response.content[0].text` (thinking blocks come first).
- **Config gotcha:** access `config.BASE_URL`/`LOGIN_URL`/`SMART_SEARCH_URL` as attributes, never `from config import ...` — `apply_notice_platform()` reassigns them at runtime.

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

## Key Domain Rules

- **Foreclosure filtering is critical.** Not all notices from "Foreclosure" saved searches are actual foreclosures. The scraper parses each notice's full text and only includes ones with trustee sale language. See `INCLUDE_PHRASES` / `EXCLUDE_PHRASES` in `foreclosure_filter.py`.
- **Probate owner_name** should be the Personal Representative/Executor/Administrator — not the deceased.
- **Owner names** in foreclosure notices typically appear after "executed by" in the deed of trust language.
- **Rate limiting:** 2-3 second random delays between requests, 3 retries per page.
- **Address dedup:** Same property can appear in multiple notices; `data_formatter.deduplicate()` keeps the most recent.

## Output

CSV files land in `output/` (gitignored). Logs go to `logs/` with timestamped filenames. Sift columns: `date_added, address, city, state, zip, owner_name, notice_type, county, source_url`.

## Detailed docs (`docs/claude/`)

Moved verbatim from this file on 2026-10-27. Read the relevant one before changing that area.

| File | Covers |
|---|---|
| `markets-and-status.md` | Pause banner in full, markets table, per-county lookup coverage |
| `jackson-build-sheet.md` | Jackson County P1 plan: price bands, ZIPs, equity rules, rungs, Obituary track, county-record pulls, known blockers |
| `pm-text-automation.md` | `pm_text_sender.py` (voicemail texts, extra-texts, line-watch, call-watch), dispatcher Worker, SMS responder, drip stop, am-start, KPI scorecard, smrtPhone API |
| `datasift-integration.md` | CSV column structure, upload wizard, enrich/skip trace, niche presets (live folder facts), login + UI automation patterns, SiftMap, Market Finder |
| `scrapers-and-sites.md` | Notice-site platforms (MO/NM ASP.NET, KS, OK), CAPTCHA, pagination and county-relabel fixes, saved searches |
| `apify-and-stage2.md` | Actor input, multi-platform Tasks, parcel lookup tiers (NM/Clay/Cass/Johnson/Platte), volume anomaly alerts, known gaps, stage-2 DataSift upload |
| `high-potential-filter.md` | Step 8b ZIP + equity gates, thresholds, suppressed-leads log, tags, opt-out flags |
| `courthouse-photo-pipeline.md` | Photo/Dropbox import, OCR patterns, Knox probate deep prospecting |
| `skill-library.md` | DataSift's 27-skill plugin library, `bluetail-*` skills, historical notes |
| `../OK_KS_SCRAPER_FEASIBILITY.md`, `../sms_reply_rules.md` | Pre-existing docs |
