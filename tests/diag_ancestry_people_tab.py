"""Follow-up diagnostic: explore the "People" search tab (as opposed to
"Records"), which is the likely home for Public Member Tree results on
the current Ancestry search UI.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_ancestry_people_tab.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ancestry_enricher

OUT_DIR = os.path.join(os.path.dirname(__file__), "diag_output")
FIRST_NAME = "Daniel"
LAST_NAME = "Williams"


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    pw, context, page = await ancestry_enricher.launch_browser()
    if not page:
        print("Could not launch/login to Ancestry")
        return

    try:
        # Land on the generic search results (same as before)
        url = f"{ancestry_enricher.ANCESTRY_URL}/search/?name={FIRST_NAME}+{LAST_NAME}"
        await page.goto(url, wait_until="domcontentloaded")
        await ancestry_enricher._delay(3, 4)
        print(f"Landed on: {page.url}")

        # Find and click the "People" tab
        people_tab = await page.query_selector("text='People'")
        if not people_tab:
            # Try link-based selector
            people_tab = await page.query_selector("a:has-text('People')")
        if people_tab:
            print("Found 'People' tab — clicking...")
            await people_tab.click()
            await ancestry_enricher._delay(3, 5)
        else:
            print("No 'People' tab found via text selector")

        print(f"URL after clicking People tab: {page.url}")
        await page.screenshot(path=os.path.join(OUT_DIR, "people_tab.png"), full_page=True)

        html = await page.content()
        html_path = os.path.join(OUT_DIR, "people_tab.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Saved HTML: {html_path} ({len(html):,} bytes)")

        # Probe selectors
        candidate_selectors = [
            "[class*='searchResult']",
            "[data-testid*='result']",
            "table tbody tr",
            "[class*='treeResult']",
            "[class*='TreeResult']",
            "[class*='personResult']",
            "[class*='PersonResult']",
            "[class*='memberTree']",
            "[class*='MemberTree']",
        ]
        print("\n--- Selector probe (People tab) ---")
        results = []
        for sel in candidate_selectors:
            count = await page.locator(sel).count()
            print(f"  {sel!r}: {count} matches")
            results.append((sel, count))

        results.sort(key=lambda x: x[1], reverse=True)
        best_sel, best_count = results[0]
        if best_count:
            print(f"\n--- First 3 via best selector {best_sel!r} ---")
            for i in range(min(3, best_count)):
                text = await page.locator(best_sel).nth(i).inner_text()
                print(f"\n[card {i}]\n{text[:600]}")

        print("\nKeeping browser open 20s for manual inspection...")
        await asyncio.sleep(20)

    finally:
        await ancestry_enricher.close_browser(pw, context)


if __name__ == "__main__":
    asyncio.run(main())
