"""Follow-up diagnostic: find a profile with a visible Children section
(the two prior test profiles didn't have one) to confirm its markup.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_ancestry_tree_profile3.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ancestry_enricher

OUT_DIR = os.path.join(os.path.dirname(__file__), "diag_output")
CANDIDATES = ["Daniel William Roberts", "Daniel Williams Congdon", "Daniel William Deupree"]


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

        for name in CANDIDATES:
            target = await page.query_selector(f"text='{name}'")
            if not target:
                print(f"'{name}' not found in results, trying next...")
                continue

            view_btn = await page.query_selector(
                f"text='{name}' >> xpath=following::*[text()=\"View profile\"][1]"
            )
            if not view_btn:
                print(f"No View profile button near '{name}'")
                continue

            async with context.expect_page() as new_page_info:
                await view_btn.click()
            profile_page = await new_page_info.value
            await profile_page.wait_for_load_state("domcontentloaded")
            await ancestry_enricher._delay(2, 4)

            has_children = await profile_page.evaluate(
                "() => !!Array.from(document.querySelectorAll('h3')).find(h => h.textContent.trim() === 'Children')"
            )
            print(f"{name}: {profile_page.url} — Children section present: {has_children}")

            if has_children:
                html = await profile_page.content()
                idx = html.find('id="familySection"')
                end = html.find("</section>", idx)
                snippet = html[idx-100:end+11]
                with open(os.path.join(OUT_DIR, "family_section_with_children.html"), "w", encoding="utf-8") as f:
                    f.write(snippet)
                await profile_page.screenshot(
                    path=os.path.join(OUT_DIR, "tree_profile3_with_children.png"), full_page=True
                )
                print("Saved family section with children + screenshot")
                await asyncio.sleep(10)
                await profile_page.close()
                break

            await profile_page.close()

        print("\nDone.")

    finally:
        await ancestry_enricher.close_browser(pw, context)


if __name__ == "__main__":
    asyncio.run(main())
