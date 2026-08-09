"""Unified enrichment pipeline for all data sources.

Provides a single canonical pipeline that all entry points call:
  - Apify Actor (daily/historical web scrape)
  - CLI daily/historical (web scrape)
  - PDF import (OCR tax sale PDFs)
  - CSV re-import (re-enrich existing data)

Each caller acquires data, builds PipelineOptions, and calls
run_enrichment_pipeline(). The pipeline handles dedup, filtering,
and all enrichment steps in a fixed canonical order.
"""

import csv
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import config
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────


@dataclass
class PipelineOptions:
    """Controls which enrichment steps run and passes sub-options."""

    # Step skip flags (default: run everything)
    skip_filter_sold: bool = True  # only CSV re-import sets False
    skip_vacant_filter: bool = False
    skip_entity_filter: bool = False
    skip_entity_research: bool = True   # opt-in via --research-entities
    skip_commercial_filter: bool = False
    skip_parcel_lookup: bool = False
    skip_tax: bool = False
    skip_smarty: bool = False
    skip_geocode: bool = False
    skip_zillow: bool = False
    skip_obituary: bool = False
    skip_ancestry: bool = False
    skip_high_potential_filter: bool = False  # true if EITHER sub-filter below is skipped — kept for back-compat / simple "skip entirely" callers
    skip_zip_filter: bool = False    # opt-out via --include-off-target
    skip_equity_filter: bool = False  # opt-out via --include-low-equity

    # High-Potential Lead Filter sub-options — None means "use config
    # defaults" (config.TARGET_ZIPS / config.HARD_DROP_EQUITY_PERCENT).
    # Overridable per-run (e.g. a future --target-zips/--min-equity-percent
    # CLI flag) without touching config.py.
    target_zips: dict[str, str] | None = None
    hard_drop_equity_percent: float | None = None

    # Obituary sub-options
    skip_heir_verification: bool = False
    max_heir_depth: int = 2
    skip_dm_address: bool = False
    tracerfy_tier1: bool = False

    # Smart detection flags (set by detect_existing_enrichment)
    has_smarty: bool = False
    has_zillow: bool = False
    has_tax: bool = False
    has_obituary: bool = False

    # Context label for summary logging
    source_label: str = ""


# ── Smart detection ──────────────────────────────────────────────────


def detect_existing_enrichment(
    notices: list[NoticeData], opts: PipelineOptions
) -> None:
    """Scan notices for pre-populated enrichment data and set has_* flags.

    Call this only for CSV re-import — fresh scrapes and PDF imports should
    always run all steps.
    """
    opts.has_smarty = any(n.dpv_match_code for n in notices)
    opts.has_zillow = any(n.estimated_value for n in notices)
    opts.has_tax = any(n.parcel_id or n.tax_delinquent_amount for n in notices)
    # Obituary: only skip if >50% of records already have data (not just any())
    deceased_count = sum(1 for n in notices if n.owner_deceased)
    total = len(notices) if notices else 1
    opts.has_obituary = deceased_count > total * 0.5

    if opts.has_smarty:
        logger.info("Smarty data detected — will preserve existing data")
    if opts.has_zillow:
        logger.info("Zillow data detected — will preserve existing data")
    if opts.has_tax:
        logger.info("Tax data detected — will preserve existing data")
    if opts.has_obituary:
        logger.info("Obituary data detected (%d/%d = %.0f%%) — will preserve existing data",
                     deceased_count, total, deceased_count / total * 100)


# ── Filters ──────────────────────────────────────────────────────────


def _filter_vacant_land(notices: list[NoticeData]) -> list[NoticeData]:
    """Remove records where the property address has no real house number.

    Vacant land parcels (e.g., "0 Andersonville Pike", "0000 Old Rd",
    or just "Andersonville Pike") are not actionable for marketing.

    Probate notices are exempt: the source notice text never contains a
    property address (that's what Step 3c's Probate Property Lookup exists
    to find, and it runs AFTER this filter) — an empty n.address on a fresh
    probate record means "not looked up yet", not "vacant land". Filtering
    them here would silently kill every probate lead before Step 3c ever
    got a chance to run (confirmed live 2026-07-16 against a real Jackson
    County MO probate notice — this was dropping 100% of probate records
    regardless of market, not just the new counties).
    """

    def _has_house_number(addr: str) -> bool:
        addr = addr.strip()
        if not addr:
            return False
        m = re.match(r"^(\d+)", addr)
        if not m:
            return False
        return int(m.group(1)) > 0

    before = len(notices)
    result = [
        n for n in notices
        if n.notice_type == "probate" or _has_house_number(n.address)
    ]
    removed = before - len(result)
    if removed:
        logger.info("  Removed %d vacant land records (no house number)", removed)
    return result


def _filter_entity_owners(notices: list[NoticeData]) -> list[NoticeData]:
    """Remove records owned by business entities (LLC, INC, CORP, etc.).

    Personal trusts and estates are NOT filtered — "JOHN DOE TRUST" is a
    person, while "FIRST TENNESSEE BANK TRUST" is a business entity.
    """

    def _is_entity(n: NoticeData) -> bool:
        # Check both tax_owner_name (preferred) and owner_name
        name = (n.tax_owner_name or n.owner_name or "").strip()
        if not name:
            return False
        if not config.BUSINESS_RE.search(name):
            return False
        # Exempt personal trusts/estates (have extractable personal name)
        if config.TRUST_NAME_RE.match(name):
            return False
        if config.ESTATE_OF_RE.match(name):
            return False
        # Entity research found a real person — keep the record
        if n.entity_person_name:
            return False
        return True

    before = len(notices)
    removed_names = []
    result = []
    for n in notices:
        if _is_entity(n):
            removed_names.append(n.tax_owner_name or n.owner_name)
        else:
            result.append(n)
    removed = before - len(result)
    if removed:
        logger.info("  Removed %d entity-owned records", removed)
        for name in removed_names[:10]:
            logger.info("    - %s", name)
        if len(removed_names) > 10:
            logger.info("    ... and %d more", len(removed_names) - 10)
    return result


