"""One-off diagnostic: verify the fixed _auto_login() actually waits for the
real post-submit redirect instead of a fixed 3-5s sleep, using a throwaway
browser profile (not the shared .ancestry_profile) so this doesn't disturb
the already-authenticated persistent session used by production runs.

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_verify_auto_login.py
"""

import asyncio
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ancestry_enricher

TMP_PROFILE_DIR = os.path.join(os.path.dirname(__file__), "diag_output", "tmp_ancestry_profile")


async def main():
    from playwright.async_api import async_playwright

    if os.path.exists(TMP_PROFILE_DIR):
        shutil.rmtree(TMP_PROFILE_DIR)
    os.makedirs(TMP_PROFILE_DIR, exist_ok=True)

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        TMP_PROFILE_DIR,
        headless=False,
        viewport={"width": 1920, "height": 1080},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = context.pages[0] if context.pages else await context.new_page()

    try:
        print("Calling _auto_login() on a fresh (never-logged-in) profile...")
        start = time.monotonic()
        ok = await ancestry_enricher._auto_login(page)
        elapsed = time.monotonic() - start

        print(f"\n_auto_login() returned: {ok}")
        print(f"Elapsed: {elapsed:.1f}s")
        print(f"Final URL: {page.url}")

        if ok:
            print("PASS — login succeeded and was correctly detected.")
        else:
            print("FAIL — login was not detected as successful (check screenshot/logs).")

        await page.screenshot(
            path=os.path.join(os.path.dirname(__file__), "diag_output", "verify_auto_login_final.png"),
            full_page=True,
        )

    finally:
        await context.close()
        await pw.stop()
        shutil.rmtree(TMP_PROFILE_DIR, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
