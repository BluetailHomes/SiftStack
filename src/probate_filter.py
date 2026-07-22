"""Classify probate notices — keep only real "Notice to Creditors" / estate
administration filings, matching the pattern foreclosure_filter.py already
uses for foreclosure notices.

Built after NM's "probate" saved search (confirmed live 2026-07-22 on
newmexicopublicnotices.com) turned out to match loosely — likely "Any
Words" keyword search against "probate estate personal representative
notice to creditors" — pulling in unrelated legal notices that happen to
share a common word. Confirmed false positives from a live test run:
  - An Albuquerque City Council meeting agenda ("LEGAL NOTICE... THE
    FOLLOWING ORDINANCE WILL BE HEARD BY THE ALBUQUERQUE CITY COUNCIL")
  - A debt-collection civil suit ("NOTICE OF PENDENCY OF SUIT" against a
    named defendant, from a credit union's attorney)
  - A self-storage lien auction ("Extra Space Storage will hold a public
    auction to sell personal property")
Each of these still costs a real 2Captcha solve + LLM extraction call in
production — this filter rejects them before that cost is spent, the same
economic reason foreclosure_filter.py exists.

INCLUDE_PHRASES below were checked against real probate notices from BOTH
markets — MO/TN test fixtures (tests/test_parser_edge_cases.py) and the
NM notices confirmed live 2026-07-22 (Sanchez, Bradley, Martinez) all
consistently use "notice to creditors" and/or "personal representative".
"""

import logging

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

# ── Inclusion keywords ─────────────────────────────────────────────────
# These phrases identify a real probate / estate-administration notice.
# Matched case-insensitively against the full notice text.
INCLUDE_PHRASES = [
    "notice to creditors",
    "personal representative",
    "letters testamentary",
    "letters of administration",
    "in the probate court",
    "probate court",
]

# ── Exclusion keywords ─────────────────────────────────────────────────
# These override inclusion — confirmed false-positive categories from
# NM's loosely-matching "probate" saved search (see module docstring).
EXCLUDE_PHRASES = [
    "notice of pendency of suit",
    "city council",
    "board of county commissioners",
    "public auction to sell personal property",
    "self storage",
    "storage treasures",
    "to satisfy storage debt",
]


def is_valid_probate(notice: NoticeData) -> bool:
    """Determine if a probate-search result is a real estate-administration notice.

    Non-probate notice types always pass through.
    """
    if notice.notice_type != "probate":
        return True  # Non-probate notices pass through unfiltered

    text = notice.raw_text.lower()

    if not text:
        logger.debug("Excluded probate (empty text): %s", notice.source_url)
        return False

    # Check exclusions first — they take priority
    for phrase in EXCLUDE_PHRASES:
        if phrase in text:
            logger.debug("Excluded probate (matched '%s'): %s", phrase, notice.source_url)
            return False

    # Check for inclusion phrases
    for phrase in INCLUDE_PHRASES:
        if phrase in text:
            return True

    # No inclusion phrase matched — exclude by default
    logger.debug("Excluded probate (no estate-administration language): %s", notice.source_url)
    return False
