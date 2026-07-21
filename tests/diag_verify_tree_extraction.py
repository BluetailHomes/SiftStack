"""One-off diagnostic: directly call the new _search_family_trees() against
"Daniel Williams Carlton" (Atlanta GA, died 6 Mar 2021) — a case already
confirmed via live DOM inspection (tests/diag_ancestry_tree_profile2.py) to
have real, non-private parent names (William Daniel Carlton Jr, Virginia
Anne Williams). This positively verifies the extraction logic end-to-end
through the actual implemented function, independent of whether the SSDI/
obituary tiers happen to confirm a death for a given search.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_verify_tree_extraction.py
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ancestry_enricher

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)-7s %(message)s")


async def main():
    pw, context, page = await ancestry_enricher.launch_browser()
    if not page:
        print("Could not launch/login to Ancestry")
        return

    try:
        members = await ancestry_enricher._search_family_trees(
            page,
            first_name="Daniel",
            last_name="Carlton",
            expected_death_date="2021",
            state="GA",
            city="Atlanta",
            middle_initial="W",
        )
        print()
        print("=" * 60)
        print(f"Extracted {len(members)} family member(s):")
        for fm in members:
            print(f"  - {fm.get('relationship', '?'):10s} {fm.get('name', '')}")
        print("=" * 60)

    finally:
        await ancestry_enricher.close_browser(pw, context)


if __name__ == "__main__":
    asyncio.run(main())
