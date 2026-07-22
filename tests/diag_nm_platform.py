"""One-off diagnostic: verify newmexicopublicnotices.com login + Smart Search
dashboard access using the existing MO selectors from config.py, before
assuming the shared-vendor (lrsws.co) hypothesis holds for the live site,
not just static HTML fetched via requests.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_nm_platform.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config as cfg

NM_BASE_URL = "https://www.newmexicopublicnotices.com"
NM_LOGIN_URL = f"{NM_BASE_URL}/authenticate.aspx"
NM_SMART_SEARCH_URL = f"{NM_BASE_URL}/SmartSearch/Default.aspx"

OUT_DIR = os.path.join(os.path.dirname(__file__), "diag_output")


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        context.set_default_timeout(30_000)
        page = await context.new_page()

        print(f"Navigating to {NM_LOGIN_URL} ...")
        await page.goto(NM_LOGIN_URL, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")
        await page.screenshot(path=os.path.join(OUT_DIR, "nm_01_login_page.png"), full_page=True)

        print(f"Email configured: {bool(cfg.NOTICE_SITE_EMAIL)}")
        print(f"Password configured: {bool(cfg.NOTICE_SITE_PASSWORD)}")

        await page.fill(cfg.SEL_LOGIN_EMAIL, cfg.NOTICE_SITE_EMAIL)
        await page.fill(cfg.SEL_LOGIN_PASSWORD, cfg.NOTICE_SITE_PASSWORD)
        await page.click(cfg.SEL_LOGIN_SUBMIT)
        await page.wait_for_load_state("networkidle")

        print(f"After submit: {page.url}")
        await page.screenshot(path=os.path.join(OUT_DIR, "nm_02_after_login.png"), full_page=True)

        if "smartsearch" in page.url.lower():
            print("LOGIN SUCCESS — on Smart Search dashboard")
        else:
            print("LOGIN FAILED or unexpected redirect")
            body = await page.locator("body").inner_text()
            print("Body snippet:", " ".join(body.split())[:1000])
            await browser.close()
            return

        # Navigate explicitly to Smart Search (in case login redirect differs)
        await page.goto(NM_SMART_SEARCH_URL, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")
        print(f"Smart Search URL: {page.url}")
        await page.screenshot(path=os.path.join(OUT_DIR, "nm_03_smartsearch.png"), full_page=True)

        # Check saved-searches dropdown selector + list current options
        dropdown = await page.query_selector(cfg.SEL_SAVED_SEARCHES_DROPDOWN)
        if dropdown:
            print(f"Saved-searches dropdown FOUND via {cfg.SEL_SAVED_SEARCHES_DROPDOWN!r}")
            options = await page.eval_on_selector_all(
                f"{cfg.SEL_SAVED_SEARCHES_DROPDOWN} option",
                "els => els.map(e => e.textContent.trim())",
            )
            print(f"Current saved search options ({len(options)}):")
            for o in options:
                print(f"  - {o!r}")
        else:
            print(f"Saved-searches dropdown NOT FOUND via {cfg.SEL_SAVED_SEARCHES_DROPDOWN!r}")

        html = await page.content()
        with open(os.path.join(OUT_DIR, "nm_smartsearch.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Saved HTML ({len(html):,} bytes)")

        print("\nKeeping browser open 20s for manual inspection...")
        await asyncio.sleep(20)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
