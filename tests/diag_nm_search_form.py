"""Explore the Smart Search form's category/type filter mechanism on
newmexicopublicnotices.com, so the Bernalillo/Sandoval saved searches can
be created matching the "probate" filtering the MO saved searches use
(e.g. "Jackson County Probate").

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_nm_search_form.py
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
        print(f"Landed on: {page.url}")

        # Dump "Popular Searches" dropdown options
        popular = await page.evaluate("""() => {
            const sel = document.querySelector('select[id*="Popular"], select[id*="popular"]');
            if (!sel) return null;
            return Array.from(sel.options).map(o => ({value: o.value, text: o.textContent.trim()}));
        }""")
        print(f"\nPopular Searches dropdown options: {popular}")

        # Dump the saved-searches dropdown too (existing ones, if any visible)
        saved = await page.evaluate("""() => {
            const sel = document.querySelector('select[id*="Saved"], select[id*="saved"]');
            if (!sel) return null;
            return Array.from(sel.options).map(o => ({value: o.value, text: o.textContent.trim()}));
        }""")
        print(f"\nSaved Searches dropdown options: {saved}")

        # Look for any Category/Type filter beyond County/City/Publication/Date
        filter_groups = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('.filter_group, [id*="divCounty"], [id*="div"]'))
                .map(el => ({id: el.id, cls: el.className, text: (el.textContent||'').trim().substring(0,80)}))
                .filter(x => x.id);
        }""")
        print(f"\nFilter group divs ({len(filter_groups)}):")
        for f in filter_groups[:20]:
            print(f"  {f}")

        # Screenshot full sidebar
        await page.screenshot(path=os.path.join(OUT_DIR, "nm_search_form_full.png"), full_page=True)

        html = await page.content()
        with open(os.path.join(OUT_DIR, "nm_search_form.html"), "w", encoding="utf-8") as f:
            f.write(html)

        await asyncio.sleep(10)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
