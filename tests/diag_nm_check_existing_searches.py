"""Check what county/filter criteria the existing "probate" and "foreclosure"
saved searches on this newmexicopublicnotices.com account actually have
applied — need to know if they're already scoped to specific counties or
are broad ("Any" county) before deciding whether to reuse or create new
county-specific saved searches for Bernalillo/Sandoval.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_nm_check_existing_searches.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config as cfg

NM_LOGIN_URL = "https://www.newmexicopublicnotices.com/authenticate.aspx"
OUT_DIR = os.path.join(os.path.dirname(__file__), "diag_output")


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(NM_LOGIN_URL, wait_until="networkidle")
        email_el = page.locator(cfg.SEL_LOGIN_EMAIL)
        pw_el = page.locator(cfg.SEL_LOGIN_PASSWORD)
        await email_el.click()
        await email_el.fill(cfg.NOTICE_SITE_EMAIL)
        await asyncio.sleep(0.3)
        await pw_el.click()
        await pw_el.fill(cfg.NOTICE_SITE_PASSWORD)
        await asyncio.sleep(0.3)
        await page.locator(cfg.SEL_LOGIN_SUBMIT).click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)

        for search_name in ["probate", "foreclosure"]:
            print(f"\n{'='*60}\nSelecting saved search: {search_name!r}")
            await page.select_option(cfg.SEL_SAVED_SEARCHES_DROPDOWN, label=search_name)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(3)
            print(f"URL after selecting: {page.url}")

            # Postback timing can destroy the JS execution context right as
            # we read it — retry once after a further short wait if so.
            async def _read_state():
                county_text = await page.evaluate("""() => {
                    const el = document.querySelector('[id*="divCounty"]');
                    return el ? el.textContent.replace(/\\s+/g, ' ').trim().substring(0, 200) : null;
                }""")
                keyword_val = await page.evaluate("""() => {
                    const el = document.querySelector('[id*="txtSearch"]');
                    return el ? el.value : null;
                }""")
                checked_counties = await page.evaluate("""() => {
                    const boxes = document.querySelectorAll('[id*="lstCounty"][type="checkbox"]:checked');
                    return Array.from(boxes).map(b => {
                        const li = b.closest('li');
                        return li ? li.textContent.trim() : b.id;
                    });
                }""")
                return county_text, keyword_val, checked_counties

            try:
                county_text, keyword_val, checked_counties = await _read_state()
            except Exception as e:
                print(f"  (retrying read after: {e})")
                await asyncio.sleep(3)
                county_text, keyword_val, checked_counties = await _read_state()

            print(f"  County filter text: {county_text}")
            print(f"  Search keywords value: {keyword_val!r}")
            print(f"  Checked county checkboxes: {checked_counties}")

            await page.screenshot(
                path=os.path.join(OUT_DIR, f"nm_existing_search_{search_name}.png"), full_page=True
            )

        await asyncio.sleep(5)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
