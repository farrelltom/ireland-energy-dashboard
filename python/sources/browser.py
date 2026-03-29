"""
Shared Playwright browser helper.

Provides a single reusable function for fetching fully-rendered page HTML
using a headless Chromium browser.  Browser lifecycle is wholly owned here;
adapters supply all DOM-interaction parameters (selectors, assertions) so
that source-specific knowledge stays in the adapter, not in this helper.

Usage in an adapter's fetch() method:

    from sources.browser import fetch_rendered

    html = fetch_rendered(
        url,
        pre_click="#onetrust-accept-btn-handler",   # optional: dismiss overlays
        click="button:has(h3:text('Standard plans'))",  # optional: expand accordion
        wait_for="text=View tariff table",          # required content signal
        content_assertion="View tariff table",      # text that MUST appear in HTML
    )

Design notes:
- Each call launches and closes its own browser instance.  This is intentional:
  adapters run once per day and we prefer simplicity over connection pooling.
- wait_for is a Playwright locator string (CSS selector or text locator).
  It must refer to content that is only present after the JS interaction
  completes, making it a reliable data-ready signal.
- content_assertion raises ValueError if the text is absent in the final HTML,
  preventing a skeleton page from being saved as if it were valid raw data.
- pre_click, click, and wait_for timeouts are all caller-configurable so
  adapters can tune per-site behaviour without modifying this file.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def fetch_rendered(
    url: str,
    *,
    pre_click: str | None = None,
    pre_click_timeout_ms: int = 8_000,
    click: str | None = None,
    click_timeout_ms: int = 10_000,
    wait_for: str | None = None,
    wait_for_timeout_ms: int = 15_000,
    content_assertion: str,
    page_load_timeout_ms: int = 30_000,
) -> str:
    """Load *url* with headless Chromium and return the rendered page HTML.

    Parameters
    ----------
    url:
        The page to load.
    pre_click:
        CSS selector for an element to click before any other interaction
        (e.g. a cookie-consent button that blocks the main content).
    pre_click_timeout_ms:
        How long to wait for pre_click to appear (ms).  If the element is
        absent within the timeout, the step is silently skipped.
    click:
        CSS selector for an element to click after pre_click (e.g. an
        accordion toggle).  Raises TimeoutError if absent.
    click_timeout_ms:
        How long to wait for *click* to be actionable (ms).
    wait_for:
        A Playwright locator expression to wait for after clicking.  Should
        refer to content that only appears once the desired data is loaded.
        Raises TimeoutError if not found within *wait_for_timeout_ms*.
    wait_for_timeout_ms:
        Timeout for *wait_for* (ms).
    content_assertion:
        A string that MUST appear in the final HTML.  If absent, raises
        ValueError — this prevents saving a skeleton page as valid raw data.
    page_load_timeout_ms:
        Timeout for the initial page load (ms).

    Returns
    -------
    str
        Full rendered HTML of the page after all interactions.

    Raises
    ------
    ValueError
        If *content_assertion* is not found in the final HTML.
    playwright.TimeoutError
        If any mandatory selector or wait_for step times out.
    """
    # Import here so the module can be imported even when playwright is not
    # installed (e.g. in test environments that only run HTML-scraping tests).
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is not installed.  Run: pip install playwright && playwright install chromium"
        ) from exc

    logger.debug("browser: launching Chromium for %s", url)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=page_load_timeout_ms)

            if pre_click:
                try:
                    page.click(pre_click, timeout=pre_click_timeout_ms)
                    logger.debug("browser: pre_click %r done", pre_click)
                    page.wait_for_timeout(500)
                except Exception:
                    logger.debug("browser: pre_click %r not found or timed out — skipping", pre_click)

            if click:
                page.click(click, timeout=click_timeout_ms)
                logger.debug("browser: click %r done", click)

            if wait_for:
                page.wait_for_selector(wait_for, timeout=wait_for_timeout_ms)
                logger.debug("browser: wait_for %r satisfied", wait_for)

            html = page.content()
        finally:
            browser.close()

    if content_assertion not in html:
        raise ValueError(
            f"browser: content_assertion not found in rendered HTML for {url!r}. "
            f"Expected to find: {content_assertion!r}"
        )

    logger.debug("browser: fetched %d chars from %s", len(html), url)
    return html
