"""Inspect the real attributes of the Column.us login form's email/password
inputs — a prior attempt's selector matched the wrong (or no) element for
email, since it's a React SPA that may not use standard type='email'/
name='email' attributes.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_column_inspect_form.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

COLUMN_BASE = "https://newmexico.column.us"


async def main():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(f"{COLUMN_BASE}/login", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        inputs = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('input')).map(el => ({
                type: el.type, name: el.name, id: el.id,
                placeholder: el.placeholder, ariaLabel: el.getAttribute('aria-label'),
                cls: el.className,
            }));
        }""")
        print(f"Found {len(inputs)} input elements:")
        for i in inputs:
            print(f"  {i}")

        await asyncio.sleep(5)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
