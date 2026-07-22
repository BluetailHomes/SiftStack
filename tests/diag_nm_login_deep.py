"""Deep diagnostic: newmexicopublicnotices.com login fails via Playwright
even though the exact same .env credentials work in a real browser. Inspect
what's actually happening — read back filled values, capture the real POST
payload, and try keystroke-by-keystroke typing instead of .fill() in case
client-side JS validation depends on real input/keyup events.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_nm_login_deep.py
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
        browser = await p.chromium.launch(headless=False, slow_mo=100)
        context = await browser.new_context()
        page = await context.new_page()

        # Capture the actual POST request payload for the auth postback
        captured = {}

        import urllib.parse as _urlparse

        def _is_target_host(url: str) -> bool:
            try:
                return _urlparse.urlparse(url).hostname == "www.newmexicopublicnotices.com"
            except Exception:
                return False

        def on_request(req):
            if req.method == "POST" and _is_target_host(req.url):
                captured["url"] = req.url
                captured["post_data"] = req.post_data

        def on_response(resp):
            if _is_target_host(resp.url) and resp.request.method == "POST":
                captured["status"] = resp.status
                captured["headers"] = dict(resp.headers)

        page.on("request", on_request)
        page.on("response", on_response)

        await page.goto(NM_LOGIN_URL, wait_until="networkidle")

        # Dump all hidden/non-obvious fields on the login form — looking for
        # an invisible reCAPTCHA/WAF token field a real browser would
        # populate via JS that Playwright might not trigger.
        hidden_fields = await page.evaluate("""() => {
            const form = document.querySelector('form');
            if (!form) return [];
            return Array.from(form.querySelectorAll('input')).map(el => ({
                name: el.name, type: el.type, id: el.id,
                value_len: (el.value || '').length,
            }));
        }""")
        print("Form input fields:")
        for f in hidden_fields:
            print(f"  {f}")

        print(f"Email to type: {cfg.NOTICE_SITE_EMAIL!r} (len={len(cfg.NOTICE_SITE_EMAIL)})")
        print(f"Password to type: len={len(cfg.NOTICE_SITE_PASSWORD)}")

        email_el = page.locator(cfg.SEL_LOGIN_EMAIL)
        pw_el = page.locator(cfg.SEL_LOGIN_PASSWORD)

        # Keystroke-by-keystroke instead of .fill(), in case client JS
        # listens for real input events
        await email_el.click()
        await email_el.type(cfg.NOTICE_SITE_EMAIL, delay=50)
        await asyncio.sleep(0.3)
        await pw_el.click()
        await pw_el.type(cfg.NOTICE_SITE_PASSWORD, delay=50)
        await asyncio.sleep(0.3)

        # Read back what's actually in the fields
        actual_email = await email_el.input_value()
        actual_pw = await pw_el.input_value()
        print(f"Actual email in field: {actual_email!r}")
        print(f"Actual password in field matches: {actual_pw == cfg.NOTICE_SITE_PASSWORD}")
        print(f"Actual password length in field: {len(actual_pw)}")

        await page.screenshot(path=os.path.join(OUT_DIR, "deep_01_before_submit.png"), full_page=True)

        btn = page.locator(cfg.SEL_LOGIN_SUBMIT)
        print(f"Submit button visible: {await btn.is_visible()}, enabled: {await btn.is_enabled()}")

        await btn.click()
        await page.wait_for_load_state("networkidle")

        print(f"\nCaptured POST: {captured.get('url', 'NONE CAPTURED')}")
        print(f"Response status: {captured.get('status', 'N/A')}")
        resp_headers = captured.get("headers", {})
        interesting_headers = {k: v for k, v in resp_headers.items()
                                if any(term in k.lower() for term in
                                       ["cf-", "x-sucuri", "x-waf", "server", "x-akamai", "x-cache"])}
        if interesting_headers:
            print(f"Interesting response headers: {interesting_headers}")
        post_data = captured.get("post_data", "")
        if post_data:
            # Don't print raw password value — just confirm the field names
            # present and whether the email appears correctly encoded.
            import urllib.parse
            parsed = urllib.parse.parse_qs(post_data)
            email_field_name = cfg.SEL_LOGIN_EMAIL.lstrip("#")
            pw_field_name = cfg.SEL_LOGIN_PASSWORD.lstrip("#")
            print(f"POST includes email field: {email_field_name in parsed}")
            if email_field_name in parsed:
                print(f"POST email value: {parsed[email_field_name]}")
            print(f"POST includes password field: {pw_field_name in parsed}")
            if pw_field_name in parsed:
                pw_vals = parsed[pw_field_name]
                print(f"POST password length: {len(pw_vals[0]) if pw_vals else 0}")
                print(f"POST password matches .env value: {pw_vals[0] == cfg.NOTICE_SITE_PASSWORD if pw_vals else False}")

        print(f"\nFinal URL: {page.url}")
        await page.screenshot(path=os.path.join(OUT_DIR, "deep_02_after_submit.png"), full_page=True)

        body = await page.locator("body").inner_text()
        if "Invalid Email" in body:
            print("Result: Invalid Email Address or Password (still failing)")
        elif "smartsearch" in page.url.lower():
            print("Result: SUCCESS")
        else:
            print("Result: unclear — check screenshot")

        await asyncio.sleep(10)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
