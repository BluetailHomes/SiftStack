"""End-to-end test: Ancestry family-tree search on a known deceased owner.

Follows the tests/test_e2e_obituary.py convention — a manual/live-integration
script, not a pytest suite (see tests/conftest.py collect_ignore). Runs the
full lookup_deceased() cascade, which now supplements family_members with a
family-tree search after any tier confirms the death.

Run:
    .venv/Scripts/python.exe tests/test_e2e_family_tree.py
"""

import asyncio
import os
import sys
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config as cfg
import ancestry_enricher

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)-7s %(message)s")

# Test case: WILLIAMS DANIEL H, 5100 Stokely Ln, Knoxville, Knox County TN
# (same record used in tests/test_e2e_obituary.py). Ancestry's own
# collections use "Daniel Williams" name order, not the tax-record
# "LAST FIRST" order.
NAME = "Daniel H Williams"
CITY = "Knoxville"
STATE = "TN"


async def main():
    if not cfg.ANCESTRY_EMAIL or not cfg.ANCESTRY_PASSWORD:
        print("ERROR: ANCESTRY_EMAIL / ANCESTRY_PASSWORD not set in .env")
        sys.exit(1)

    pw, context, page = await ancestry_enricher.launch_browser()
    if not page:
        print("ERROR: could not launch/login to Ancestry")
        sys.exit(1)

    try:
        print(f"Looking up: {NAME}, {CITY}, {STATE}")
        result = await ancestry_enricher.lookup_deceased(page, name=NAME, city=CITY, state=STATE)

        print()
        print("=" * 60)
        print("RESULT:")
        if not result:
            print("  No match found (or no result confirmed deceased).")
        else:
            print(f"  confirmed_deceased: {result.get('confirmed_deceased')!r}")
            print(f"  full_name:          {result.get('full_name')!r}")
            print(f"  date_of_death:      {result.get('date_of_death')!r}")
            print(f"  source_type:        {result.get('source_type')!r}")
            print(f"  source_url:         {result.get('source_url')!r}")
            family_members = result.get("family_members", [])
            print(f"  family_members ({len(family_members)}):")
            for fm in family_members:
                print(f"    - {fm.get('relationship', '?'):10s} {fm.get('name', '')}")
            if not family_members:
                print("    (none — either no public tree matched, or the matched")
                print("     tree had no non-private family members)")
        print("=" * 60)

    finally:
        await ancestry_enricher.close_browser(pw, context)


if __name__ == "__main__":
    asyncio.run(main())
