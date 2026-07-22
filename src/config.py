"""Configuration for SiftStack — full-stack REI operations platform."""

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
LOG_DIR = PROJECT_ROOT / "logs"
STATE_FILE = PROJECT_ROOT / "last_run.json"
SEEN_IDS_FILE = PROJECT_ROOT / "seen_ids.json"
SEEN_IDS_PRUNE_DAYS = 90
# Notices that exhausted all CAPTCHA retries during scraping.
# Persisted so the next run's summary can surface them instead of
# silently dropping — and a future retry pass can prioritize them.
CAPTCHA_FAILED_IDS_FILE = PROJECT_ROOT / "captcha_failed_ids.json"
CAPTCHA_FAILED_PRUNE_DAYS = 14
COOKIES_FILE = PROJECT_ROOT / "cookies.json"
DROPBOX_STATE_FILE = PROJECT_ROOT / "dropbox_state.json"
PHOTO_STATE_FILE = PROJECT_ROOT / "photo_state.json"

# ── Dropbox Watcher ────────────────────────────────────────────────────
DROPBOX_POLL_INTERVAL = int(os.getenv("DROPBOX_POLL_INTERVAL", "900"))  # seconds (default 15 min)
DROPBOX_ROOT_FOLDER = os.getenv("DROPBOX_ROOT_FOLDER", "")  # root folder path in Dropbox, e.g. "/Bluetail Courthouse Photos"
DROPBOX_STORAGE_WARN_PERCENT = 80  # warn when storage usage exceeds this %

OUTPUT_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ── Notice Platform Registry ─────────────────────────────────────────
# All notice-site vendors this codebase knows about, keyed by the value
# NOTICE_PLATFORM (below) selects between. Defined before "Credentials"
# below so per-platform credential env var names can be derived from it;
# COUNTIES further down documents what each key means for scraper.py
# compatibility.
NOTICE_PLATFORMS: dict[str, str] = {
    "mopublicnotices": "https://www.mopublicnotices.com",
    "newmexicopublicnotices": "https://www.newmexicopublicnotices.com",
    "oklahomanotices": "https://www.oklahomanotices.com",
    "kansaspublicnotices": "https://www.kansaspublicnotices.com",
    "tnpublicnotice": "https://www.tnpublicnotice.com",
}

# One platform per run (see CLAUDE.md "Markets & Data Sources" — separate
# scheduled runs per platform, not multi-platform-in-one-run). Defaults to
# "mopublicnotices" for backward compatibility with existing .env/Actor
# configs that predate this variable. main.py cross-checks the counties
# selected for a run against this value (see _filter_searches_by_platform)
# rather than silently pointing a run at the wrong site if someone mixes
# counties from two platforms in one invocation.
NOTICE_PLATFORM = os.getenv("NOTICE_PLATFORM", "mopublicnotices").strip().lower()
if NOTICE_PLATFORM not in NOTICE_PLATFORMS:
    raise ValueError(
        f"NOTICE_PLATFORM={NOTICE_PLATFORM!r} is not a known platform — "
        f"must be one of {sorted(NOTICE_PLATFORMS)}"
    )
BASE_URL = NOTICE_PLATFORMS[NOTICE_PLATFORM]
LOGIN_URL = f"{BASE_URL}/authenticate.aspx"
SMART_SEARCH_URL = f"{BASE_URL}/SmartSearch/Default.aspx"

