"""Targeted diagnostic: capture the RAW HTTP response (all headers + full
body) for the failed login POST on newmexicopublicnotices.com/authenticate.aspx
— not just what ends up rendered in the DOM. Looking for anything more
specific than the generic "Invalid Email Address or Password" text: an
ASP.NET validation summary with an error code, a WAF/bot-detection header,
a Set-Cookie indicating a lockout state, X-AspNet-Version mismatches, etc.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_nm_login_raw_response.py
"""

import asyncio
import os
import sys
import urllib.parse as _urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config as cfg

NM_LOGIN_URL = "https://www.newmexicopublicnotices.com/authenticate.aspx"
OUT_DIR = os.path.join(os.path.dirname(__file__), "diag_output")


def _is_target_host(url: str) -> bool:
    try:
        return _urlparse.urlparse(url).hostname == "www.newmexicopublicnotices.com"
    except Exception:
        return False


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        captured_responses = []

        def on_response(resp):
            if resp.request.method == "POST" and _is_target_host(resp.url):
                captured_responses.append(resp)

        page.on("response", on_response)

        print(f"Email configured: {bool(cfg.NOTICE_SITE_EMAIL)}")
        print(f"Password configured: {bool(cfg.NOTICE_SITE_PASSWORD)}")

        await page.goto(NM_LOGIN_URL, wait_until="networkidle")

        email_el = page.locator(cfg.SEL_LOGIN_EMAIL)
        pw_el = page.locator(cfg.SEL_LOGIN_PASSWORD)
        await email_el.click()
        await email_el.fill(cfg.NOTICE_SITE_EMAIL)
        await asyncio.sleep(0.3)
        await pw_el.click()
        await pw_el.fill(cfg.NOTICE_SITE_PASSWORD)
        await asyncio.sleep(0.3)

        # Confirm what's actually in the fields right before submit
        actual_email = await email_el.input_value()
        actual_pw = await pw_el.input_value()
        print(f"Email field before submit: {actual_email!r}")
        print(f"Password field before submit: len={len(actual_pw)}, matches_env={actual_pw == cfg.NOTICE_SITE_PASSWORD}")

        btn = page.locator(cfg.SEL_LOGIN_SUBMIT)
        await btn.click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)

        print(f"\n{len(captured_responses)} POST response(s) to newmexicopublicnotices.com captured")

        for i, resp in enumerate(captured_responses):
            print(f"\n{'='*70}")
            print(f"Response #{i}: {resp.request.method} {resp.url}")
            print(f"Status: {resp.status} {resp.status_text}")

            print("\n--- All response headers ---")
            headers = await resp.all_headers()
            for k, v in headers.items():
                print(f"  {k}: {v}")

            print("\n--- Set-Cookie (from headers array, may include multiple) ---")
            try:
                headers_array = await resp.headers_array()
                cookies = [h for h in headers_array if h["name"].lower() == "set-cookie"]
                for c in cookies:
                    print(f"  {c['value']}")
            except Exception as e:
                print(f"  (could not read headers_array: {e})")

            print("\n--- Full response body ---")
            try:
                body = await resp.text()
                # Save full body to file (could be large — full ASP.NET page)
                body_path = os.path.join(OUT_DIR, f"nm_raw_response_{i}.html")
                with open(body_path, "w", encoding="utf-8") as f:
                    f.write(body)
                print(f"  Saved full body ({len(body):,} chars) to {body_path}")

                # Search for anything that looks like a specific error code,
                # validation summary, or ASP.NET custom error trace
                import re
                for pattern, label in [
                    (r'class="[^"]*[Vv]alidation[Ss]ummary[^"]*"[^>]*>(.*?)</\w+>', "ValidationSummary block"),
                    (r'(?i)error\s*code[:\s]*([A-Z0-9_-]+)', "Error code mention"),
                    (r'(?i)(blocked|banned|suspicious|automat(ed|ion)|bot\s*detect|rate\s*limit|too many|throttl)', "Bot/rate-limit keyword"),
                    (r'(?i)(locked|lockout|disabled|suspended)', "Lockout keyword"),
                    (r'(?i)(captcha|recaptcha|turnstile|hcaptcha)', "CAPTCHA keyword"),
                    (r'Server Error|Stack Trace|Exception', "ASP.NET server error/exception"),
                ]:
                    matches = re.findall(pattern, body)
                    if matches:
                        print(f"  [{label}] found {len(matches)} match(es): {matches[:3]}")
            except Exception as e:
                print(f"  (could not read body: {e})")

        # Also check final page state
        print(f"\nFinal URL: {page.url}")
        final_body = await page.locator("body").inner_text()
        import re
        error_snippet = final_body[max(0, final_body.find("Invalid") - 50):final_body.find("Invalid") + 200] if "Invalid" in final_body else "(no 'Invalid' text found)"
        print(f"Rendered error context: {error_snippet}")

        await page.screenshot(path=os.path.join(OUT_DIR, "nm_raw_response_final.png"), full_page=True)

        await asyncio.sleep(5)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
