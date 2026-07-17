"""2Captcha integration for solving CAPTCHAs on notice detail pages.

Two independent CAPTCHA providers are in play depending on which notice site
is being scraped:
  - Google reCAPTCHA v2 — the original tnpublicnotice.com build. Solved via
    the 2Captcha SDK's `recaptcha()` helper (legacy in.php/res.php API).
  - Cloudflare Turnstile — found on mopublicnotices.com's Details.aspx pages
    (confirmed live against a Jackson County MO probate notice on
    2026-07-16 — see logs/screenshots/timeout_result1_attempt1_*.png from
    that debugging session). Solved via 2Captcha's newer JSON API:
    POST /createTask with type "TurnstileTaskProxyless", then poll
    /getTaskResult.

Detection is explicit (separate iframe selectors), not unified into one
code path — a site using either provider keeps working independently of
the other, and a site using neither falls through cleanly.

Critical lesson from the Turnstile investigation: do NOT wait on
`page.wait_for_load_state("networkidle")` while a Turnstile widget is
unsolved. Turnstile does its own continuous background network activity,
so networkidle never fires — the page isn't hung, the wait condition is
just permanently false. Use a bounded `wait_for_function` polling for the
actual cleared state instead.
"""

import asyncio
import logging
import time

import requests
from playwright.async_api import Page, TimeoutError as PwTimeout
from twocaptcha import TwoCaptcha

import config
from config import (
    MAX_RETRIES,
    RECAPTCHA_SITEKEY,
    SEL_CAPTCHA_IFRAME,
    SEL_TURNSTILE_WIDGET,
    SEL_VIEW_NOTICE_BUTTON,
)

logger = logging.getLogger(__name__)

TWOCAPTCHA_CREATE_TASK_URL = "https://api.2captcha.com/createTask"
TWOCAPTCHA_GET_RESULT_URL = "https://api.2captcha.com/getTaskResult"
TURNSTILE_POLL_INTERVAL = 3.0     # seconds between getTaskResult polls
TURNSTILE_POLL_TIMEOUT = 90.0     # give up waiting on the solve after this long
TURNSTILE_CLEAR_TIMEOUT_MS = 15_000  # wait for widget/content state after injecting token
TURNSTILE_AGREE_TIMEOUT_MS = 15_000  # wait for content after clicking "I Agree, View Notice"


async def solve_captcha_and_view(page: Page) -> bool:
    """Detect which CAPTCHA (if any) guards this notice detail page and solve it.

    Retries are handled inside each provider-specific solver. Returns True if
    the notice text is now visible, False otherwise.
    """
    if not config.CAPTCHA_API_KEY:
        logger.error("CAPTCHA_API_KEY not set — cannot solve CAPTCHA")
        return False

    page_url = page.url

    # Check for IP block message before wasting time on CAPTCHA
    block_msg = await page.query_selector(
        "text='You are not permitted to view public notices'"
    )
    if block_msg:
        logger.error(
            "IP BLOCKED: Site says 'not permitted to view' — "
            "need residential proxy or different IP"
        )
        return False

    # Check if the notice content is already visible (no CAPTCHA needed)
    content_el = await page.query_selector("text='Notice Content'")
    if content_el:
        logger.info("Notice content already visible — no CAPTCHA needed")
        return True

    # Explicit detection — Turnstile and reCAPTCHA are solved by entirely
    # separate functions below, so a site running either provider keeps
    # working regardless of what the other one does. Give either iframe a
    # few seconds to actually render before deciding neither is present —
    # the caller only waits for domcontentloaded before we get here, and
    # both providers inject their iframe via JS shortly after that.
    detected = await _detect_captcha_iframe(page)
    if detected == "turnstile":
        logger.warning("Detected Cloudflare Turnstile on %s", page_url)
        return await _solve_turnstile_and_view(page)

    if detected == "recaptcha":
        logger.warning("Detected reCAPTCHA v2 on %s", page_url)
        return await _solve_recaptcha_and_view(page)

    logger.warning(
        "No known CAPTCHA iframe (Turnstile or reCAPTCHA) found on %s — "
        "trying the reCAPTCHA v2 path as a last resort",
        page_url,
    )
    return await _solve_recaptcha_and_view(page)


