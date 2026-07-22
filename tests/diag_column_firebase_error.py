"""Capture the actual Firebase Auth error response from
identitytoolkit.googleapis.com when logging into newmexico.column.us, to
get a precise reason (EMAIL_NOT_FOUND / INVALID_PASSWORD / USER_DISABLED /
etc.) instead of guessing from a generic UI message.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_column_firebase_error.py
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


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        signin_responses = []
        page.on("response", lambda r: signin_responses.append(r)
                if "identitytoolkit.googleapis.com" in r.url and "signInWithPassword" in r.url else None)

        await page.goto(f"{COLUMN_BASE}/login", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        print(f"COLUMN_EMAIL len={len(COLUMN_EMAIL)}, COLUMN_PASSWORD len={len(COLUMN_PASSWORD)}")

        email_input = page.locator("input[type='email'], input[name='email']").first
        pw_input = page.locator("input[type='password']").first
        await email_input.click()
        await email_input.fill(COLUMN_EMAIL)
        await asyncio.sleep(0.3)
        actual_email = await email_input.input_value()
        print(f"Email field after fill: {actual_email!r}")

        await pw_input.click()
        await pw_input.fill(COLUMN_PASSWORD)
        await asyncio.sleep(0.3)
        actual_pw = await pw_input.input_value()
        print(f"Password field after fill: len={len(actual_pw)}, matches={actual_pw == COLUMN_PASSWORD}")

        submit_btn = page.locator("button:has-text('Log In'), button[type='submit']").first
        await submit_btn.click()
        await asyncio.sleep(4)

        captured = {}
        if signin_responses:
            resp = signin_responses[-1]
            try:
                captured["signin_status"] = resp.status
                captured["signin_body"] = await resp.json()
            except Exception as e:
                captured["signin_body"] = f"<could not parse: {e}>"

        print(f"Final URL: {page.url}")

        # Also check for a visible on-page error message
        body_text = await page.locator("body").inner_text()
        import re
        error_lines = [l for l in body_text.split("\n") if l.strip() and
                       re.search(r"invalid|error|incorrect|not found|disabled|wrong", l, re.IGNORECASE)]
        print(f"Visible error-ish lines on page: {error_lines}")

        print(f"\nFirebase signIn response status: {captured.get('signin_status', 'NOT CAPTURED')}")
        print(f"Firebase signIn response body: {json.dumps(captured.get('signin_body', {}), indent=2)}")

        await page.screenshot(path=os.path.join(OUT_DIR, "column_firebase_error.png"), full_page=True)

        await asyncio.sleep(5)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
