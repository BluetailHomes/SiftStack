"""Logged into newmexico.column.us successfully — now explore the /notices
area: what's visible, whether Bernalillo/Sandoval + our notice types
(foreclosure, probate, tax sale, etc.) are covered, and what API calls
fetch the actual notice data (as opposed to auth/analytics noise).

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_column_notices_explore.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config as cfg  # noqa: F401 — triggers load_dotenv()

COLUMN_EMAIL = os.getenv("COLUMNUS_EMAIL", "")
COLUMN_PASSWORD = os.getenv("COLUMNUS_PASSWORD", "")
COLUMN_BASE = "https://newmexico.column.us"
OUT_DIR = os.path.join(os.path.dirname(__file__), "diag_output")

NOISE_HOSTS = (
    "launchdarkly.com", "datadoghq.com", "doubleclick.net", "google.com",
    "analytics.google.com", "googletagmanager.com", "stripe.com",
    "bugsnag.com", "frontapp.com", "googleadservices.com",
    "identitytoolkit.googleapis.com", "securetoken.googleapis.com",
)


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    from playwright.async_api import async_playwright

    api_calls = []  # (method, url, response_status, response_snippet)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        responses_by_url = {}

        async def capture_response(resp):
            if resp.request.resource_type in ("xhr", "fetch") and not any(h in resp.url for h in NOISE_HOSTS):
                try:
                    body = await resp.json()
                    snippet = json.dumps(body)[:500]
                except Exception:
                    snippet = "<non-json>"
                responses_by_url[resp.url] = {"status": resp.status, "snippet": snippet, "method": resp.request.method}

        page.on("response", lambda r: asyncio.create_task(capture_response(r)))

        await page.goto(f"{COLUMN_BASE}/login", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        email_input = page.locator("input[type='email'], input[name='email']").first
        pw_input = page.locator("input[type='password']").first
        await email_input.click()
        await email_input.fill(COLUMN_EMAIL)
        await asyncio.sleep(0.3)
        await pw_input.click()
        await pw_input.fill(COLUMN_PASSWORD)
        await asyncio.sleep(0.3)
        submit_btn = page.locator("button:has-text('Log In'), button[type='submit']").first
        await submit_btn.click()
        await asyncio.sleep(5)

        print(f"Landed on: {page.url}")
        await page.screenshot(path=os.path.join(OUT_DIR, "column_notices_01.png"), full_page=True)

        body_text = await page.locator("body").inner_text()
        print("\n--- /notices page text (first 3000 chars) ---")
        print(body_text[:3000])

        html = await page.content()
        with open(os.path.join(OUT_DIR, "column_notices.html"), "w", encoding="utf-8") as f:
            f.write(html)

        await asyncio.sleep(3)  # let any lazy data calls settle
        await browser.close()

    print(f"\n--- {len(responses_by_url)} unique non-noise API responses captured ---")
    for url, info in responses_by_url.items():
        print(f"\n{info['method']} {url}")
        print(f"  status={info['status']}")
        print(f"  body: {info['snippet']}")

    with open(os.path.join(OUT_DIR, "column_notices_api.json"), "w", encoding="utf-8") as f:
        json.dump(responses_by_url, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