def _filter_commercial(notices: list[NoticeData]) -> list[NoticeData]:
    """Remove records with Smarty RDI = 'Commercial'.

    Only filters when rdi is explicitly 'Commercial' — empty rdi
    (no Smarty data) passes through.
    """
    before = len(notices)
    result = [n for n in notices if n.rdi.lower() != "commercial"]
    removed = before - len(result)
    if removed:
        logger.info("  Removed %d commercial properties", removed)
    return result


# ── High-Potential Lead Filter (target ZIPs + equity) ──────────────────
# Added 2026-08-09 per Kaycie's request: only pull leads in high-
# investor-activity ZIPs with sufficient equity. See config.TARGET_ZIPS,
# config.HARD_DROP_EQUITY_PERCENT / config.LOW_EQUITY_FLAG_PERCENT, and
# config.HIGH_EQUITY_OWNERSHIP_YEARS for the underlying data/thresholds
# and their sourcing/reasoning.
#
# Revised 2026-08-09 (same day, second pass) after Kaycie ran the v1
# design (single 30% hard cutoff) past a separate advisory chat, which
# raised a real risk: property_enricher's equity_percent is an ESTIMATE
# (80% LTV + amortization-table assumption, not a real mortgage payoff
# balance), so a single hard cutoff can kill a genuinely great probate
# deal on a stale/wrong debt figure. Revised design, three load-bearing
# decisions from that review, all reflected below:
#
#  1. Two equity thresholds, not one — HARD_DROP_EQUITY_PERCENT (20%)
#     below which a lead is suppressed, and LOW_EQUITY_FLAG_PERCENT (30%)
#     below which (but >= 20%) a lead is KEPT but tagged "low_equity" for
#     a human to sanity-check rather than auto-processed or auto-killed.
#  2. Never drop a lead for MISSING equity data — only a CONFIRMED
#     equity_percent below 20% causes a drop. "unknown"/no-data records
#     are kept and tagged "equity_unknown", never suppressed.
#  3. Suppressed leads (zip-out-of-target OR equity-below-20%) are never
#     hard-deleted — every one is appended to a QA CSV via
#     _write_suppressed_log() so the 20%/30% cutoffs can be revisited
#     later against real suppressed-lead samples instead of guessed once
#     and left alone.
#
# One decision carried over unchanged from v1: don't drop a probate lead
# on ZIP before we've actually tried to find its property address. Not
# every market has a property-address lookup tier yet (NM and Knox do;
# MO does not, as of this build) — see _filter_high_potential() below.


def _years_since_sale(mls_last_sold_date: str) -> int | None:
    """Years elapsed since a property's last recorded sale, or None if unknown."""
    if not mls_last_sold_date:
        return None
    try:
        sold = datetime.strptime(mls_last_sold_date, "%Y-%m-%d")
    except ValueError:
        return None
    return (datetime.now() - sold).days // 365


def _classify_equity_signal(n: NoticeData) -> str:
    """Classify a record's equity signal for filtering and tagging.

    Returns one of:
    - "confirmed_high": real equity_percent >= config.LOW_EQUITY_FLAG_PERCENT (30%)
      — process normally.
    - "low_equity_flag": real equity_percent in
      [config.HARD_DROP_EQUITY_PERCENT, config.LOW_EQUITY_FLAG_PERCENT) —
      i.e. 20-30% — kept, but tagged "low_equity" so a human glances at it
      rather than the deal auto-proceeding or auto-dying on a rough debt
      estimate.
    - "low_equity_drop": real equity_percent < config.HARD_DROP_EQUITY_PERCENT
      (20%) — suppressed by _filter_high_potential(), logged to the QA CSV,
      never silently deleted.
    - "likely_high_ownership": no computed equity_percent, but either (a) the
      property's last recorded sale is old enough (see
      config.HIGH_EQUITY_OWNERSHIP_YEARS) or (b) Zillow has NO sale on record
      at all — for probate specifically, "never sold on Zillow's record" is
      itself a strong long-ownership signal (a decedent who owned the home
      for decades, likely purchased before Zillow's data coverage begins),
      not an absence of signal. Treated as a positive equity proxy, never
      suppressed.
    - "unknown": no equity_percent AND no basis to infer long ownership —
      kept and tagged "equity_unknown", never suppressed. (e.g. a recent,
      known sale with equity_percent still uncomputed for some other
      reason — missing zestimate, pre-1990 purchase-year cutoff in
      property_enricher._estimate_remaining_balance, etc.)
    """
    if n.equity_percent:
        try:
            pct = float(n.equity_percent)
        except ValueError:
            pct = None
        if pct is not None:
            if pct < config.HARD_DROP_EQUITY_PERCENT:
                return "low_equity_drop"
            if pct < config.LOW_EQUITY_FLAG_PERCENT:
                return "low_equity_flag"
            return "confirmed_high"

    years = _years_since_sale(n.mls_last_sold_date)
    if years is None:
        # No sale on Zillow's record at all. For probate leads this is a
        # meaningful signal (long, unbroken ownership), not just missing
        # data — but for non-probate notice types it's more likely a plain
        # Zillow data gap, so only treat it as a positive signal for
        # probate specifically.
        if n.notice_type == "probate":
            return "likely_high_ownership"
        return "unknown"
    if years >= config.HIGH_EQUITY_OWNERSHIP_YEARS:
        return "likely_high_ownership"
    return "unknown"


