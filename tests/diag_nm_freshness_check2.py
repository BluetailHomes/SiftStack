"""Search newmexicopublicnotices.com for Bernalillo county notices (no login
required) and check the most recent notice dates in the results, to see
whether this site is still actively publishing or has gone stale.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_nm_freshness_check2.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

NM_BASE = "https://www.newmexicopublicnotices.com"
OUT_DIR = os.path.join(os.path.dirname(__file__), "diag_output")


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(f"{NM_BASE}/", wait_until="networkidle")

        # Bernalillo is the first county checkbox (alphabetical list, confirmed
        # via countyDiv text dump: "Bernalillo Catron Chaves Cibola Colfax...")
        checkbox = page.locator("#ctl00_ContentPlaceHolder1_as1_lstCounty_0")
        label_text = await page.evaluate(
            "() => document.querySelector('#ctl00_ContentPlaceHolder1_as1_lstCounty_0')"
            ".closest('li')?.textContent?.trim()"
        )
        print(f"Checkbox 0 label: {label_text!r}")
        # The county filter accordion is collapsed by default — element
        # exists but fails Playwright's visibility check. JS click bypasses
        # actionability checks (same pattern used elsewhere in this repo
        # for React/ASP.NET elements that are present but not "visible").
        clicked = await page.evaluate("""() => {
            const el = document.querySelector('#ctl00_ContentPlaceHolder1_as1_lstCounty_0');
            if (!el) return false;
            el.checked = true;
            el.dispatchEvent(new Event('click', {bubbles: true}));
            return true;
        }""")
        print(f"Clicked Bernalillo checkbox via JS: {clicked}")
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)

        go_btn = page.locator("[id$='btnGo']")
        print(f"Go button count: {await go_btn.count()}")
        await go_btn.first.evaluate("el => el.click()")
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)

        print(f"Results URL: {page.url}")
        await page.screenshot(path=os.path.join(OUT_DIR, "freshness2_results.png"), full_page=True)

        html = await page.content()
        with open(os.path.join(OUT_DIR, "freshness2_results.html"), "w", encoding="utf-8") as f:
            f.write(html)

        # Extract dates near "Bernalillo" mentions in the results
        import re
        body_text = await page.locator("body").inner_text()
        dates = re.findall(r"\d{1,2}/\d{1,2}/\d{4}", body_text)
        print(f"\nAll dates found on results page ({len(dates)}):")
        print(sorted(set(dates), reverse=True)[:20])

        print("\n--- Body text (first 4000 chars) ---")
        print(body_text[:4000])

        await asyncio.sleep(10)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
