"""Log into newmexico.column.us and capture api.column.us traffic while
navigating to notice search, to find the real data API (as opposed to the
analytics/tracking noise from LaunchDarkly/Datadog/Google/Stripe/Front).

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_column_us_login.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config as cfg

COLUMN_BASE = "https://newmexico.column.us"
OUT_DIR = os.path.join(os.path.dirname(__file__), "diag_output")

# Hosts that are pure analytics/tracking noise — filtered out of the report
NOISE_HOSTS = (
    "launchdarkly.com", "datadoghq.com", "doubleclick.net", "google.com",
    "analytics.google.com", "googletagmanager.com", "stripe.com",
    "bugsnag.com", "frontapp.com", "googleadservices.com",
)


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    from playwright.async_api import async_playwright

    api_calls = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        def on_request(req):
            if req.resource_type in ("xhr", "fetch") and not any(h in req.url for h in NOISE_HOSTS):
                api_calls.append({"method": req.method, "url": req.url, "phase": "pre-login"})

        page.on("request", on_request)

        print(f"Email configured: {bool(cfg.NOTICE_SITE_EMAIL)}")
        await page.goto(f"{COLUMN_BASE}/login", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Fill login form
        email_input = page.locator("input[type='email'], input[name='email']").first
        pw_input = page.locator("input[type='password']").first
        await email_input.fill(cfg.NOTICE_SITE_EMAIL)
        await pw_input.fill(cfg.NOTICE_SITE_PASSWORD)
        await page.screenshot(path=os.path.join(OUT_DIR, "column_02_login_filled.png"), full_page=True)

        submit_btn = page.locator("button:has-text('Log In'), button[type='submit']").first
        await submit_btn.click()
        await asyncio.sleep(5)

        print(f"After login: {page.url}")
        await page.screenshot(path=os.path.join(OUT_DIR, "column_03_after_login.png"), full_page=True)

        body_text = await page.locator("body").inner_text()
        print("\n--- Post-login page text (first 1500 chars) ---")
        print(body_text[:1500])

        # Mark the phase boundary so we can tell pre/post-login apart
        for call in api_calls:
            pass
        post_login_start = len(api_calls)

        # Try to find a notices/search area to trigger data-fetch API calls
        for link_text in ["Notices", "Search", "Public Notices", "Browse", "My Notices"]:
            el = page.locator(f"text='{link_text}'").first
            if await el.count():
                print(f"Clicking nav link: {link_text!r}")
                try:
                    await el.click(timeout=5000)
                    await asyncio.sleep(3)
                    break
                except Exception as e:
                    print(f"  click failed: {e}")

        print(f"\nCurrent URL after nav attempt: {page.url}")
        await page.screenshot(path=os.path.join(OUT_DIR, "column_04_after_nav.png"), full_page=True)

        await asyncio.sleep(3)
        await browser.close()

    print(f"\n--- {len(api_calls)} non-noise XHR/fetch calls captured ---")
    for c in api_calls:
        print(f"  {c['method']} {c['url']}")

    with open(os.path.join(OUT_DIR, "column_api_calls.json"), "w", encoding="utf-8") as f:
        json.dump(api_calls, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