def _write_suppressed_log(suppressed: list[tuple[NoticeData, str]]) -> None:
    """Append suppressed leads to a QA CSV — never hard-delete a drop.

    Per the 2026-08-09 review: suppressed leads should be sampled
    periodically ("would we actually have wanted any of these?") to
    validate/tune the 20%/30% equity cutoffs and the target-ZIP list
    against real deal outcomes, rather than guessed once and left alone.
    One row per suppressed record, appended (not overwritten) so the log
    accumulates across runs. Best-effort — a logging failure here must
    never break the actual pipeline run.
    """
    if not suppressed:
        return
    try:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        log_path = config.OUTPUT_DIR / "suppressed_leads.csv"
        is_new = not log_path.exists()
        with open(log_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow([
                    "date_logged", "run_id", "address", "city", "state", "zip",
                    "zip_tier", "county", "notice_type", "estimated_value",
                    "estimated_equity", "equity_percent", "mls_last_sold_date",
                    "reason_suppressed",
                ])
            for n, reason in suppressed:
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d"), n.run_id, n.address,
                    n.city, n.state, n.zip, n.zip_tier, n.county,
                    n.notice_type, n.estimated_value, n.estimated_equity,
                    n.equity_percent, n.mls_last_sold_date, reason,
                ])
        logger.info("  Logged %d suppressed lead(s) to %s (QA, not deleted)",
                     len(suppressed), log_path)
    except OSError as e:
        logger.warning("  Could not write suppressed-leads QA log: %s", e)


def _filter_high_potential(
    notices: list[NoticeData],
    target_zips: dict[str, str],
    hard_drop_equity_percent: float,
    skip_zip_filter: bool = False,
    skip_equity_filter: bool = False,
) -> list[NoticeData]:
    """Drop leads outside target ZIPs or with confirmed equity < 20%.

    Sets notice.zip_tier and notice.equity_signal on every surviving
    record (used for DataSift tagging — see datasift_formatter._build_tags)
    regardless of whether either filter is active for that record —
    skip_zip_filter/skip_equity_filter only control whether the matching
    DROP decision runs, not whether the tag is computed.

    ZIP filter: a record is dropped only when it has a CONFIRMED property
    street address (notice.address non-blank — via a parcel-lookup tier or
    a genuine in-text address match) whose ZIP is NOT in target_zips. A
    record with no confirmed property address passes through unfiltered
    even if notice.zip happens to be set — most commonly probate leads in
    a market without a property-address lookup tier (MO's Jackson/Platte/
    Cass, as of this build; MO's Clay, NM, and Knox all have one — see
    mo_parcel_lookup.py / nm_parcel_lookup.py / tax_enricher.py). Requiring
    a real address, not just a non-blank zip, is what actually delivers
    Kaycie's explicit instruction (find the property address before
    dropping on ZIP, don't guess or substitute the PR's mailing ZIP) —
    checking notice.zip alone isn't sufficient, since notice_parser's
    "Strategy 5" last-resort fallback (_extract_city_zip_fallback) can
    populate notice.zip from any plausible-looking ZIP found anywhere in
    the notice text (often the PR's own mailing ZIP) while deliberately
    leaving notice.address blank. Found and fixed 2026-08-09 via a live MO
    run where 22/42 records were dropped as "outside target ZIPs" despite
    only 3 records having a Smarty-confirmed property address that run.

    Equity filter: a record is dropped only when _classify_equity_signal()
    returns "low_equity_drop" — i.e. a real, computed equity_percent below
    HARD_DROP_EQUITY_PERCENT (20%). "low_equity_flag" (20-30%), "unknown",
    and "likely_high_ownership" all pass through unfiltered — the 20-30%
    band is tagged for human review, not auto-dropped, per the 2026-08-09
    review (see module comment above).

    Every dropped record — ZIP or equity — is logged to a QA CSV via
    _write_suppressed_log() rather than silently discarded.
    """
    zip_dropped = 0
    equity_dropped = 0
    result = []
    suppressed: list[tuple[NoticeData, str]] = []

    for n in notices:
        n.equity_signal = _classify_equity_signal(n)

        # A ZIP is only trustworthy as "the property's ZIP" when a real
        # property street address was also resolved (via a parcel-lookup
        # tier or a genuine in-text address match). notice_parser's
        # "Strategy 5" last-resort fallback (_extract_city_zip_fallback)
        # can populate notice.zip from ANY plausible-looking ZIP found
        # anywhere in the notice text — most often the PR/executor's own
        # mailing ZIP embedded in the notice's boilerplate — while
        # deliberately leaving notice.address blank (see its own
        # docstring: "leave address empty"). Gating on address.strip()
        # too, not just zip.strip(), is what actually delivers Kaycie's
        # "find the property address before dropping on ZIP" rule —
        # checking zip alone let an unconfirmed, likely-PR ZIP silently
        # drive real drop decisions. Found 2026-08-09 via a live MO run
        # where 22/42 records were dropped as "outside target ZIPs" despite
        # only 3 records having a Smarty-confirmed property address that
        # same run — the other 19 were fallback ZIPs, not property ZIPs.
        zip_clean = n.zip.strip()
        has_property_address = bool(n.address.strip())
        if zip_clean and has_property_address:
            n.zip_tier = target_zips.get(zip_clean, "")
            if not skip_zip_filter and zip_clean not in target_zips:
                zip_dropped += 1
                suppressed.append((n, "zip_out_of_target"))
                continue
        # else: no confirmed property address (so no trustworthy ZIP) —
        # pass through, don't guess. n.zip_tier stays "" here even if
        # n.zip happens to be set, since that ZIP isn't property-confirmed.

        if not skip_equity_filter and n.equity_signal == "low_equity_drop":
            equity_dropped += 1
            suppressed.append((n, f"equity_below_{hard_drop_equity_percent:.0f}pct"))
            continue

        result.append(n)

    if zip_dropped:
        logger.info("  Removed %d records outside target ZIPs", zip_dropped)
    if equity_dropped:
        logger.info(
            "  Removed %d records with confirmed equity below %.0f%%",
            equity_dropped, hard_drop_equity_percent,
        )
    _write_suppressed_log(suppressed)

    kept = len(result)
    unresolved_zip = sum(1 for n in result if not n.address.strip())
    if unresolved_zip:
        logger.info(
            "  %d/%d surviving records have no confirmed property address yet"
            " — passed through unfiltered rather than dropped (see docstring)",
            unresolved_zip, kept,
        )
    low_equity_flagged = sum(1 for n in result if n.equity_signal == "low_equity_flag")
    if low_equity_flagged:
        logger.info(
            "  %d/%d surviving records flagged \"low_equity\" (20-30%%) — "
            "kept, recommend human review before spending on them",
            low_equity_flagged, kept,
        )
    return result