# ── Credentials ────────────────────────────────────────────────────────
# NOTICE_SITE_EMAIL/PASSWORD log in to whichever public-notice platform is
# active (NOTICE_PLATFORM above). Resolution order lets one .env hold
# credentials for multiple platforms without them overwriting each other —
# NOTICE_PLATFORM picks which pair a given run actually uses:
#   1. Platform-specific override, e.g. NEWMEXICOPUBLICNOTICES_EMAIL/
#      _PASSWORD — the uppercased NOTICE_PLATFORMS key exactly, no
#      abbreviation, so OK/KS get this for free when they go live.
#   2. Generic NOTICE_SITE_EMAIL/PASSWORD — single-platform setups that
#      don't need per-platform separation.
#   3. Legacy TNPN_EMAIL/PASSWORD — backwards compat with .env files from
#      before this platform system existed.
_platform_env_prefix = NOTICE_PLATFORM.upper()
NOTICE_SITE_EMAIL = os.getenv(
    f"{_platform_env_prefix}_EMAIL",
    os.getenv("NOTICE_SITE_EMAIL", os.getenv("TNPN_EMAIL", "")),
)
NOTICE_SITE_PASSWORD = os.getenv(
    f"{_platform_env_prefix}_PASSWORD",
    os.getenv("NOTICE_SITE_PASSWORD", os.getenv("TNPN_PASSWORD", "")),
)
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")  # 2Captcha API key
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # Claude Haiku for LLM parsing
SMARTY_AUTH_ID = os.getenv("SMARTY_AUTH_ID", "")        # Smarty address standardization
SMARTY_AUTH_TOKEN = os.getenv("SMARTY_AUTH_TOKEN", "")
OPENWEBNINJA_API_KEY = os.getenv("OPENWEBNINJA_API_KEY", "")  # Zillow property enrichment
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")              # Serper.dev Google Search API
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")        # Firecrawl JS-rendered scraping
TRACERFY_API_KEY = os.getenv("TRACERFY_API_KEY", "")          # Tracerfy skip tracing
TRESTLE_API_KEY = os.getenv("TRESTLE_API_KEY", "")            # Trestle phone validation
DATASIFT_EMAIL = os.getenv("DATASIFT_EMAIL", "")              # DataSift.ai login
DATASIFT_PASSWORD = os.getenv("DATASIFT_PASSWORD", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")        # Slack/Discord webhook
ANCESTRY_EMAIL = os.getenv("ANCESTRY_EMAIL", "")              # Ancestry.com login
ANCESTRY_PASSWORD = os.getenv("ANCESTRY_PASSWORD", "")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY", "")            # Dropbox OAuth2 app key
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET", "")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN", "")

# ── LLM Backend ──────────────────────────────────────────────────────
LLM_BACKEND = os.getenv("LLM_BACKEND", "anthropic")           # "anthropic", "ollama", or "openrouter"
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")  # fallback default; overridden per call site by LLM_MODELS below
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")        # Local Ollama model
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1/")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")       # OpenRouter API key
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# Per-call-site Anthropic model pinning, so cost/capability match the task
# instead of every call sharing one global model. Keys are call-site
# purposes rather than filenames since obituary_enricher.py splits across
# two tiers. Only used when LLM_BACKEND == "anthropic" — Ollama/OpenRouter
# dev backends keep using OLLAMA_MODEL/OPENROUTER_MODEL regardless.
LLM_MODELS = {
    # High-volume, low-reasoning structured field extraction — runs on
    # every notice where regex fails. llm_parser.py (scraped-notice +
    # courthouse-photo OCR fallback), pdf_importer.py (bulk PDF table parse).
    "ocr_extraction": "claude-haiku-4-5-20251001",
    # LLC/entity signer extraction from web search snippets — one bounded
    # extraction call, not multi-step reasoning. entity_researcher.py.
    "entity_research": "claude-haiku-4-5-20251001",
    # obituary_enricher.py survivor-name and mailing-address extraction —
    # mechanical extraction, not the deceased-owner match judgment call.
    "obituary_support": "claude-haiku-4-5-20251001",
    # obituary_enricher.py's DOD/name match against the property owner —
    # wrong answer means contacting the wrong family member, needs real
    # judgment. _parse_obituary_with_llm() only.
    "obituary_match": "claude-sonnet-5",
    # Deep-prospecting report prose summary — low volume (once per report),
    # low stakes, has a deterministic template fallback if this fails.
    "situation_summary": "claude-haiku-4-5-20251001",
}

# ── ASP.NET Selectors ─────────────────────────────────────────────────
# Login form (verified on authenticate.aspx)
SEL_LOGIN_EMAIL = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_txtEmailAddress"
SEL_LOGIN_PASSWORD = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_txtPassword"
SEL_LOGIN_SUBMIT = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_btnAuth"

# Smart Search dashboard / saved searches
# Suffix match (not exact ID) — MO's real ID is "ctl00_as1_ddlSavedSearches"
# but NM's is "ctl00_ContentPlaceHolder1_as1_ddlSavedSearches" (confirmed
# live 2026-07-22, tests/diag_nm_search_form.py). Same vendor platform,
# different master-page ContentPlaceHolder nesting per site — an exact-ID
# selector silently breaks cross-site even though the URL paths and login
# form field IDs match exactly. Suffix match is safe for both.
SEL_SAVED_SEARCHES_DROPDOWN = "[id$='_ddlSavedSearches']"
SEL_PER_PAGE_DROPDOWN = 'select[name$="ddlPerPage"]'

