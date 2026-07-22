"""One-off diagnostic: check whether newmexicopublicnotices.com's Bernalillo/
Sandoval county notices are still being published recently, or whether
publication has already moved to newmexico.column.us. The site's public
search sidebar is visible even when NOT logged in (confirmed live), so this
doesn't need working credentials.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_nm_freshness_check.py
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
        print(f"Landed on: {page.url}")
        await page.screenshot(path=os.path.join(OUT_DIR, "freshness_01_home.png"), full_page=True)

        # Dump structure of the "FILTERED BY" accordion (County/City/Publication/Date)
        info = await page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('[id*="County"], [id*="county"]').forEach(el => {
                out.push({tag: el.tagName, id: el.id, cls: el.className,
                           text: (el.textContent || '').trim().substring(0, 60)});
            });
            return out.slice(0, 30);
        }""")
        print(f"County-related elements ({len(info)}):")
        for i in info:
            print(f"  {i}")

        await asyncio.sleep(8)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