def _compute_mailable(notices: list[NoticeData]) -> None:
    """Set mailable flag: 'yes' if a real mailing address is present.

    For probate notices, the property address is frequently unknown (see
    _filter_vacant_land). Two other addresses can substitute:
      - The PR/executor's own address, extracted by notice_parser straight
        from the notice text (owner_street/owner_city/owner_zip). Only
        present on notice formats that name a PR with an address — "unknown
        heirs" notices, for example, have neither (confirmed live
        2026-07-17 against a real Jackson County MO notice).
      - The decision-maker's address, found later by obituary_enricher's
        heir/DM lookup (decision_maker_street/city/zip) — this is the
        PRIMARY contact mechanism for probate per the deep-prospecting
        pipeline, not a fallback, and is commonly the ONLY address
        available when the notice names no PR directly (same live case:
        a verified-living heir was identified with 100% confidence, but
        her street address lookup hit a search-provider rate limit, so
        checking decision_maker_street matters even when the lookup
        partially fails).
    Property address is checked first since it's most complete when
    present; the two probate-specific addresses are checked in order as
    fallbacks.
    """
    for n in notices:
        has_property_addr = bool(n.address.strip() and n.city.strip() and n.zip.strip())
        has_pr_addr = bool(
            n.notice_type == "probate"
            and n.owner_street.strip() and n.owner_city.strip() and n.owner_zip.strip()
        )
        has_dm_addr = bool(
            n.notice_type == "probate"
            and n.decision_maker_street.strip()
            and n.decision_maker_city.strip()
            and n.decision_maker_zip.strip()
        )
        n.mailable = "yes" if (has_property_addr or has_pr_addr or has_dm_addr) else ""


# ── Run ID ───────────────────────────────────────────────────────────


