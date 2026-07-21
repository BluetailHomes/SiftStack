"""Follow-up diagnostic: open "Daniel Williams Carlton" profile (has a
photo avatar in search results, suggesting a fuller public tree entry),
expand the Siblings toggle, and check for a Children section — to fill
gaps from the first profile (Daniel Williams Jackson Jr, who had private
parents, no spouse, no children shown).

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_ancestry_tree_profile2.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ancestry_enricher

OUT_DIR = os.path.join(os.path.dirname(__file__), "diag_output")


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    pw, context, page = await ancestry_enricher.launch_browser()
    if not page:
        print("Could not launch/login to Ancestry")
        return

    try:
        url = f"{ancestry_enricher.ANCESTRY_URL}/search?name=Daniel+Williams&searchMode=simple&types=t"
        await page.goto(url, wait_until="domcontentloaded")
        await ancestry_enricher._delay(3, 5)

        target = await page.query_selector("text='Daniel Williams Carlton'")
        if not target:
            print("Could not find 'Daniel Williams Carlton' in results")
            return

        # Find the "View profile" button within the same card
        card = await target.evaluate_handle(
            "el => el.closest('[class*=\"searchResult\"], [data-testid*=\"result\"], li, div')"
        )
        view_btn = await page.query_selector("text='Daniel Williams Carlton' >> xpath=following::*[text()=\"View profile\"][1]")
        if not view_btn:
            print("Could not find View profile button near target")
            return

        async with context.expect_page() as new_page_info:
            await view_btn.click()
        profile_page = await new_page_info.value
        await profile_page.wait_for_load_state("domcontentloaded")
        await ancestry_enricher._delay(3, 5)

        print(f"Profile URL: {profile_page.url}")
        print(f"Profile title: {await profile_page.title()}")

        # Expand siblings if present
        sib_btn = await profile_page.query_selector("#toggleSiblingsBtn")
        if sib_btn:
            print("Found Siblings toggle — clicking to expand...")
            await sib_btn.click()
            await ancestry_enricher._delay(1, 2)
        else:
            print("No Siblings toggle on this profile")

        await profile_page.screenshot(path=os.path.join(OUT_DIR, "tree_profile2_expanded.png"), full_page=True)

        html = await profile_page.content()
        with open(os.path.join(OUT_DIR, "tree_profile2.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Saved profile HTML ({len(html):,} bytes)")

        # Extract just the familySection for quick reading
        idx = html.find('id="familySection"')
        if idx >= 0:
            end = html.find("</section>", idx)
            snippet = html[idx-100:end+11]
            with open(os.path.join(OUT_DIR, "tree_profile2_family_section.html"), "w", encoding="utf-8") as f:
                f.write(snippet)
            print("Saved familySection snippet")

        print("\nKeeping browsers open 25s for manual inspection...")
        await asyncio.sleep(25)

    finally:
        await ancestry_enricher.close_browser(pw, context)


if __name__ == "__main__":
    asyncio.run(main())
