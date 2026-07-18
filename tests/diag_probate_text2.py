"""One-off diagnostic: capture raw text from specific KNOWN Jackson County
MO probate notice IDs directly (bypassing search-result ordering, which
turned out to include wrong-county false positives that are already
filtered out in production by is_target_county() but got in the way of
this diagnostic).

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_probate_text2.py
"""

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from playwright.async_api import async_playwright

from config import (
    BASE_URL,
    LOGIN_URL,
    SEL_LOGIN_EMAIL,
    SEL_LOGIN_PASSWORD,
    SEL_LOGIN_SUBMIT,
    NOTICE_SITE_EMAIL,
    NOTICE_SITE_PASSWORD,
)
from captcha_solver import solve_captcha_and_view
from notice_parser import parse_notice_page, is_target_county


def _session_base(page_url: str) -> str:
    m = re.search(r"(https?://[^/]+/\(S\([^)]+\)\)/)", page_url)
    return m.group(1) if m else BASE_URL + "/"

# Known Jackson County MO probate notice IDs from earlier successful scrapes
# (recorded in seen_ids.json from prior runs this session).
NOTICE_IDS = ["1236715", "1236711", "1236707", "1236703", "1236699"]


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
        print("Logged in, landed on:", page.url)

        for notice_id in NOTICE_IDS:
            url = f"{_session_base(page.url)}Details.aspx?ID={notice_id}"
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(1)
            print(f"\nNavigated to {url} -> actual url: {page.url}")

            content_visible = await page.query_selector("text='Notice Content'")
            if not content_visible:
                solved = await solve_captcha_and_view(page)
                if not solved:
                    print(f"=== NOTICE {notice_id} — CAPTCHA SOLVE FAILED, skipping ===")
                    continue

            notice = await parse_notice_page(page, "Jackson", "probate", None)
            in_county = is_target_county(notice.raw_text, "Jackson")

            print(f"{'=' * 70}")
            print(f"=== NOTICE {notice_id} (is_target_county=Jackson: {in_county}) ===")
            print(f"{'=' * 70}")
            print(f"decedent_name : {notice.decedent_name!r}")
            print(f"owner_name    : {notice.owner_name!r}   <- PR name field")
            print(f"owner_street  : {notice.owner_street!r}")
            print(f"owner_city    : {notice.owner_city!r}")
            print(f"owner_state   : {notice.owner_state!r}")
            print(f"owner_zip     : {notice.owner_zip!r}")
            print("--- RAW TEXT (first 3000 chars) ---")
            print(notice.raw_text[:3000])

        await browser.close()


asyncio.run(main())
