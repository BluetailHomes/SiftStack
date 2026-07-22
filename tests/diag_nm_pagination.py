"""Root-cause the NM per-page persistence bug: page 1 correctly shows 50
results after setting the per-page dropdown, but page 2+ only shows 10.
Log in, select 'probate', set per-page=50, then navigate to page 2 and
directly inspect the per-page dropdown's actual value + row count, instead
of guessing.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_nm_pagination.py
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

        await page.select_option(cfg.SEL_SAVED_SEARCHES_DROPDOWN, label="probate")
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(3)
        # Extra settle — confirmed live 2026-07-22 that a second delayed
        # navigation can still be in flight here even after networkidle.
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(3)
        print(f"After selecting probate: {page.url}")

        async def _retry_query(selector, tries=6, wait=2.0):
            for i in range(tries):
                try:
                    return await page.query_selector(selector)
                except Exception as e:
                    print(f"  query_selector retry {i+1}/{tries} after: {e}")
                    await asyncio.sleep(wait)
            return None

        # Set per-page to 50
        dropdown = await _retry_query(cfg.SEL_PER_PAGE_DROPDOWN)
        if dropdown:
            val = await dropdown.input_value()
            print(f"Per-page dropdown initial value: {val}")
            await page.select_option(cfg.SEL_PER_PAGE_DROPDOWN, "50")
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)
            val2 = await dropdown.input_value()
            print(f"Per-page dropdown after setting to 50: {val2}")
        else:
            print("Per-page dropdown NOT FOUND")

        # Count actual view buttons on page 1
        buttons1 = await page.query_selector_all(cfg.SEL_VIEW_BUTTON_PATTERN)
        print(f"Page 1: {len(buttons1)} view buttons found")

        # Dump the per-page dropdown's full HTML for inspection
        pp_html = await page.evaluate(f"""() => {{
            const el = document.querySelector('{cfg.SEL_PER_PAGE_DROPDOWN}');
            return el ? el.outerHTML : null;
        }}""")
        print(f"\nPer-page dropdown HTML (page 1):\n{pp_html}\n")

        # Now click Next page
        next_btn = await page.query_selector(cfg.SEL_NEXT_PAGE_BUTTON)
        if next_btn:
            await next_btn.click()
            await page.wait_for_load_state("load")
            await asyncio.sleep(3)
            print(f"After Next click: {page.url}")

            dropdown2 = await page.query_selector(cfg.SEL_PER_PAGE_DROPDOWN)
            val3 = await dropdown2.input_value() if dropdown2 else "NOT FOUND"
            print(f"Per-page dropdown value on page 2: {val3}")

            buttons2 = await page.query_selector_all(cfg.SEL_VIEW_BUTTON_PATTERN)
            print(f"Page 2: {len(buttons2)} view buttons found")

            pp_html2 = await page.evaluate(f"""() => {{
                const el = document.querySelector('{cfg.SEL_PER_PAGE_DROPDOWN}');
                return el ? el.outerHTML : null;
            }}""")
            print(f"\nPer-page dropdown HTML (page 2):\n{pp_html2}\n")

            info_el = await page.query_selector(cfg.SEL_PAGE_INFO)
            info_text = await info_el.inner_text() if info_el else "NOT FOUND"
            print(f"Page info text: {info_text!r}")
        else:
            print("Next button not found")

        await page.screenshot(path=os.path.join(OUT_DIR, "nm_pagination_page2.png"), full_page=True)
        html = await page.content()
        with open(os.path.join(OUT_DIR, "nm_pagination_page2.html"), "w", encoding="utf-8") as f:
            f.write(html)

        await asyncio.sleep(10)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