# Search results (verified on Search.aspx)
SEL_RESULTS_GRID = "#ctl00_ContentPlaceHolder1_WSExtendedGrid1_GridView1"
# :visible is required — each grid row renders BOTH a visible "btnView" and
# a hidden "btnView2" duplicate (leftover from the site's "Hide Read
# Notices" toggle). Without :visible, query_selector_all returns 2 matches
# per row, which silently misaligns every subsequent index against
# scraper.py's positional view_buttons[idx] lookup — idx=1 hits row 1's
# hidden duplicate instead of row 2, and Playwright's actionability check
# waits forever for a hidden element to become clickable. Confirmed live
# via tests/diag_gobacktimeout.py (2026-07-17) — this was misattributed to
# a go_back()/bfcache issue before the actual button dump revealed it.
SEL_VIEW_BUTTON_PATTERN = "input[name$='btnView2']:visible, input[name$='btnView']:visible"
SEL_NEXT_PAGE_BUTTON = "#ctl00_ContentPlaceHolder1_WSExtendedGrid1_GridView1_ctl01_btnNext"
SEL_PAGE_INFO = "table.wsResultsGrid"

# Notice detail page — two independent CAPTCHA providers are in play
# depending on which notice site/county is being scraped. See
# captcha_solver.py for the explicit-detection dispatcher.
SEL_CAPTCHA_IFRAME = "iframe[src*='recaptcha']"
# mopublicnotices.com's Turnstile widget iframe has NO `src` attribute at
# all (confirmed via live DOM dump 2026-07-16 — tests/diag_turnstile.py) —
# an iframe[src*=...] selector can never match it. The reliable marker is
# the widget's own container div, which carries Cloudflare's standard
# `cf-turnstile` class regardless of its (ASP.NET-legacy-named) id.
SEL_TURNSTILE_WIDGET = ".cf-turnstile"
SEL_VIEW_NOTICE_BUTTON = "#ctl00_ContentPlaceHolder1_PublicNoticeDetailsBody1_btnViewNotice"
RECAPTCHA_SITEKEY = "6LdtSg8sAAAAADTdRyZxJ2R2sS82pKALNMvMqSyL"  # legacy tnpublicnotice.com build
# Turnstile has no fixed sitekey here — it's extracted live from the page DOM
# per-notice (see captcha_solver._extract_turnstile_sitekey), confirmed
# present on mopublicnotices.com's Details.aspx pages (2026-07-16).

# ── Rate Limiting ──────────────────────────────────────────────────────
REQUEST_DELAY_MIN = 2.0  # seconds between requests
REQUEST_DELAY_MAX = 3.0
MAX_RETRIES = 3
RESULTS_PER_PAGE = 50  # max the site allows

# ── Image Processing ───────────────────────────────────────────────────
BLUR_THRESHOLD = int(os.getenv("BLUR_THRESHOLD", "100"))   # Laplacian variance; below = rejected as blurry
TESSERACT_PSM_PDF = 3    # fully automatic — best for PDF tax sale tables
TESSERACT_PSM_PHOTO = 4  # assume single column of variable-size text — best for terminal screen photos

# ── Notice Types ───────────────────────────────────────────────────────
NOTICE_TYPES = ["foreclosure", "probate"]


