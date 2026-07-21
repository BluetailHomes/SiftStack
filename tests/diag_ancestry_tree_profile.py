"""Follow-up diagnostic: run a location-narrowed People (types=t) search for
the known test case (Daniel [H] Williams, Knoxville/Knox County TN) and open
the top matching profile to inspect the family-relationships DOM.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_ancestry_tree_profile.py
"""

import asyncio
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ancestry_enricher

OUT_DIR = os.path.join(os.path.dirname(__file__), "diag_output")
FIRST_NAME = "Daniel"
LAST_NAME = "Williams"
BIRTH_LOCATION = "Knoxville, Knox, Tennessee, USA"


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    pw, context, page = await ancestry_enricher.launch_browser()
    if not page:
        print("Could not launch/login to Ancestry")
        return

    try:
        params = {
            "name": f"{FIRST_NAME} {LAST_NAME}",
            "searchMode": "simple",
            "types": "t",
            "residence": BIRTH_LOCATION,
        }
        url = f"{ancestry_enricher.ANCESTRY_URL}/search?{urllib.parse.urlencode(params)}"
        print(f"Navigating to: {url}")
        await page.goto(url, wait_until="domcontentloaded")
        await ancestry_enricher._delay(3, 5)
        print(f"Landed on: {page.url}")
        await page.screenshot(path=os.path.join(OUT_DIR, "tree_search_narrowed.png"), full_page=True)

        html = await page.content()
        with open(os.path.join(OUT_DIR, "tree_search_narrowed.html"), "w", encoding="utf-8") as f:
            f.write(html)

        # Click first "View profile" link
        view_profile = await page.query_selector("text='View profile'")
        if not view_profile:
            print("No 'View profile' link found")
            return

        print("Clicking first 'View profile'...")
        async with context.expect_page() as new_page_info:
            await view_profile.click()
        profile_page = await new_page_info.value
        await profile_page.wait_for_load_state("domcontentloaded")
        await ancestry_enricher._delay(3, 5)

        print(f"Profile URL: {profile_page.url}")
        print(f"Profile title: {await profile_page.title()}")
        await profile_page.screenshot(path=os.path.join(OUT_DIR, "tree_profile.png"), full_page=True)

        profile_html = await profile_page.content()
        with open(os.path.join(OUT_DIR, "tree_profile.html"), "w", encoding="utf-8") as f:
            f.write(profile_html)
        print(f"Saved profile HTML ({len(profile_html):,} bytes)")

        # Probe for family-section selectors
        candidate_selectors = [
            "[class*='family']",
            "[class*='Family']",
            "[class*='relationship']",
            "[class*='Relationship']",
            "[data-testid*='family']",
            "[class*='factPanel']",
            "[class*='FactPanel']",
        ]
        print("\n--- Selector probe (profile page) ---")
        for sel in candidate_selectors:
            count = await profile_page.locator(sel).count()
            print(f"  {sel!r}: {count} matches")

        print("\nKeeping browsers open 25s for manual inspection...")
        await asyncio.sleep(25)

    finally:
        await ancestry_enricher.close_browser(pw, context)


if __name__ == "__main__":
    asyncio.run(main())
