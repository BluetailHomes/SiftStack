"""Follow-up diagnostic: screenshot immediately after navigating to signin,
before trying to interact with any fields, to see what actually loaded
(persistent profile may carry state from previous run).

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_ancestry_login3.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ancestry_enricher

OUT_DIR = os.path.join(os.path.dirname(__file__), "diag_output")


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    from playwright.async_api import async_playwright

    ancestry_enricher.PROFILE_DIR.mkdir(exist_ok=True)
    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        str(ancestry_enricher.PROFILE_DIR),
        headless=False,
        viewport={"width": 1920, "height": 1080},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = context.pages[0] if context.pages else await context.new_page()

    try:
        await page.goto(ancestry_enricher.SIGNIN_URL, wait_until="domcontentloaded")
        print(f"URL right after goto: {page.url}")
        print(f"Title right after goto: {await page.title()}")
        await page.screenshot(path=os.path.join(OUT_DIR, "login3_immediate.png"), full_page=True)

        await asyncio.sleep(3)
        print(f"URL after 3s: {page.url}")
        print(f"Title after 3s: {await page.title()}")
        await page.screenshot(path=os.path.join(OUT_DIR, "login3_after3s.png"), full_page=True)

        html_path = os.path.join(OUT_DIR, "login3.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(await page.content())
        print(f"Saved HTML: {html_path}")

        print("Keeping browser open 20s for manual inspection...")
        await asyncio.sleep(20)

    finally:
        await context.close()
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