# ── County / Market Registry ────────────────────────────────────────────
# Single source of truth for which counties SiftStack operates in and what
# state/data-source each one maps to. Other modules should derive state
# abbreviations, state full names, city fallbacks, and reference URLs from
# this registry instead of hardcoding a single state.
#
# notice_platform values and what they mean for scraper.py compatibility:
#   "mopublicnotices"         - ASP.NET WebForms (Missouri Press Association).
#                                Live and verified working with the current
#                                scraper automation.
#   "newmexicopublicnotices"  - ASP.NET WebForms, same "lrsws.co" vendor
#                                domain as the original tnpublicnotice.com
#                                (verified via TLS cert SAN inspection).
#                                High-confidence compatible, but NOT yet
#                                live: saved searches must be created in the
#                                site UI and credentials obtained first.
#   "oklahomanotices"         - Backed by opa.eclipping.org, a DIFFERENT
#                                vendor (not ASP.NET WebForms). NOT
#                                compatible with the current scraper
#                                automation — needs dedicated scraper work.
#   "kansaspublicnotices"     - Backed by "NewzGroup" (shared TLS cert with
#                                kypublicnotice.com/ndpublicnotices.com), a
#                                THIRD vendor. Also NOT compatible with the
#                                current scraper automation.
#   "tnpublicnotice" (legacy) - Original ASP.NET WebForms platform. Kept for
#                                Knox/Blount, which are dormant/legacy, not
#                                actively scraped.
@dataclass
class CountyProfile:
    """Metadata for one operating county — the market/state model other
    modules should key off of instead of hardcoding a single state."""
    county: str            # "Jackson"
    state: str              # "MO" — 2-letter USPS abbreviation
    state_full: str         # "Missouri" — for search queries/regexes that need the full name
    notice_platform: str    # key into NOTICE_PLATFORMS (see "Notice Platform Registry" above)
    scraper_ready: bool     # True only if the current Playwright automation can drive this platform
    major_city: str         # county seat / primary city, used as a city-fallback match
    zip_prefixes: list[str] # approximate 3-digit ZIP prefixes for this county (soft validation heuristic only)
    assessor_url: str       # county tax assessor site — reference only, no public API assumed
    court_records_url: str  # court/case-record lookup system — reference only
    active: bool = True     # False = dormant/legacy market, excluded from default SAVED_SEARCHES
    notes: str = ""         # caveats worth remembering (e.g. "probate coverage unconfirmed")

