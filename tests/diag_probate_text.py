"""One-off diagnostic: capture raw notice text + current regex extraction
results from several different Jackson County MO probate notices, to see
the actual PR-naming wording MO uses before touching PROBATE_NAME_RE /
PR_ADDRESS_RE (which were built against Knox/TN wording).

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_probate_text.py [N]
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
from captcha_solver import solve_captcha_and_view
from notice_parser import parse_notice_page


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5

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
        await asyncio.sleep(2)

        # Verify we're actually looking at what we think we're looking at
        # before trusting any of the results below.
        header_text = await page.evaluate(
            """() => {
                const body = document.body.innerText;
                const idx = body.indexOf('Advanced Search');
                return idx === -1 ? 'HEADER NOT FOUND' : body.slice(idx, idx + 300);
            }"""
        )
        print("=== SEARCH HEADER (sanity check) ===")
        print(header_text)
        print(f"Page URL: {page.url}")

        buttons = await page.query_selector_all(SEL_VIEW_BUTTON_PATTERN)
        print(f"Buttons found on results page: {len(buttons)}\n")

        for idx in range(n):
            buttons = await page.query_selector_all(SEL_VIEW_BUTTON_PATTERN)
            if idx >= len(buttons):
                print(f"Only {len(buttons)} buttons on this page — stopping")
                break

            await buttons[idx].click()
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(1)

            content_visible = await page.query_selector("text='Notice Content'")
            if not content_visible:
                solved = await solve_captcha_and_view(page)
                if not solved:
                    print(f"\n=== NOTICE {idx} — CAPTCHA SOLVE FAILED, skipping ===")
                    await page.go_back()
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(1)
                    continue

            notice = await parse_notice_page(page, "Jackson", "probate", None)

            print(f"\n{'=' * 70}")
            print(f"=== NOTICE {idx}  (url={page.url}) ===")
            print(f"{'=' * 70}")
            print(f"decedent_name : {notice.decedent_name!r}")
            print(f"owner_name    : {notice.owner_name!r}   <- this is the PR name field")
            print(f"owner_street  : {notice.owner_street!r}")
            print(f"owner_city    : {notice.owner_city!r}")
            print(f"owner_state   : {notice.owner_state!r}")
            print(f"owner_zip     : {notice.owner_zip!r}")
            print("--- RAW TEXT (first 2500 chars) ---")
            print(notice.raw_text[:2500])

            await page.go_back()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)
            post_back_buttons = await page.query_selector_all(SEL_VIEW_BUTTON_PATTERN)
            print(f"[after go_back] url={page.url} buttons={len(post_back_buttons)}")

        await browser.close()


asyncio.run(main())
