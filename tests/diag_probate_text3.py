"""One-off diagnostic: capture raw text from several real Jackson County MO
probate notices via the actual click-through flow (search results -> View
button -> Turnstile solve), same as production scraper.py — direct
page.goto() to Details.aspx turned out NOT to trigger the site's embedded
PDF-viewer rendering, so this uses the real navigation path instead.

Skips (but logs) any result that fails the is_target_county check — the
saved search returns occasional wrong-county false positives (confirmed:
a Benton County foreclosure appeared under "Jackson County Probate"),
which is exactly what that filter exists to catch in production.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_probate_text3.py [target_good_count]
"""

import asyncio
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Force UTF-8 stdout — PDF-extracted notice text can contain ligatures
# (e.g. U+FB01 "fi") that Windows' default cp1252 console can't encode,
# which previously crashed print() before the raw text ever got written.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright

from config import (
    LOGIN_URL,
    SEL_LOGIN_EMAIL,
    SEL_LOGIN_PASSWORD,
    SEL_LOGIN_SUBMIT,
    SEL_PER_PAGE_DROPDOWN,
    SEL_SAVED_SEARCHES_DROPDOWN,
    SEL_VIEW_BUTTON_PATTERN,
    NOTICE_SITE_EMAIL,
    NOTICE_SITE_PASSWORD,
)
from captcha_solver import solve_captcha_and_view
from notice_parser import parse_notice_page, is_target_county


async def main():
    target_good = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    max_attempts = target_good + 6  # budget for a few wrong-county misses

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

        good_count = 0
        idx = 0
        while good_count < target_good and idx < max_attempts:
            buttons = await page.query_selector_all(SEL_VIEW_BUTTON_PATTERN)
            if idx >= len(buttons):
                print(f"Ran out of buttons at idx={idx} ({len(buttons)} available) — stopping")
                break

            try:
                await buttons[idx].click()
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(1)

                content_visible = await page.query_selector("text='Notice Content'")
                if not content_visible:
                    solved = await solve_captcha_and_view(page)
                    if not solved:
                        print(f"\n[{idx}] CAPTCHA SOLVE FAILED — skipping")
                        idx += 1
                        await page.go_back()
                        await page.wait_for_load_state("networkidle")
                        await asyncio.sleep(1)
                        continue

                notice = await parse_notice_page(page, "Jackson", "probate", None)
                in_county = is_target_county(notice.raw_text, "Jackson")

                if not in_county or "Estate" not in notice.raw_text:
                    print(f"\n[{idx}] SKIPPING — is_target_county=Jackson:{in_county}, "
                          f"looks-like-probate:{'Estate' in notice.raw_text} "
                          f"(first 150 chars: {notice.raw_text[:150]!r})")
                else:
                    good_count += 1
                    print(f"\n{'=' * 70}")
                    print(f"=== GOOD SAMPLE {good_count} (idx={idx}, url={page.url}) ===")
                    print(f"{'=' * 70}")
                    print(f"decedent_name : {notice.decedent_name!r}")
                    print(f"owner_name    : {notice.owner_name!r}   <- PR name field")
                    print(f"owner_street  : {notice.owner_street!r}")
                    print(f"owner_city    : {notice.owner_city!r}")
                    print(f"owner_state   : {notice.owner_state!r}")
                    print(f"owner_zip     : {notice.owner_zip!r}")
                    print("--- RAW TEXT (first 3000 chars) ---")
                    print(notice.raw_text[:3000])

                await page.go_back()
                await page.wait_for_load_state("networkidle")
                # Matches scraper.py's production handling: after solving a
                # CAPTCHA, an extra history entry (pre-solve Details page) can
                # sit between the content-revealed page and Search.aspx.
                if "details" in page.url.lower():
                    await page.go_back()
                    await page.wait_for_load_state("networkidle")
                await asyncio.sleep(1)

            except Exception as e:
                print(f"\n[{idx}] ERROR: {e} — attempting recovery")
                try:
                    await page.go_back()
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(1)
                except Exception:
                    pass

            idx += 1

        print(f"\n\nDone — collected {good_count}/{target_good} good samples in {idx} attempts")
        await browser.close()


asyncio.run(main())
