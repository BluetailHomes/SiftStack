"""Tests for scraper_ok.py's page-segmentation, venue-verification, and
dedup logic — the pure-text-processing pieces that don't need a live HTTP
session. Fixture text below is copied verbatim from two real pages
downloaded live from opa.eclipping.org on 2026-09-14 (Tulsa World,
2025-02-11 and 2025-09-14 issues) — see scraper_ok.py's module docstring
for the full investigation these fixtures come from.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from scraper_ok import (
    _date_range_preset_for_days,
    _normalize_page_text,
    _notice_dedup_key,
    _resolve_notice_venue,
    _segment_page_into_candidates,
)

# Real bundled page text (2026-09-14 live download) mixing a forfeiture
# notice, two genuine Tulsa County probate estates, and unrelated
# civil-suit/classified-ad filler in between — exactly the shape
# scraper_ks.py's own docstring describes for Kansas, confirmed here to
# also be true of Oklahoma.
REAL_BUNDLED_PAGE = """IN THE DISTRICT COURT OF TULSA COUNTY
STATE OF OKLAHOMA
STATE OF OKLAHOMA,
Steve Kunzweiler, District Attorney,
vs. $4,008.00 AND TWO (2) GUNS
CASE NO. CV-2024-1946

THE STATE OF OKLAHOMA TO: Deserae Marie Lemoine. You are hereby notified that the State of Oklahoma has seized and intends to forfeit $4,008.00 AND TWO (2) GUNS.

SALE DATE FEBRUARY 19, 2025

Published in the Tulsa World,
Tulsa, Tulsa County, Oklahoma,
February 4,11,2025
IN THE DISTRICT COURT WITHIN
AND FOR TULSA COUNTY
STATE OF OKLAHOMA

IN THE MATTER OF THE ESTATE OF
CLIFFORD F. BEAN,
Deceased.

Notice to Creditors: Letters Testamentary have been issued. Personal Representative: Jane Bean.
Case No. PB-2025-0031

Published in the Tulsa World, Tulsa, Tulsa County, Oklahoma, February 4,11,18, 2025
IN THE DISTRICT COURT OF TULSA COUNTY
STATE OF OKLAHOMA
Case No. CS-24-7143

CREDIT ACCEPTANCE CORPORATION,
v.
NIKITA CONERLY,
Plaintiff,
Defendant.

FREE STUMP GRINDING
with tree removal, Tree Trimming & FREE est..

Published in the Tulsa World, Tulsa, Tulsa County, Oklahoma, February 4, 11,2025
IN THE DISTRICT COURT OF TULSA COUNTY STATE OF OKLAHOMA
IN THE MATTER OF THE ESTATE OF
Mary lola Haxel Brown, Deceased.
Case No. PB-2025-0035

Notice to Creditors is hereby given that Letters of Administration have been issued to the Personal Representative.

Published in the Tulsa World, Tulsa, Tulsa County, Oklahoma, February 11,18,25, 2025"""

# Real text (2026-09-14 live download, 2025-09-14 issue) — a genuine
# out-of-state probate notice found inside a local Tulsa paper, the exact
# false-positive that motivates _resolve_notice_venue()'s state check.
REAL_OUT_OF_STATE_NOTICE = """IN THE CIRCUIT COURT OF SEBASTIAN COUNTY, ARKANSAS PROBATE
DIVISION VI
IN THE MATTER OF THE ESTATE OF
RUBY MAE COCHRAN, Deceased
WARNING ORDER
No. 66GPR-25-85
TO: Angela Cochran Mills, HEIR OF RUBY MAE COCHRAN
You are hereby notified that The Estate of Ruby Mae Cochran... has filed a Petition for Authority to Sell Real Estate.