async def _detect_captcha_iframe(page: Page, timeout_ms: int = 15_000) -> str | None:
    """Wait briefly for either CAPTCHA provider's marker element to appear.

    Returns "turnstile", "recaptcha", or None if neither shows up in time.

    Turnstile is detected by its widget container (SEL_TURNSTILE_WIDGET,
    the standard `cf-turnstile` class) rather than an iframe[src=...]
    selector — mopublicnotices.com's Turnstile iframe has no `src`
    attribute at all (confirmed via live DOM dump 2026-07-16:
    tests/diag_turnstile.py), so a src-based selector could never match it.

    15s (not 5s) because Turnstile was observed taking longer than 5s to
    render during the same investigation — a 5s window fell through to the
    reCAPTCHA "last resort" path on every attempt, burning wasted (though
    unbilled — 2Captcha returns ERROR_CAPTCHA_UNSOLVABLE for free) retries
    instead of ever reaching the Turnstile solver.
    """
    try:
        result = await page.wait_for_function(
            """([turnstileSel, recaptchaSel]) => {
                if (document.querySelector(turnstileSel)) return 'turnstile';
                if (document.querySelector(recaptchaSel)) return 'recaptcha';
                return null;
            }""",
            arg=[SEL_TURNSTILE_WIDGET, SEL_CAPTCHA_IFRAME],
            timeout=timeout_ms,
        )
        return await result.json_value()
    except PwTimeout:
        return None


# ── Google reCAPTCHA v2 ────────────────────────────────────────────────


async def _solve_recaptcha_and_view(page: Page) -> bool:
    """Solve Google reCAPTCHA v2 and click View Notice to reveal the full text.

    Retries up to MAX_RETRIES times on failure.
    """
    page_url = page.url

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            try:
                view_btn = await page.wait_for_selector(
                    SEL_VIEW_NOTICE_BUTTON, timeout=15000
                )
            except PwTimeout:
                logger.warning(
                    "View Notice button not found within 15s on %s (attempt %d/%d)",
                    page_url, attempt, MAX_RETRIES,
                )
                continue

            logger.warning(
                "Solving reCAPTCHA for %s (attempt %d/%d)", page_url, attempt, MAX_RETRIES
            )
            solver = TwoCaptcha(config.CAPTCHA_API_KEY)
            result = solver.recaptcha(
                sitekey=RECAPTCHA_SITEKEY,
                url=page_url,
            )
            token = result.get("code") if isinstance(result, dict) else str(result)

            if not token:
                logger.warning("2Captcha returned empty token (attempt %d)", attempt)
                continue

            # Inject the token into the page's hidden reCAPTCHA response field
            await page.evaluate(
                """(token) => {
                    const el = document.getElementById('g-recaptcha-response');
                    if (el) { el.value = token; el.style.display = 'block'; }
                    const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
                    if (ta) { ta.value = token; ta.style.display = 'block'; }
                    // Trigger the reCAPTCHA callback if it exists
                    if (typeof ___grecaptcha_cfg !== 'undefined') {
                        const clients = ___grecaptcha_cfg.clients;
                        if (clients) {
                            Object.keys(clients).forEach(key => {
                                const client = clients[key];
                                const findCallback = (obj) => {
                                    if (!obj || typeof obj !== 'object') return;
                                    Object.values(obj).forEach(v => {
                                        if (typeof v === 'object' && v !== null) {
                                            if (typeof v.callback === 'function') {
                                                v.callback(token);
                                            }
                                            findCallback(v);
                                        }
                                    });
                                };
                                findCallback(client);
                            });
                        }
                    }
                }""",
                token,
            )

            # Brief pause for any callback-triggered actions
            await asyncio.sleep(1)

            # Click the "View Notice" button to submit with the solved CAPTCHA.
            # Re-find the button in case the callback caused a DOM update.
            view_btn = await page.query_selector(SEL_VIEW_NOTICE_BUTTON)
            if not view_btn:
                # Callback may have auto-submitted — check if content is visible
                content_el = await page.query_selector("text='Notice Content'")
                if content_el:
                    logger.warning("CAPTCHA solved — callback auto-submitted form")
                    return True
                logger.warning("View Notice button gone after token inject (attempt %d)", attempt)
                continue

            await view_btn.click()
            await page.wait_for_load_state("networkidle")

            # Verify the notice content is now visible
            content_el = await page.query_selector("text='Notice Content'")
            if content_el:
                logger.warning("CAPTCHA solved — notice text visible")
                return True

            # Fallback: check if CAPTCHA message is gone
            captcha_msg = await page.query_selector(
                "text='You must complete the reCAPTCHA'"
            )
            if not captcha_msg:
                logger.warning("CAPTCHA solved — gate cleared")
                return True

            logger.warning("CAPTCHA still present after attempt %d", attempt)

        except Exception:
            logger.exception("reCAPTCHA solve error (attempt %d/%d)", attempt, MAX_RETRIES)

    logger.error("All %d reCAPTCHA attempts failed for %s", MAX_RETRIES, page_url)
    return False


