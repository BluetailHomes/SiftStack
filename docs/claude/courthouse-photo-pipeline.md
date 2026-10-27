<!-- Moved verbatim from CLAUDE.md on 2026-10-27 to keep CLAUDE.md small. -->
# Courthouse photo pipeline

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