def _generate_run_id() -> str:
    """Generate a timestamped run ID for data lineage tracking."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:8]
    return f"{ts}_{short_uuid}"


# ── Data Validation ──────────────────────────────────────────────────

# Regex for garbage OCR: mostly non-alphanumeric characters
_GARBAGE_RE = re.compile(r"^[^a-zA-Z0-9]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_records(notices: list[NoticeData]) -> list[NoticeData]:
    """Validate records before export. Removes invalid records and logs issues.

    Checks:
      - address, city, zip must be non-empty. For most notice types this
        means the property address. For probate, three fallbacks are tried
        in order — property address, then the PR/executor's mailing
        address (owner_street/city/zip, parsed straight from the notice
        text — absent on notice formats that name no PR, like "unknown
        heirs" notices), then the decision-maker's address
        (decision_maker_street/city/zip, found later by obituary_enricher's
        heir/DM lookup — the PRIMARY probate contact mechanism, not a
        fallback, and often the only address available when the notice
        itself names no PR). See _compute_mailable for the same three-tier
        reasoning.
      - address must contain at least one letter (not pure garbage OCR)
      - date fields must be valid YYYY-MM-DD format if present
    """
    valid = []
    invalid_count = 0

    for n in notices:
        issues = []

        if n.notice_type == "probate" and not n.address.strip():
            # No property address — fall back to the PR's mailing address,
            # then the decision-maker's, instead of demanding a property
            # address that's frequently never located.
            if n.owner_street.strip():
                addr, label = n.owner_street, "PR"
                city_attr, state_attr = "owner_city", "owner_state"
            elif n.decision_maker_street.strip():
                addr, label = n.decision_maker_street, "DM"
                city_attr, state_attr = "decision_maker_city", "decision_maker_state"
            else:
                addr, label, city_attr, state_attr = "", "PR/DM", None, None

            if not addr.strip():
                issues.append("missing address (no property, PR, or DM address)")
            elif _GARBAGE_RE.match(addr):
                issues.append(f"garbage {label} address: {addr!r}")
            elif city_attr and not getattr(n, city_attr).strip():
                # A real street/PO Box exists but the notice didn't restate
                # city/state (common — e.g. "P.O. Box 53578, Albuquerque,
                # NM 87153" IS restated and extracts fine, but some notices
                # only give "at the address listed below" with no city).
                # Default from the county's known major_city/state rather
                # than rejecting an otherwise-mailable address — confirmed
                # live 2026-07-22 against real NM probate notices. zip is
                # no longer required in this fallback: street (or PO Box)
                # + a defaulted city/state is enough to be mailable, and
                # Smarty (Step 6, if configured) can complete/validate the
                # zip separately — see standardize_owner_addresses().
                profile = config.COUNTIES.get(n.county.strip().lower())
                if profile:
                    setattr(n, city_attr, profile.major_city)
                    if state_attr and not getattr(n, state_attr).strip():
                        setattr(n, state_attr, profile.state)
                else:
                    issues.append(
                        f"missing {label} city and county {n.county!r} not in "
                        f"registry to default from"
                    )
        else:
            # Required fields
            if not n.address.strip():
                issues.append("missing address")
            elif _GARBAGE_RE.match(n.address):
                issues.append(f"garbage address: {n.address!r}")

            if not n.city.strip():
                issues.append("missing city")

            if not n.zip.strip():
                issues.append("missing zip")

        # Date format validation (only if populated)
        for date_field in ("date_added", "auction_date"):
            val = getattr(n, date_field, "")
            if val and not _DATE_RE.match(val):
                issues.append(f"bad {date_field}: {val!r}")

        if issues:
            invalid_count += 1
            if invalid_count <= 10:
                label = n.address or n.owner_name or "(unknown)"
                logger.warning("  Validation failed [%s]: %s", label, "; ".join(issues))
            continue

        valid.append(n)

    if invalid_count:
        logger.info("  Removed %d invalid records (validation)", invalid_count)
        if invalid_count > 10:
            logger.info("  ... (showing first 10 of %d)", invalid_count)

    return valid


# ── Pipeline ─────────────────────────────────────────────────────────


def run_enrichment_pipeline(
    notices: list[NoticeData],
    opts: PipelineOptions,
) -> list[NoticeData]:
    """Run the full enrichment pipeline on a list of notices.

    Steps (canonical order):
      1. Filter sold properties
      2. Deduplicate
      3. Vacant land filter
      4. Parcel address lookup
      5. Tax delinquency enrichment
      6. Smarty address standardization
      7. Reverse geocode + Smarty retry
      8. Zillow property enrichment
      9. Obituary deceased owner detection
     10. Compute mailable flag
     11. Log summary

    Returns the (possibly filtered) list, modified in-place.
    """
    from data_formatter import deduplicate

    # ── Step 1: Filter Sold ──────────────────────────────────────────
    if not opts.skip_filter_sold:
        logger.info("── Step 1: Filter Sold Properties ──")
        try:
            from data_formatter import filter_sold

            before = len(notices)
            notices = filter_sold(notices)
            removed = before - len(notices)
            if removed:
                logger.info("  Removed %d sold properties", removed)
            else:
                logger.info("  No sold properties found")
        except Exception as e:
            logger.warning("  Filter sold failed: %s", e)

    # ── Stamp run_id on all records ─────────────────────────────────
    run_id = _generate_run_id()
    for n in notices:
        n.run_id = run_id
    logger.info("Pipeline run_id: %s (%d records)", run_id, len(notices))

    # ── Step 2: Deduplicate ──────────────────────────────────────────
    logger.info("── Step 2: Deduplicate ──")
    before = len(notices)
    notices = deduplicate(notices)
    removed = before - len(notices)
    logger.info(
        "  %d records after dedup%s",
        len(notices),
        f" (removed {removed})" if removed else "",
    )

    # ── Step 3: Vacant Land Filter ───────────────────────────────────
    if not opts.skip_vacant_filter:
        logger.info("── Step 3: Vacant Land Filter ──")
        before = len(notices)
        notices = _filter_vacant_land(notices)
        logger.info("  %d records after filter", len(notices))
    if not notices:
        logger.warning("No records remaining after filtering")
        return notices

    # ── Step 3a: Entity Research ──────────────────────────────────
    if not opts.skip_entity_research:
        if config.ANTHROPIC_API_KEY:
            try:
                from entity_researcher import enrich_entity_data

                enrich_entity_data(notices, config.ANTHROPIC_API_KEY)
            except ImportError:
                logger.warning("  entity_researcher not available — skipping")
            except Exception as e:
                logger.warning("  Entity research failed: %s", e)
        else:
            logger.info("── Step 3a: Entity Research (no API key) ──")
    else:
        logger.info("── Step 3a: Entity Research (skipped) ──")

    # ── Step 3b: Entity Owner Filter ──────────────────────────────
    if not opts.skip_entity_filter:
        logger.info("── Step 3b: Entity Owner Filter ──")
        before = len(notices)
        notices = _filter_entity_owners(notices)
        logger.info("  %d records after filter", len(notices))
    else:
        logger.info("── Step 3b: Entity Owner Filter (skipped) ──")
    if not notices:
        logger.warning("No records remaining after filtering")
        return notices

    # ── Step 3c: Probate Property Lookup ────────────────────────────
    # For probate records without a property address, search a county
    # property/tax data source by the decedent's name to find their
    # property. Knox (TN) uses its own Tax API. Bernalillo/Sandoval (NM)
    # use the NM Office of the State Engineer's statewide parcels service
    # instead (added 2026-08-06 — see nm_parcel_lookup.py and CLAUDE.md's
    # "NM Property-Address Lookup Tier" for the full writeup). No other
    # county has an equivalent source yet — see CLAUDE.md's "0/24 property
    # addresses" note for why this gap exists in the first place.
    probate_no_addr_knox = [
        n for n in notices
        if n.notice_type == "probate"
        and not n.address.strip()
        and n.decedent_name.strip()
        and n.county.lower() == "knox"
    ]
    if probate_no_addr_knox:
        logger.info(
            "── Step 3c: Probate Property Lookup — Knox (%d candidates) ──",
            len(probate_no_addr_knox),
        )
        try:
            from tax_enricher import _probate_property_lookup
            _probate_property_lookup(probate_no_addr_knox)
            found = sum(1 for n in probate_no_addr_knox if n.address.strip())
            logger.info("  Property address found: %d/%d", found, len(probate_no_addr_knox))
        except ImportError:
            logger.warning("  _probate_property_lookup not available — skipping")
        except Exception as e:
            logger.warning("  Probate property lookup failed: %s", e)

    probate_no_addr_nm = [
        n for n in notices
        if n.notice_type == "probate"
        and not n.address.strip()
        and n.decedent_name.strip()
        and n.county.lower() in ("bernalillo", "sandoval")
    ]
    if probate_no_addr_nm:
        logger.info(
            "── Step 3c: Probate Property Lookup — NM (%d candidates) ──",
            len(probate_no_addr_nm),
        )
        try:
            from nm_parcel_lookup import probate_property_lookup_nm
            probate_property_lookup_nm(probate_no_addr_nm)
            found = sum(1 for n in probate_no_addr_nm if n.address.strip())
            logger.info("  Property address found: %d/%d", found, len(probate_no_addr_nm))
        except ImportError:
            logger.warning("  nm_parcel_lookup not available — skipping")
        except Exception as e:
            logger.warning("  NM probate property lookup failed: %s", e)

    # Missouri (Clay County only as of this build — added 2026-08-09; see
    # mo_parcel_lookup.py's module docstring for why Jackson/Platte/Cass
    # aren't wired up yet). MO's gap was confirmed worse than NM's
    # pre-tier state (0/6 property addresses on a real scheduled run) —
    # see CLAUDE.md's High-Potential Lead Filter section.
    probate_no_addr_mo = [
        n for n in notices
        if n.notice_type == "probate"
        and not n.address.strip()
        and n.decedent_name.strip()
        and n.county.lower() == "clay"
    ]
    if probate_no_addr_mo:
        logger.info(
            "── Step 3c: Probate Property Lookup — MO (%d candidates) ──",
            len(probate_no_addr_mo),
        )
        try:
            from mo_parcel_lookup import probate_property_lookup_mo
            probate_property_lookup_mo(probate_no_addr_mo)
            found = sum(1 for n in probate_no_addr_mo if n.address.strip())
            logger.info("  Property address found: %d/%d", found, len(probate_no_addr_mo))
        except ImportError:
            logger.warning("  mo_parcel_lookup not available — skipping")
        except Exception as e:
            logger.warning("  MO probate property lookup failed: %s", e)

    # ── Step 4: Parcel Address Lookup ────────────────────────────────
    if not opts.skip_parcel_lookup and not opts.skip_tax:
        candidates = [
            n
            for n in notices
            if n.parcel_id.strip() and n.county.lower() == "knox"
        ]
        if candidates:
            logger.info(
                "── Step 4: Parcel Address Lookup (%d candidates) ──",
                len(candidates),
            )
            try:
                from tax_enricher import lookup_parcel_addresses

                lookup_parcel_addresses(notices)
            except ImportError:
                logger.warning("  tax_enricher not available — skipping")
            except Exception as e:
                logger.warning("  Parcel address lookup failed: %s", e)
        else:
            logger.info("── Step 4: Parcel Address Lookup (no candidates) ──")
    elif opts.skip_parcel_lookup:
        logger.info("── Step 4: Parcel Address Lookup (skipped) ──")

    # ── Step 5: Tax Delinquency ──────────────────────────────────────
    if not opts.skip_tax and not opts.has_tax:
        logger.info("── Step 5: Tax Delinquency Enrichment ──")
        try:
            from tax_enricher import enrich_tax_delinquency

            enrich_tax_delinquency(notices)
            enriched = sum(1 for n in notices if n.tax_delinquent_years)
            logger.info("  Tax-delinquent: %d/%d", enriched, len(notices))
        except ImportError:
            logger.warning("  tax_enricher not available — skipping")
        except Exception as e:
            logger.warning("  Tax enrichment failed: %s", e)
    elif opts.has_tax:
        logger.info("── Step 5: Tax Delinquency (preserved — data already present) ──")
    elif opts.skip_tax:
        logger.info("── Step 5: Tax Delinquency (skipped) ──")

    # ── Step 6: Smarty Address Standardization ───────────────────────
    if not opts.skip_smarty and not opts.has_smarty:
        if config.SMARTY_AUTH_ID and config.SMARTY_AUTH_TOKEN:
            logger.info("── Step 6: Smarty Address Standardization ──")
            try:
                from address_standardizer import standardize_addresses

                standardize_addresses(
                    notices, config.SMARTY_AUTH_ID, config.SMARTY_AUTH_TOKEN
                )
                confirmed = sum(
                    1 for n in notices if n.dpv_match_code == "Y"
                )
                logger.info(
                    "  USPS-confirmed: %d/%d", confirmed, len(notices)
                )
            except ImportError:
                logger.warning(
                    "  smartystreets-python-sdk not installed — skipping"
                )
            except Exception as e:
                logger.warning("  Smarty standardization failed: %s", e)

            # Probate PR/DM mailing addresses that have a street but no
            # full city/zip (the notice didn't restate it) — a known
            # county + partial street is exactly what Smarty can complete.
            # Runs before Step 9b's county-major_city fallback, so a real
            # Smarty-confirmed city/zip wins over the coarser default when
            # available.
            try:
                from address_standardizer import standardize_owner_addresses

                standardize_owner_addresses(
                    notices, config.SMARTY_AUTH_ID, config.SMARTY_AUTH_TOKEN
                )
            except ImportError:
                pass  # already warned above if the SDK itself is missing
            except Exception as e:
                logger.warning("  Smarty owner-address standardization failed: %s", e)
        else:
            logger.info("── Step 6: Smarty (no API keys configured) ──")
    elif opts.has_smarty:
        logger.info(
            "── Step 6: Smarty (preserved — data already present) ──"
        )
    elif opts.skip_smarty:
        logger.info("── Step 6: Smarty (skipped) ──")

    # ── Step 6a: Commercial Property Filter ─────────────────────────
    if not opts.skip_commercial_filter:
        logger.info("── Step 6a: Commercial Property Filter ──")
        before = len(notices)
        notices = _filter_commercial(notices)
        logger.info("  %d records after filter", len(notices))
        if not notices:
            logger.warning("No records remaining after filtering")
            return notices
    else:
        logger.info("── Step 6a: Commercial Property Filter (skipped) ──")

    # ── Step 7: Reverse Geocode Retry ────────────────────────────────
    if (
        not opts.skip_geocode
        and not opts.skip_smarty
        and not opts.has_smarty
    ):
        if config.SMARTY_AUTH_ID and config.SMARTY_AUTH_TOKEN:
            logger.info("── Step 7: Reverse Geocode + Smarty Retry ──")
            try:
                from address_standardizer import retry_with_geocoded_city

                retry_with_geocoded_city(
                    notices,
                    config.SMARTY_AUTH_ID,
                    config.SMARTY_AUTH_TOKEN,
                )
            except ImportError:
                pass  # Function may not exist in older builds
            except Exception as e:
                logger.warning("  Reverse geocode retry failed: %s", e)
    else:
        skip_reason = (
            "skipped"
            if opts.skip_geocode
            else "Smarty skipped/preserved"
        )
        logger.info("── Step 7: Reverse Geocode (%s) ──", skip_reason)

    # ── Step 8: Zillow Property Enrichment ───────────────────────────
    if not opts.skip_zillow and not opts.has_zillow:
        if config.OPENWEBNINJA_API_KEY:
            logger.info("── Step 8: Zillow Property Enrichment ──")
            try:
                from property_enricher import enrich_properties

                enrich_properties(notices, config.OPENWEBNINJA_API_KEY)
                enriched = sum(1 for n in notices if n.estimated_value)
                logger.info(
                    "  Zillow-enriched: %d/%d", enriched, len(notices)
                )
            except ImportError:
                logger.warning(
                    "  property_enricher not available — skipping"
                )
            except Exception as e:
                logger.warning("  Zillow enrichment failed: %s", e)
        else:
            logger.info("── Step 8: Zillow (no API key configured) ──")
    elif opts.has_zillow:
        logger.info(
            "── Step 8: Zillow (preserved — data already present) ──"
        )
    elif opts.skip_zillow:
        logger.info("── Step 8: Zillow (skipped) ──")

    # ── Step 8b: High-Potential Filter (target ZIPs + equity) ───────
    # Runs right after Zillow (Step 8), the step that populates
    # equity_percent, and before Step 9 (Obituary) — the most expensive
    # step (LLM calls + web search) — so leads that don't meet the bar
    # never reach it. See _filter_high_potential()'s docstring for the
    # two hard rules (never drop on missing equity data; never drop a
    # probate lead on ZIP before we've tried to find its property
    # address).
    both_skipped = opts.skip_high_potential_filter or (opts.skip_zip_filter and opts.skip_equity_filter)
    if not both_skipped:
        logger.info("── Step 8b: High-Potential Filter (target ZIPs + equity) ──")
        if opts.skip_zip_filter:
            logger.info("  ZIP filter disabled (--include-off-target) — tagging only, no drops")
        if opts.skip_equity_filter:
            logger.info("  Equity filter disabled (--include-low-equity) — tagging only, no drops")
        notices = _filter_high_potential(
            notices,
            target_zips=opts.target_zips if opts.target_zips is not None else config.TARGET_ZIPS,
            hard_drop_equity_percent=(
                opts.hard_drop_equity_percent
                if opts.hard_drop_equity_percent is not None
                else config.HARD_DROP_EQUITY_PERCENT
            ),
            skip_zip_filter=opts.skip_zip_filter,
            skip_equity_filter=opts.skip_equity_filter,
        )
        logger.info("  %d records after filter", len(notices))
        if not notices:
            logger.warning("No records remaining after filtering")
            return notices
    else:
        logger.info("── Step 8b: High-Potential Filter (skipped) ──")

    # ── Step 9: Obituary Enrichment ──────────────────────────────────
    if not opts.skip_obituary and not opts.has_obituary:
        if config.ANTHROPIC_API_KEY:
            logger.info("── Step 9: Obituary Deceased Owner Detection ──")
            try:
                from obituary_enricher import enrich_obituary_data

                enrich_obituary_data(
                    notices,
                    config.ANTHROPIC_API_KEY,
                    skip_heir_verification=opts.skip_heir_verification,
                    max_heir_depth=opts.max_heir_depth,
                    skip_dm_address=opts.skip_dm_address,
                    tracerfy_tier1=getattr(opts, "tracerfy_tier1", False),
                    skip_ancestry=opts.skip_ancestry,
                )
                confirmed = sum(1 for n in notices if n.owner_deceased)
                logger.info(
                    "  Obituary-confirmed deceased: %d/%d",
                    confirmed,
                    len(notices),
                )
            except ImportError:
                logger.warning(
                    "  obituary_enricher not available — skipping"
                )
            except Exception as e:
                logger.warning("  Obituary enrichment failed: %s", e)
        else:
            logger.info(
                "── Step 9: Obituary (no Anthropic API key configured) ──"
            )
    elif opts.has_obituary:
        logger.info(
            "── Step 9: Obituary (preserved — data already present) ──"
        )
    elif opts.skip_obituary:
        logger.info("── Step 9: Obituary (skipped) ──")

    # ── Step 9b: Data Validation ────────────────────────────────────
    logger.info("── Step 9b: Data Validation ──")
    before = len(notices)
    notices = _validate_records(notices)
    logger.info("  %d records after validation", len(notices))
    if not notices:
        logger.warning("No records remaining after validation")
        return notices

    # ── Step 10: Compute Mailable Flag ───────────────────────────────
    logger.info("── Step 10: Compute Mailable Flag ──")
    _compute_mailable(notices)
    mailable = sum(1 for n in notices if n.mailable)
    logger.info(
        "  Mailable: %d/%d (%.0f%%)",
        mailable,
        len(notices),
        100 * mailable / len(notices) if notices else 0,
    )

    # ── Step 11: Summary ─────────────────────────────────────────────
    _log_summary(notices, opts)

    return notices


# ── Summary ──────────────────────────────────────────────────────────


def _log_summary(notices: list[NoticeData], opts: PipelineOptions) -> None:
    """Log comprehensive summary stats after pipeline completes."""
    total = len(notices)
    if not total:
        return

    logger.info("══ Pipeline Summary (%s) ══", opts.source_label or "unknown")
    logger.info("Total records: %d", total)

    # By type / county
    by_type: dict[str, int] = {}
    by_county: dict[str, int] = {}
    for n in notices:
        by_type[n.notice_type] = by_type.get(n.notice_type, 0) + 1
        by_county[n.county] = by_county.get(n.county, 0) + 1
    for ntype, count in sorted(by_type.items()):
        logger.info("  %s: %d", ntype, count)
    for county, count in sorted(by_county.items()):
        logger.info("  %s county: %d", county, count)

    # Smarty
    smarty_confirmed = sum(1 for n in notices if n.dpv_match_code == "Y")
    if smarty_confirmed:
        logger.info(
            "  Smarty USPS-confirmed: %d/%d (%.0f%%)",
            smarty_confirmed,
            total,
            100 * smarty_confirmed / total,
        )

    # Mailable
    mailable = sum(1 for n in notices if n.mailable)
    logger.info(
        "  Mailable: %d/%d (%.0f%%)",
        mailable,
        total,
        100 * mailable / total,
    )

    # Zillow
    zillow_enriched = sum(1 for n in notices if n.estimated_value)
    if zillow_enriched:
        logger.info("  Zillow-enriched: %d/%d", zillow_enriched, total)
        equity_values = [
            float(n.estimated_equity)
            for n in notices
            if n.estimated_equity
        ]
        if equity_values:
            avg_equity = sum(equity_values) / len(equity_values)
            logger.info("  Avg estimated equity: $%s", f"{avg_equity:,.0f}")

    # Tax
    tax_enriched = sum(1 for n in notices if n.tax_delinquent_years)
    if tax_enriched:
        logger.info("  Tax-delinquent: %d/%d", tax_enriched, total)

    # Deceased indicators
    deceased_count = sum(1 for n in notices if n.deceased_indicator)
    if deceased_count:
        from collections import Counter

        by_indicator = Counter(
            n.deceased_indicator for n in notices if n.deceased_indicator
        )
        breakdown = ", ".join(
            f"{k}: {v}" for k, v in by_indicator.most_common()
        )
        logger.info(
            "  Likely deceased: %d/%d (%s)", deceased_count, total, breakdown
        )

    # Obituary-confirmed
    obit_confirmed = sum(1 for n in notices if n.owner_deceased)
    if obit_confirmed:
        logger.info(
            "  Obituary-confirmed deceased: %d/%d", obit_confirmed, total
        )
        with_dm = sum(1 for n in notices if n.decision_maker_name)
        if with_dm:
            dm_verified = sum(
                1
                for n in notices
                if n.decision_maker_status == "verified_living"
            )
            dm_from_tax = sum(
                1
                for n in notices
                if n.decision_maker_source == "tax_record_joint_owner"
            )
            logger.info(
                "  Decision-maker ID'd: %d/%d (%.0f%%)",
                with_dm,
                obit_confirmed,
                100 * with_dm / obit_confirmed,
            )
            if dm_verified:
                logger.info("    Verified living: %d", dm_verified)
            if dm_from_tax:
                logger.info("    From tax record: %d", dm_from_tax)

    # Probate
    probate_total = sum(1 for n in notices if n.notice_type == "probate")
    if probate_total:
        probate_with_addr = sum(
            1
            for n in notices
            if n.notice_type == "probate" and n.address
        )
        logger.info(
            "  Probate with address: %d/%d",
            probate_with_addr,
            probate_total,
        )

    # High-Potential Filter — equity signal breakdown on survivors
    _hp_both_skipped = opts.skip_high_potential_filter or (opts.skip_zip_filter and opts.skip_equity_filter)
    if not _hp_both_skipped:
        signal_counts: dict[str, int] = {}
        for n in notices:
            if n.equity_signal:
                signal_counts[n.equity_signal] = signal_counts.get(n.equity_signal, 0) + 1
        if signal_counts:
            # Note: "low_equity_drop" deliberately absent — those records
            # were suppressed in Step 8b and never reach this summary.
            breakdown = ", ".join(
                f"{label}: {signal_counts.get(key, 0)}"
                for key, label in (
                    ("confirmed_high", "confirmed high (30%+)"),
                    ("low_equity_flag", "flagged low equity (20-30%)"),
                    ("likely_high_ownership", "likely high (ownership)"),
                    ("unknown", "unknown"),
                )
                if signal_counts.get(key, 0)
            )
            logger.info("  Equity signal (survivors): %s", breakdown)