COUNTIES: dict[str, CountyProfile] = {
    # ── Missouri — live, ASP.NET WebForms, already verified working ────
    "jackson": CountyProfile(
        county="Jackson", state="MO", state_full="Missouri",
        notice_platform="mopublicnotices", scraper_ready=True,
        major_city="Kansas City", zip_prefixes=["640", "641"],
        assessor_url="https://www.jacksongov.org/departments/collection-taxes-assessment/assessment",
        court_records_url="https://www.courts.mo.gov/casenet/base/welcome.do",
        notes="Foreclosure/tax-sale notices confirmed on mopublicnotices.com; "
              "probate coverage not confirmed in site copy — verify on first live pull. "
              "ArcGIS Open Data Hub parcel layers available as a closer-to-API assessor option.",
    ),
    "clay": CountyProfile(
        county="Clay", state="MO", state_full="Missouri",
        notice_platform="mopublicnotices", scraper_ready=True,
        major_city="Liberty", zip_prefixes=["640", "641"],
        assessor_url="https://gisweb.claycountymo.gov/ps/",
        court_records_url="https://www.courts.mo.gov/casenet/base/welcome.do",
        notes="Same caveats as Jackson — probate coverage unconfirmed on mopublicnotices.com.",
    ),
    "platte": CountyProfile(
        county="Platte", state="MO", state_full="Missouri",
        notice_platform="mopublicnotices", scraper_ready=True,
        major_city="Platte City", zip_prefixes=["640", "641"],
        assessor_url="https://www.co.platte.mo.us/real-property",
        court_records_url="https://www.courts.mo.gov/casenet/base/welcome.do",
        notes="Same caveats as Jackson — probate coverage unconfirmed on mopublicnotices.com.",
    ),
    "cass": CountyProfile(
        county="Cass", state="MO", state_full="Missouri",
        notice_platform="mopublicnotices", scraper_ready=True,
        major_city="Harrisonville", zip_prefixes=["647", "640"],
        assessor_url="https://cass.missouriassessors.com/search.php",
        court_records_url="https://www.courts.mo.gov/casenet/base/welcome.do",
        notes="Same caveats as Jackson — probate coverage unconfirmed on mopublicnotices.com.",
    ),
    # ── New Mexico — live as of 2026-07-22 ──────────────────────────────
    "bernalillo": CountyProfile(
        county="Bernalillo", state="NM", state_full="New Mexico",
        notice_platform="newmexicopublicnotices", scraper_ready=True, active=True,
        major_city="Albuquerque", zip_prefixes=["871", "870"],
        assessor_url="https://www.bernco.gov/assessor/",
        court_records_url="https://caselookup.nmcourts.gov/",
        notes="Same vendor (lrsws.co) as tnpublicnotice.com — confirmed live 2026-07-22: login form "
              "field IDs match MO exactly, but SEL_SAVED_SEARCHES_DROPDOWN's exact ID did NOT (different "
              "master-page ContentPlaceHolder nesting — fixed to a suffix selector in config.py). The "
              "account's pre-existing \"probate\" saved search already covers Bernalillo + Sandoval "
              "together in one query (keywords: probate/estate/personal representative/notice to "
              "creditors) — see SAVED_SEARCHES below; a \"foreclosure\" saved search also already exists "
              "on the account (same two-county scope) but isn't wired into SAVED_SEARCHES yet. "
              "Probate case coverage on caselookup.nmcourts.gov is district-court only; NM often handles "
              "routine probate through an independent county Probate Court — verify separately. "
              "ArcGIS Open Data Hub parcel layers available as a closer-to-API assessor option.",
    ),
    "sandoval": CountyProfile(
        county="Sandoval", state="NM", state_full="New Mexico",
        notice_platform="newmexicopublicnotices", scraper_ready=True, active=True,
        major_city="Rio Rancho", zip_prefixes=["870", "871"],
        assessor_url="https://eaweb.sandovalcountynm.gov/Assessor",
        court_records_url="https://caselookup.nmcourts.gov/",
        notes="Same caveats as Bernalillo — live 2026-07-22, shares the same \"probate\" saved search "
              "(covers both counties in one query).",
    ),
    # ── Oklahoma — NOT scraper-compatible with the current automation ──
    "oklahoma": CountyProfile(
        county="Oklahoma", state="OK", state_full="Oklahoma",
        notice_platform="oklahomanotices", scraper_ready=False, active=False,
        major_city="Oklahoma City", zip_prefixes=["731", "730"],
        assessor_url="https://docs.oklahomacounty.org/AssessorWP5/DefaultSearch.asp",
        court_records_url="https://www.oscn.net/dockets/Search.aspx",
        notes="oklahomanotices.com's search backend is opa.eclipping.org — a different vendor than the "
              "ASP.NET WebForms platform this scraper automates. Needs dedicated scraper development; "
              "not something to fake with unverified selectors.",
    ),
    "tulsa": CountyProfile(
        county="Tulsa", state="OK", state_full="Oklahoma",
        notice_platform="oklahomanotices", scraper_ready=False, active=False,
        major_city="Tulsa", zip_prefixes=["741", "740"],
        assessor_url="https://assessor.tulsacounty.org/Property/Search",
        court_records_url="https://www.oscn.net/dockets/Search.aspx",
        notes="Same caveats as Oklahoma County (not scraper-compatible yet).",
    ),
    # ── Kansas — NOT scraper-compatible with the current automation ────
    "johnson": CountyProfile(
        county="Johnson", state="KS", state_full="Kansas",
        notice_platform="kansaspublicnotices", scraper_ready=False, active=False,
        major_city="Olathe", zip_prefixes=["660", "661", "662"],
        assessor_url="https://www.jocogov.org/department/appraiser/property-data",
        court_records_url="https://www.kscourts.gov/eCourt/District-Court-Records",
        notes="kansaspublicnotices.com runs on a 'NewzGroup'-family vendor (shared TLS cert with "
              "kypublicnotice.com/ndpublicnotices.com) — different platform than this scraper automates. "
              "Needs dedicated scraper development. Court records moved from a county-only terminal system "
              "to the statewide Kansas eCourt portal in Nov 2024.",
    ),
    # ── Tennessee — dormant/legacy, kept functional but inactive ───────
    "knox": CountyProfile(
        county="Knox", state="TN", state_full="Tennessee",
        notice_platform="tnpublicnotice", scraper_ready=True, active=False,
        major_city="Knoxville", zip_prefixes=["377", "378", "379"],
        assessor_url="https://www.kgis.org",
        court_records_url="",
        notes="Original build market. Kept dormant (not deleted) — has the only real tax-API integration "
              "(Knox County Tax API) and Knoxville-calibrated rehab/comp/deal-analysis defaults.",
    ),
    "blount": CountyProfile(
        county="Blount", state="TN", state_full="Tennessee",
        notice_platform="tnpublicnotice", scraper_ready=True, active=False,
        major_city="Maryville", zip_prefixes=["377", "378"],
        assessor_url="https://assessment.cot.tn.gov",
        court_records_url="",
        notes="Dormant/legacy market, kept alongside Knox — TPAD scraper only, no free tax API.",
    ),
}


@dataclass
class SavedSearch:
    """Represents a saved search on the county's notice platform (see COUNTIES)."""
    county: str
    notice_type: str  # One of NOTICE_TYPES
    saved_search_name: str  # Exact name in the Saved Searches dropdown


