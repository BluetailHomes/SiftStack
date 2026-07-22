"""One-off diagnostic: explore newmexico.column.us with a headed browser —
Column, PBC is a different vendor (JS SPA) from the ASP.NET WebForms
platform (lrsws.co) this codebase's scraper automates. Log in, look at the
real UI, and capture network requests to see if there's a JSON API worth
building against instead of scraping rendered HTML.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_column_us_explore.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config as cfg

COLUMN_BASE = "https://newmexico.column.us"
OUT_DIR = os.path.join(os.path.dirname(__file__), "diag_output")


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    from playwright.async_api import async_playwright

    captured_requests = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        def on_request(req):
            # Track XHR/fetch calls (likely API traffic for a JS SPA)
            if req.resource_type in ("xhr", "fetch"):
                captured_requests.append({
                    "method": req.method,
                    "url": req.url,
                    "resource_type": req.resource_type,
                })

        page.on("request", on_request)

        print(f"Navigating to {COLUMN_BASE} ...")
        # SPA — like DataSift's app.reisift.io, this likely keeps background
        # connections open (websockets/polling), so networkidle never
        # fires. Use domcontentloaded + a fixed settle delay instead.
        await page.goto(COLUMN_BASE, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(4)
        print(f"Landed on: {page.url}")
        print(f"Title: {await page.title()}")
        await page.screenshot(path=os.path.join(OUT_DIR, "column_01_home.png"), full_page=True)

        # Dump visible text to understand the landing page
        body_text = await page.locator("body").inner_text()
        print("\n--- Landing page text (first 2000 chars) ---")
        print(body_text[:2000])

        await asyncio.sleep(3)
        await browser.close()

    print(f"\n--- Captured {len(captured_requests)} XHR/fetch requests on landing ---")
    for r in captured_requests[:40]:
        print(f"  {r['method']} {r['url']}")

    with open(os.path.join(OUT_DIR, "column_requests_landing.json"), "w", encoding="utf-8") as f:
        json.dump(captured_requests, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
