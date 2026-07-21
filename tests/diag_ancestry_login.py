"""One-off diagnostic: step through Ancestry login manually with screenshots
at each stage, to debug why _auto_login() is failing.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_ancestry_login.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config as cfg
import ancestry_enricher

OUT_DIR = os.path.join(os.path.dirname(__file__), "diag_output")


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    from playwright.async_api import async_playwright

    ancestry_enricher.PROFILE_DIR.mkdir(exist_ok=True)
    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        str(ancestry_enricher.PROFILE_DIR),
        headless=False,
        viewport={"width": 1920, "height": 1080},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = context.pages[0] if context.pages else await context.new_page()

    try:
        print(f"Email configured: {bool(cfg.ANCESTRY_EMAIL)}")
        print(f"Password configured: {bool(cfg.ANCESTRY_PASSWORD)}")

        await page.goto(ancestry_enricher.SIGNIN_URL, wait_until="domcontentloaded")
        await ancestry_enricher._delay(2, 3)
        print(f"After goto signin: {page.url}")
        await page.screenshot(path=os.path.join(OUT_DIR, "login_01_signin_page.png"), full_page=True)

        # Try each email selector and report which one hits
        email_sel_used = None
        for sel in ["input[name='username']", "input[type='email']", "#username"]:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                print(f"Email field found via: {sel}")
                email_sel_used = sel
                await el.click()
                await ancestry_enricher._delay(0.3, 0.6)
                await el.fill(cfg.ANCESTRY_EMAIL)
                break
        if not email_sel_used:
            print("NO EMAIL FIELD FOUND with known selectors")

        await page.screenshot(path=os.path.join(OUT_DIR, "login_02_after_email.png"), full_page=True)

        pw_sel_used = None
        for sel in ["input[name='password']", "input[type='password']", "#password"]:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                print(f"Password field found via: {sel}")
                pw_sel_used = sel
                await el.click()
                await ancestry_enricher._delay(0.3, 0.6)
                await el.fill(cfg.ANCESTRY_PASSWORD)
                break
        if not pw_sel_used:
            print("NO PASSWORD FIELD FOUND with known selectors (may require clicking Next/Continue first)")

        await page.screenshot(path=os.path.join(OUT_DIR, "login_03_after_password.png"), full_page=True)

        btn_used = None
        for sel in ["button[type='submit']", "input[type='submit']"]:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                print(f"Submit button found via: {sel}")
                btn_used = sel
                await btn.click()
                break
        if not btn_used:
            print("NO SUBMIT BUTTON FOUND with known selectors")

        await ancestry_enricher._delay(3, 5)
        print(f"After submit: {page.url}")
        title = await page.title()
        print(f"Title: {title}")
        await page.screenshot(path=os.path.join(OUT_DIR, "login_04_after_submit.png"), full_page=True)

        # Dump any visible error/alert text
        error_text = await page.evaluate("""() => {
            const sels = ['[role="alert"]', '.alert', '[class*="error"]', '[class*="Error"]'];
            const out = [];
            for (const s of sels) {
                document.querySelectorAll(s).forEach(el => {
                    const t = (el.textContent || '').trim();
                    if (t) out.push(s + ': ' + t);
                });
            }
            return out;
        }""")
        print("Error/alert elements found on page:")
        for e in error_text:
            print(f"  {e}")

        html_path = os.path.join(OUT_DIR, "login_final.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(await page.content())
        print(f"Saved final HTML: {html_path}")

        print("\nKeeping browser open 20s for manual inspection...")
        await asyncio.sleep(20)

    finally:
        await context.close()
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
