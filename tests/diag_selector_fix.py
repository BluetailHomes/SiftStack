"""One-off diagnostic: verify SEL_VIEW_BUTTON_PATTERN's new :visible clause
returns exactly one button per grid row (not two), and that clicking
sequential indices actually lands on sequential Details.aspx notices.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_selector_fix.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from playwright.async_api import async_playwright

from config import (
    LOGIN_URL,
    SEL_LOGIN_EMAIL,
    SEL_LOGIN_PASSWORD,
    SEL_LOGIN_SUBMIT,
    SEL_SAVED_SEARCHES_DROPDOWN,
    SEL_VIEW_BUTTON_PATTERN,
    NOTICE_SITE_EMAIL,
    NOTICE_SITE_PASSWORD,
)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        context.set_default_timeout(60_000)
        page = await context.new_page()

        await page.goto(LOGIN_URL)
        await page.wait_for_load_state("networkidle")
        await page.fill(SEL_LOGIN_EMAIL, NOTICE_SITE_EMAIL)
        await page.fill(SEL_LOGIN_PASSWORD, NOTICE_SITE_PASSWORD)
        await page.click(SEL_LOGIN_SUBMIT)
        await page.wait_for_load_state("networkidle")

        async with page.expect_navigation(wait_until="networkidle", timeout=30000):
            await page.select_option(SEL_SAVED_SEARCHES_DROPDOWN, label="Jackson County Probate")

        buttons = await page.query_selector_all(SEL_VIEW_BUTTON_PATTERN)
        print(f"Buttons matched with new :visible selector: {len(buttons)}")
        for i, b in enumerate(buttons[:6]):
            name = await b.get_attribute("name")
            visible = await b.is_visible()
            print(f"  [{i}] name={name} visible={visible}")

        # Click index 0, note the notice ID, go back, click index 1, note
        # the notice ID — they should be DIFFERENT notices now.
        ids = []
        for idx in (0, 1):
            buttons = await page.query_selector_all(SEL_VIEW_BUTTON_PATTERN)
            await buttons[idx].click()
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(1)
            print(f"\nClicked index {idx} -> URL: {page.url}")
            ids.append(page.url)
            await page.go_back()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)

        print("\nDistinct notices reached:", len(set(ids)) == 2)
        print("IDs:", ids)

        await browser.close()


asyncio.run(main())