def state_for_county(county: str) -> str:
    """Look up the 2-letter state abbreviation for a known county. Empty string if unknown."""
    profile = COUNTIES.get((county or "").strip().lower())
    return profile.state if profile else ""


def state_full_for_county(county: str) -> str:
    """Look up the full state name for a known county. Empty string if unknown."""
    profile = COUNTIES.get((county or "").strip().lower())
    return profile.state_full if profile else ""


def platform_for_county(county: str) -> str:
    """Look up the notice_platform key for a known county. Empty string if unknown."""
    profile = COUNTIES.get((county or "").strip().lower())
    return profile.notice_platform if profile else ""


# All state abbreviations/full names present in the registry — used to build
# state-token regexes and validation sets so they aren't hardcoded to TN.
KNOWN_STATE_ABBRS: set[str] = {p.state for p in COUNTIES.values()}
STATE_NAMES: dict[str, str] = {p.state: p.state_full for p in COUNTIES.values()}


# ── Saved Searches ─────────────────────────────────────────────────────
# These names must match exactly what appears in the dropdown on the site.
# Create the dropdown entries with these exact labels before the first real run.
# Only ACTIVE + scraper_ready counties are included — Oklahoma/Kansas aren't
# scrapable with the current automation yet.
#
# New Mexico's "probate" entry is unusual: unlike MO's one-search-per-county
# pattern, this single saved search on newmexicopublicnotices.com already
# covers both Bernalillo and Sandoval together (confirmed live 2026-07-22)
# — so both counties list it here (needed for correct --counties filtering
# per county), and main.py's _dedupe_by_saved_search_name() collapses them
# back to one actual site search when both counties are requested together,
# avoiding a redundant duplicate scrape of identical results.
SAVED_SEARCHES: list[SavedSearch] = [
    SavedSearch("Jackson", "probate", "Jackson County Probate"),
    SavedSearch("Clay", "probate", "Clay County Probate"),
    SavedSearch("Platte", "probate", "Platte County Probate"),
    SavedSearch("Cass", "probate", "Cass County Probate"),
    SavedSearch("Bernalillo", "probate", "probate"),
    SavedSearch("Sandoval", "probate", "probate"),
]

# ── Entity Detection ──────────────────────────────────────────────────
# Business entity patterns — shared across obituary_enricher, tax_enricher,
# and enrichment_pipeline for entity filtering.
BUSINESS_RE = re.compile(
    r"\b(?:LLC|L\.L\.C|INC|CORP|CORPORATION|COMPANY|CO\b|LTD|LP|L\.P|"
    r"PARTNERSHIP|ASSOCIATION|ASSOC|BANK|CREDIT UNION|CHURCH|MINISTRIES|"
    r"HOUSING|AUTHORITY|DEVELOPMENT|ENTERPRISES|PROPERTIES|INVESTMENTS|"
    r"GROUP|HOLDINGS|MANAGEMENT|SERVICES|FOUNDATION|ORGANIZATION)\b",
    re.IGNORECASE,
)

# Trust/estate patterns — personal trusts are NOT business entities
TRUST_NAME_RE = re.compile(
    r"^(?:THE\s+)?([\w]+(?:\s+[\w.]+)+?)\s+(?:REVOCABLE\s+)?(?:LIVING\s+)?TRUST\b",
    re.IGNORECASE,
)
ESTATE_OF_RE = re.compile(
    r"^(?:THE\s+)?ESTATE\s+OF\s+([\w]+(?:\s+[\w.]+)+?)(?:\s*,|\s*$)",
    re.IGNORECASE,
)

_config_logger = logging.getLogger(__name__)


# ── State File Utilities ─────────────────────────────────────────────


def save_state(path: Path, data: dict) -> None:
    """Write JSON state to disk atomically (write tmp → rename).

    Creates a .bak copy of the previous file before overwriting.
    """
    # Back up current file
    if path.exists():
        try:
            bak = path.with_suffix(path.suffix + ".bak")
            bak.write_bytes(path.read_bytes())
        except OSError:
            pass  # Best-effort backup

    # Atomic write: tmp → rename
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path) -> dict:
    """Load JSON state from disk, falling back to .bak if corrupt."""
    for candidate in [path, path.with_suffix(path.suffix + ".bak")]:
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                _config_logger.warning("Failed to read %s: %s", candidate, e)
    return {}
