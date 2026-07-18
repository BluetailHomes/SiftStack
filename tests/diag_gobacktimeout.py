"""One-off diagnostic: figure out why clicking into the SECOND search result
after a go_back() from the first never reaches Details.aspx (times out
waiting for domcontentloaded, screenshot shows we're still on Search.aspx).

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_gobacktimeout.py
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

requests_log = []
console_log = []


def _on_request(req):
    requests_log.append(f"REQUEST  {req.method} {req.url}")


def _on_response(resp):
    requests_log.append(f"RESPONSE {resp.status} {resp.url}")


def _on_console(msg):
    console_log.append(f"[{msg.type}] {msg.text}")


def _on_pageerror(err):
    console_log.append(f"[pageerror] {err}")


async def dump_state(page, label):
    print(f"\n=== STATE: {label} ===")
    print("URL:", page.url)
    buttons = await page.query_selector_all(SEL_VIEW_BUTTON_PATTERN)
    print("View buttons found:", len(buttons))
    for i, b in enumerate(buttons[:3]):
        try:
            visible = await b.is_visible()
            enabled = await b.is_enabled()
            name = await b.get_attribute("name")
            print(f"  [{i}] name={name} visible={visible} enabled={enabled}")
        except Exception as e:
            print(f"  [{i}] ERROR reading button state: {e}")
    # ASP.NET postback state fields
    viewstate = await page.evaluate(
        """() => {
            const el = document.querySelector('input[name="__VIEWSTATE"]');
            return el ? el.value.length : null;
        }"""
    )
    eventvalidation = await page.evaluate(
        """() => {
            const el = document.querySelector('input[name="__EVENTVALIDATION"]');
            return el ? el.value.length : null;
        }"""
    )
    print(f"__VIEWSTATE length: {viewstate}, __EVENTVALIDATION length: {eventvalidation}")
    await page.screenshot(path=f"tests/diag_gobacktimeout_{label}.png")
    print(f"Screenshot saved: tests/diag_gobacktimeout_{label}.png")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        context.set_default_timeout(60_000)
        page = await context.new_page()
        page.on("request", _on_request)
        page.on("response", _on_response)
        page.on("console", _on_console)
        page.on("pageerror", _on_pageerror)

        await page.goto(LOGIN_URL)
        await page.wait_for_load_state("networkidle")
        await page.fill(SEL_LOGIN_EMAIL, NOTICE_SITE_EMAIL)
        await page.fill(SEL_LOGIN_PASSWORD, NOTICE_SITE_PASSWORD)
        await page.click(SEL_LOGIN_SUBMIT)
        await page.wait_for_load_state("networkidle")

        async with page.expect_navigation(wait_until="networkidle", timeout=30000):
            await page.select_option(SEL_SAVED_SEARCHES_DROPDOWN, label="Jackson County Probate")

        await dump_state(page, "01_search_results")

        # ── Click result 1 (index 0) ──
        buttons = await page.query_selector_all(SEL_VIEW_BUTTON_PATTERN)
        requests_log.clear()
        await buttons[0].click()
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(2)
        await dump_state(page, "02_after_click_result1")
        print("\n--- Network activity during click 1 (last 15) ---")
        for line in requests_log[-15:]:
            print(line)

        # ── Go back ──
        requests_log.clear()
        console_log.clear()
        await page.go_back()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)
        await dump_state(page, "03_after_go_back")
        print("\n--- Network activity during go_back (last 15) ---")
        for line in requests_log[-15:]:
            print(line)

        # ── Click result 2 (index 1) — the one that hangs ──
        requests_log.clear()
        console_log.clear()
        buttons = await page.query_selector_all(SEL_VIEW_BUTTON_PATTERN)
        print(f"\nAbout to click button index 1 of {len(buttons)} found")
        target = buttons[1]
        try:
            bbox = await target.bounding_box()
            print("Target bounding box:", bbox)
        except Exception as e:
            print("Could not get bounding box:", e)

        click_error = None
        try:
            await target.click(timeout=10_000)
            print("Click call returned normally")
        except Exception as e:
            click_error = str(e)
            print("Click call raised:", e)

        # Don't wait the full 60s — poll for up to 15s and report exactly
        # what's happening instead of hanging like the real scraper does.
        for i in range(15):
            await asyncio.sleep(1)
            print(f"  t+{i+1}s url={page.url}")

        await dump_state(page, "04_after_click_result2")
        print("\n--- Network activity after click 2 (all) ---")
        for line in requests_log:
            print(line)
        print("\n--- Console/page errors after click 2 ---")
        for line in console_log:
            print(line)
        if click_error:
            print("\nClick error was:", click_error)

        await browser.close()


asyncio.run(main())
