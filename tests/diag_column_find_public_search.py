"""Check whether newmexico.column.us has a PUBLIC notice search (browse all
published notices, like newmexicopublicnotices.com's anonymous search)
separate from the logged-in account's "my notices" dashboard, which showed
0 results because this account has never placed a notice itself.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_column_find_public_search.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config as cfg  # noqa: F401

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
        await page.locator("button:has-text('Log In'), button[type='submit']").first.click()
        await asyncio.sleep(5)

        # Dump ALL nav links in the app shell
        links = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                text: (a.textContent || '').trim(), href: a.getAttribute('href'),
            })).filter(l => l.text || l.href);
        }""")
        print(f"Found {len(links)} links in authenticated app shell:")
        for l in links:
            print(f"  {l}")

        # Try common public-search paths directly
        for path in ["/search", "/public", "/public-notices", "/browse", "/notices/search"]:
            try:
                resp = await page.goto(f"{COLUMN_BASE}{path}", wait_until="domcontentloaded", timeout=10000)
                print(f"\n{path} -> status {resp.status if resp else '?'}, url={page.url}")
                text = (await page.locator("body").inner_text())[:300]
                print(f"  text: {text}")
            except Exception as e:
                print(f"\n{path} -> ERROR {e}")

        await page.screenshot(path=os.path.join(OUT_DIR, "column_shell.png"), full_page=True)
        await asyncio.sleep(5)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
