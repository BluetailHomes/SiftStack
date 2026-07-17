"""One-off diagnostic: dump the real DOM structure of the Turnstile challenge
on a known-failing mopublicnotices.com notice detail page.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_turnstile.py
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


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        context.set_default_timeout(60_000)
        page = await context.new_page()

        await page.goto(LOGIN_URL)
        await page.wait_for_load_state("networkidle")
        await page.fill(SEL_LOGIN_EMAIL, NOTICE_SITE_EMAIL)
        await page.fill(SEL_LOGIN_PASSWORD, NOTICE_SITE_PASSWORD)
        await page.click(SEL_LOGIN_SUBMIT)
        await page.wait_for_load_state("networkidle")
        print("Login landed on:", page.url)

        async with page.expect_navigation(wait_until="networkidle", timeout=30000):
            await page.select_option(SEL_SAVED_SEARCHES_DROPDOWN, label="Jackson County Probate")
        print("Search landed on:", page.url)

        btn = (await page.query_selector_all(SEL_VIEW_BUTTON_PATTERN))[0]
        await btn.click()
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(5)  # let any widget JS run
        print("Detail page:", page.url)

        # Dump every iframe in the main document
        iframes = await page.eval_on_selector_all(
            "iframe",
            "els => els.map(e => ({src: e.getAttribute('src'), id: e.id, cls: e.className}))",
        )
        print("\n=== IFRAMES (main document) ===")
        for f in iframes:
            print(f)

        # Recursively walk shadow roots looking for iframes/turnstile markers
        shadow_dump = await page.evaluate(
            """() => {
                const results = [];
                function walk(root, path) {
                    const all = root.querySelectorAll('*');
                    all.forEach(el => {
                        if (el.shadowRoot) {
                            results.push({path: path + ' > ' + el.tagName + (el.className ? '.' + el.className : ''), hasShadow: true});
                            walk(el.shadowRoot, path + ' > ' + el.tagName + '::shadow');
                        }
                        if (el.tagName === 'IFRAME') {
                            results.push({path: path + ' > iframe', src: el.getAttribute('src'), id: el.id});
                        }
                    });
                }
                walk(document, 'document');
                return results;
            }"""
        )
        print("\n=== SHADOW ROOTS + NESTED IFRAMES ===")
        for s in shadow_dump:
            print(s)

        # Any element whose class/id mentions turnstile/captcha/cloudflare
        markers = await page.evaluate(
            """() => {
                const all = document.querySelectorAll('*');
                const hits = [];
                all.forEach(el => {
                    const idc = (el.id || '') + ' ' + (el.className || '');
                    if (/turnstile|captcha|cloudflare|cf-/i.test(idc)) {
                        hits.push({tag: el.tagName, id: el.id, cls: (typeof el.className === 'string' ? el.className : '')});
                    }
                });
                return hits;
            }"""
        )
        print("\n=== ELEMENTS MENTIONING turnstile/captcha/cloudflare/cf- ===")
        for m in markers:
            print(m)

        # Any <script src> mentioning cloudflare/turnstile
        scripts = await page.eval_on_selector_all(
            "script[src]",
            "els => els.map(e => e.getAttribute('src')).filter(s => /cloudflare|turnstile/i.test(s || ''))",
        )
        print("\n=== SCRIPT TAGS mentioning cloudflare/turnstile ===")
        for s in scripts:
            print(s)

        await page.screenshot(path="tests/diag_turnstile_screenshot.png", full_page=True)
        print("\nScreenshot saved: tests/diag_turnstile_screenshot.png")

        # Dump raw HTML around any 'Verify you are human' text for full context
        html_snippet = await page.evaluate(
            """() => {
                const body = document.body.innerHTML;
                const idx = body.indexOf('Verify you are human');
                if (idx === -1) return 'NOT FOUND';
                return body.slice(Math.max(0, idx - 1500), idx + 500);
            }"""
        )
        print("\n=== HTML AROUND 'Verify you are human' ===")
        print(html_snippet)

        await browser.close()


asyncio.run(main())