Published in the Tulsa World, Tulsa, Tulsa County, Oklahoma, August 31, September 7,14,21,2025"""


def test_segment_splits_bundled_page_into_expected_notice_count():
    # Real production flow normalizes the raw pdfminer text (collapsing the
    # PDF's original line breaks) before segmenting — see
    # _normalize_page_text()'s docstring for why the footer regex needs
    # that done first.
    segments = _segment_page_into_candidates(_normalize_page_text(REAL_BUNDLED_PAGE))
    # 4 footers in the fixture -> 4 segments: forfeiture, Bean estate, the
    # civil suit (which has no ESTATE OF language and gets filtered out
    # later by the caller, not by segmentation itself), and the Brown estate.
    assert len(segments) == 4
    assert "CLIFFORD F. BEAN" in segments[1]
    assert "CREDIT ACCEPTANCE" in segments[2]
    assert "Mary lola Haxel Brown" in segments[3]


def test_segment_single_footer_still_splits_into_two():
    text = "NOTICE ONE TEXT\nPublished in the Tulsa World, Tulsa, Tulsa County, Oklahoma, January 1, 2025\nTRAILING TEXT WITH NO FOOTER"
    segments = _segment_page_into_candidates(_normalize_page_text(text))
    assert len(segments) == 2
    assert "NOTICE ONE TEXT" in segments[0]
    assert "TRAILING TEXT" in segments[1]


def test_segment_no_footer_returns_whole_text():
    text = "just some text with no boundary marker at all"
    assert _segment_page_into_candidates(text) == [text]


def test_normalize_page_text_collapses_line_wraps_and_dehyphenates():
    text = "IN THE DISTRICT COURT OF TUL-\nSA COUNTY\nSTATE OF OKLAHOMA"
    assert _normalize_page_text(text) == "IN THE DISTRICT COURT OF TULSA COUNTY STATE OF OKLAHOMA"


def test_resolve_venue_finds_tulsa_oklahoma():
    county, state = _resolve_notice_venue("IN THE DISTRICT COURT OF TULSA COUNTY STATE OF OKLAHOMA")
    assert county == "Tulsa"
    assert state == "Oklahoma"


def test_resolve_venue_within_and_for_phrasing():
    county, state = _resolve_notice_venue("IN THE DISTRICT COURT WITHIN AND FOR TULSA COUNTY STATE OF OKLAHOMA")
    assert county == "Tulsa"
    assert state == "Oklahoma"


def test_resolve_venue_rejects_out_of_state_real_sample():
    county, state = _resolve_notice_venue(REAL_OUT_OF_STATE_NOTICE)
    assert county == "Sebastian"
    assert state == "Arkansas"


def test_resolve_venue_comma_before_state():
    county, state = _resolve_notice_venue("IN THE CIRCUIT COURT OF SEBASTIAN COUNTY, ARKANSAS PROBATE DIVISION VI")
    assert county == "Sebastian"
    assert state == "Arkansas"


def test_resolve_venue_no_state_word_is_unknown_not_rejected():
    # "PROBATE" isn't a real state name — must not be trusted as one.
    county, state = _resolve_notice_venue("IN THE DISTRICT COURT OF TULSA COUNTY PROBATE DIVISION")
    assert county == "Tulsa"
    assert state is None


def test_resolve_venue_no_match_returns_none_none():
    county, state = _resolve_notice_venue("just some unrelated ad copy with no court caption")
    assert county is None
    assert state is None


def test_dedup_key_uses_case_number_when_present():
    key = _notice_dedup_key("https://example.com/page.pdf", "Case No. PB-2025-0035 more text")
    assert key == "PB-2025-0035"


def test_dedup_key_case_insensitive_case_number():
    key = _notice_dedup_key("https://example.com/page.pdf", "case no. pb-2025-0035")
    assert key == "PB-2025-0035"


def test_dedup_key_falls_back_to_hash_without_case_number():
    key1 = _notice_dedup_key("https://example.com/tuw-page.pdf", "some notice text with no case number")
    key2 = _notice_dedup_key("https://example.com/tuw-page.pdf", "different notice text on same page")
    assert key1 != key2
    assert key1.startswith("tuw-page.pdf:")


def test_date_range_preset_picks_smallest_covering_value():
    assert _date_range_preset_for_days(1) == 1
    assert _date_range_preset_for_days(2) == 3
    assert _date_range_preset_for_days(7) == 7
    assert _date_range_preset_for_days(8) == 30
    assert _date_range_preset_for_days(400) == 365