# ── Cloudflare Turnstile ────────────────────────────────────────────────


async def _extract_turnstile_sitekey(page: Page) -> str | None:
    """Find the Turnstile widget's sitekey in the page DOM.

    Cloudflare Turnstile renders its container with a `data-sitekey`
    attribute. Falls back to parsing the challenge iframe's `k` query
    param (Cloudflare embeds the sitekey there too) if the container
    attribute isn't present by the time we look.
    """
    try:
        return await page.evaluate(
            """() => {
                const el = document.querySelector('[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                const iframe = document.querySelector(
                    'iframe[src*="challenges.cloudflare.com"]'
                );
                if (iframe) {
                    const src = iframe.getAttribute('src') || '';
                    const match = src.match(/[?&]k=([^&]+)/);
                    if (match) return decodeURIComponent(match[1]);
                }
                return null;
            }"""
        )
    except Exception:
        logger.debug("Turnstile sitekey extraction failed", exc_info=True)
        return None


async def _create_turnstile_task(sitekey: str, url: str) -> int | None:
    """POST /createTask with type TurnstileTaskProxyless. Returns taskId, or None on failure."""
    try:
        resp = await asyncio.to_thread(
            requests.post,
            TWOCAPTCHA_CREATE_TASK_URL,
            json={
                "clientKey": config.CAPTCHA_API_KEY,
                "task": {
                    "type": "TurnstileTaskProxyless",
                    "websiteURL": url,
                    "websiteKey": sitekey,
                },
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("errorId"):
            logger.warning(
                "2Captcha createTask error: %s (%s)",
                data.get("errorCode"), data.get("errorDescription"),
            )
            return None
        return data.get("taskId")
    except Exception:
        logger.exception("2Captcha createTask request failed")
        return None


async def _poll_turnstile_result(task_id: int) -> str | None:
    """Poll /getTaskResult until the Turnstile solve is ready or the poll times out."""
    deadline = time.monotonic() + TURNSTILE_POLL_TIMEOUT
    while time.monotonic() < deadline:
        await asyncio.sleep(TURNSTILE_POLL_INTERVAL)
        try:
            resp = await asyncio.to_thread(
                requests.post,
                TWOCAPTCHA_GET_RESULT_URL,
                json={"clientKey": config.CAPTCHA_API_KEY, "taskId": task_id},
                timeout=15,
            )
            data = resp.json()
            if data.get("errorId"):
                logger.warning(
                    "2Captcha getTaskResult error: %s (%s)",
                    data.get("errorCode"), data.get("errorDescription"),
                )
                return None
            if data.get("status") == "ready":
                return data.get("solution", {}).get("token")
            # status == "processing" — keep polling
        except Exception:
            logger.exception("2Captcha getTaskResult request failed")
            return None

    logger.warning(
        "2Captcha Turnstile solve timed out after %.0fs (taskId=%s)",
        TURNSTILE_POLL_TIMEOUT, task_id,
    )
    return None


async def _wait_for_turnstile_cleared(page: Page, timeout_ms: int = TURNSTILE_CLEAR_TIMEOUT_MS) -> bool:
    """Best-effort wait for the injected token to register after we set it.

    Deliberately NOT page.wait_for_load_state("networkidle") — Turnstile's
    own background network activity means the page never goes idle while
    the widget is present. That's what turned a would-be fast failure into
    a 60-second hang per notice during the 2026-07-16 investigation.

    This is intentionally lenient: it confirms the hidden response field
    actually picked up our token (proof the injection worked), or that the
    notice content already appeared outright. It does NOT require the
    widget's container to disappear — Cloudflare's checkbox UI may not
    visually update just from an API-injected token, and the real proof is
    whether clicking "I Agree, View Notice" afterward reveals the notice.
    A False return here is a soft signal, not a hard failure — the caller
    still attempts the Agree click either way.
    """
    try:
        await page.wait_for_function(
            """() => {
                const bodyText = document.body.innerText;
                if (bodyText.includes('Notice Content')) return true;
                const resp = document.querySelector(
                    'input[name="cf-turnstile-response"], input[id^="cf-chl-widget-"]'
                );
                return !!(resp && resp.value && resp.value.length > 0);
            }""",
            timeout=timeout_ms,
        )
        return True
    except PwTimeout:
        return False


async def _solve_turnstile_and_view(page: Page) -> bool:
    """Solve Cloudflare Turnstile and click 'I Agree, View Notice' to reveal the full text.

    Uses 2Captcha's createTask (TurnstileTaskProxyless) + getTaskResult JSON
    API — a separate code path from the reCAPTCHA v2 solver above, so either
    provider keeps working independently of the other. Retries up to
    MAX_RETRIES times on failure.
    """
    page_url = page.url

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            try:
                await page.wait_for_selector(SEL_VIEW_NOTICE_BUTTON, timeout=15000)
            except PwTimeout:
                logger.warning(
                    "View Notice button not found within 15s on %s (attempt %d/%d)",
                    page_url, attempt, MAX_RETRIES,
                )
                continue

            sitekey = await _extract_turnstile_sitekey(page)
            if not sitekey:
                logger.warning(
                    "Could not find Turnstile sitekey on %s (attempt %d/%d)",
                    page_url, attempt, MAX_RETRIES,
                )
                continue

            logger.warning(
                "Solving Turnstile for %s (attempt %d/%d, sitekey=%s)",
                page_url, attempt, MAX_RETRIES, sitekey,
            )
            task_id = await _create_turnstile_task(sitekey, page_url)
            if not task_id:
                continue

            token = await _poll_turnstile_result(task_id)
            if not token:
                logger.warning("2Captcha returned no Turnstile token (attempt %d)", attempt)
                continue

            # Inject the token into Cloudflare's hidden response field and
            # invoke any declared data-callback, mirroring what the widget's
            # own JS would do on a real solve. Targets both the standard
            # `name="cf-turnstile-response"` field and the dynamic
            # `id="cf-chl-widget-*_response"` pattern actually observed on
            # mopublicnotices.com (2026-07-16 DOM dump) — the two aren't
            # confirmed identical there, so we set both defensively.
            await page.evaluate(
                """(token) => {
                    document.querySelectorAll(
                        'input[name="cf-turnstile-response"], input[id^="cf-chl-widget-"]'
                    ).forEach(el => { el.value = token; });
                    const widget = document.querySelector('[data-sitekey]');
                    const cb = widget && widget.getAttribute('data-callback');
                    if (cb && typeof window[cb] === 'function') {
                        window[cb](token);
                    }
                    if (window.turnstile && typeof window.turnstile.callback === 'function') {
                        window.turnstile.callback(token);
                    }
                }""",
                token,
            )

            cleared = await _wait_for_turnstile_cleared(page)
            if not cleared:
                # Soft signal only — the token may still be valid even if we
                # couldn't confirm it landed in the response field in time.
                # Proceed to the Agree click; that's the real test.
                logger.warning(
                    "Could not confirm Turnstile token registered (attempt %d) — "
                    "trying 'I Agree, View Notice' anyway",
                    attempt,
                )

            # Content may already be visible via an AJAX reveal
            content_el = await page.query_selector("text='Notice Content'")
            if content_el:
                logger.warning("Turnstile solved — notice text visible without extra click")
                return True

            # Otherwise submit with "I Agree, View Notice"
            view_btn = await page.query_selector(SEL_VIEW_NOTICE_BUTTON)
            if not view_btn:
                logger.warning("View Notice button gone after Turnstile clear (attempt %d)", attempt)
                continue

            await view_btn.click()
            # Same reasoning as _wait_for_turnstile_cleared — poll for the
            # actual content instead of waiting on networkidle.
            try:
                await page.wait_for_selector(
                    "text='Notice Content'", timeout=TURNSTILE_AGREE_TIMEOUT_MS
                )
                logger.warning("Turnstile solved — notice text visible")
                return True
            except PwTimeout:
                logger.warning("Notice Content did not appear after Agree click (attempt %d)", attempt)

        except Exception:
            logger.exception("Turnstile solve error (attempt %d/%d)", attempt, MAX_RETRIES)

    logger.error("All %d Turnstile attempts failed for %s", MAX_RETRIES, page_url)
    return False
