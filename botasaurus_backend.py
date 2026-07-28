"""
MapLead — Botasaurus (anti-detect) Google Maps backend
=======================================================

Free, open-source backend powered by `botasaurus-driver`
(https://github.com/omkarcloud/botasaurus) — the anti-detect
Selenium-compatible driver behind omkarcloud's popular
google-maps-scraper.

Why this backend exists:
- The plain Playwright backend stops at ~7 results on Google Maps because
  Google's anti-bot kicks in within seconds. Botasaurus patches dozens of
  fingerprint signals (Canvas, WebGL, AudioContext, permissions, WebRTC,
  CDP leak, etc.) so Google treats us as a real user.
- It's free, no API key, no signup, runs on your machine.
- Same UI as the rest of MapLead.

Install with:  pip install botasaurus-driver
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any, Optional

from scraper import Business, BusinessList, ProgressCallback

logger = logging.getLogger("maplead.botasaurus")

# ---------------------------------------------------------------------------
# Selectors — Google Maps DOM (CSS only, kept in sync with scraper.py)
# ---------------------------------------------------------------------------
SEARCH_INPUT_SELECTORS = [
    'input[name="q"]',        # current design (mid-2025+)
    '#searchboxinput',        # legacy selector
]
LISTINGS_PANEL = 'div[role="feed"]'
LISTING_LINK = 'a[href*="/maps/place/"]'
PLACE_NAME = 'h1.DUwDvf'
PLACE_ADDRESS = 'button[data-item-id="address"] div.rogA2c, button[data-item-id="address"]'
PLACE_PHONE = 'button[data-item-id^="phone"] div.rogA2c, button[data-item-id^="phone"]'
PLACE_WEBSITE = 'a[data-item-id="authority"]'
PLACE_RATING = 'div.F7nice span[aria-hidden="true"]'
PLACE_REVIEWS = 'span.RDAp4e, button[aria-label*="reviews" i], a[href*="review"]'
PLACE_CATEGORY = 'button.DkEaL'

# Anti-bot helpers
SCROLL_PAUSE_RANGE = (1.4, 2.6)  # seconds between scrolls
CLICK_PAUSE_RANGE = (2.5, 4.5)   # seconds after clicking a listing


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _safe_int(value: Any) -> Optional[int]:
    f = _safe_float(value)
    return int(f) if f is not None else None


# ---------------------------------------------------------------------------
# Sync scraper — runs inside asyncio.to_thread to keep the async MapLead API
# ---------------------------------------------------------------------------
def _scrape_sync(
    search_term: str,
    total: int,
    headless: bool,
    locale: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> BusinessList:
    """Scrape Google Maps with botasaurus-driver. Synchronous — wrap in to_thread."""
    from botasaurus_driver.driver import Driver

    business_list = BusinessList()

    driver = Driver(
        headless=headless,
        block_images=True,           # faster, less noise on the wire
        wait_for_complete_page_load=False,
        lang=locale,
        window_size=(1366, 900),
        # Anti-detect extensions: these come built-in with botasaurus-driver
        # and bypass Google/Cloudflare fingerprinting out of the box.
    )

    try:
        logger.info("[Botasaurus] Navigating to Google Maps for: %s", search_term)
        # `google_get` adds a Google referer — looks more legit to Maps
        driver.google_get("https://www.google.com/maps", accept_google_cookies=True)

        # 1. Type the search query (try modern selector first, fall back to legacy)
        # Use `type` (real keystrokes) instead of `set_text` because Google Maps
        # intercepts input events via jsaction handlers — value-only assignment
        # doesn't trigger the autocomplete UI.
        filled = False
        for sel in SEARCH_INPUT_SELECTORS:
            if driver.is_element_present(sel):
                # Click to focus, then type — this triggers jsaction events
                driver.click(sel, wait=4)
                driver.type(sel, search_term, wait=4)
                filled = True
                logger.info("[Botasaurus] Used search selector: %s", sel)
                break
        if not filled:
            raise RuntimeError(
                "Could not find Google Maps search input. "
                f"Tried: {SEARCH_INPUT_SELECTORS}"
            )
        driver.sleep(random.uniform(0.6, 1.4))

        # Submit search: click the search button (more reliable than Enter key
        # because Google Maps intercepts keyboard events via jsaction handlers).
        if driver.is_element_present('button[aria-label="Search"]'):
            driver.click('button[aria-label="Search"]', wait=4)
        else:
            # Fallback: dispatch Enter via JS
            active_sel = 'input[name="q"]' if driver.is_element_present('input[name="q"]') else '#searchboxinput'
            driver.run_js(
                f"document.querySelector('{active_sel}').dispatchEvent("
                "new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }))"
            )
        driver.sleep(random.uniform(4.5, 6.5))

        # 2. Confirm we're on the results page
        if driver.count(LISTINGS_PANEL, wait=2) == 0:
            # Sometimes Maps shows a "Places" tab first. Click it.
            for tab in ('button[aria-label*="Places"]', 'button[aria-label*="places"]'):
                if driver.is_element_present(tab):
                    driver.click(tab)
                    driver.sleep(2.0)
                    break
            if driver.count(LISTINGS_PANEL, wait=2) == 0:
                logger.warning("[Botasaurus] Results panel not found")
                return business_list

        # 3. Scroll the panel until we have enough listings
        logger.info("[Botasaurus] Scrolling to load up to %d listings", total)
        rounds_without_new = 0
        previous_count = 0
        for _ in range(120):  # hard cap; usually converges in <30 rounds
            current = driver.count(LISTING_LINK, wait=2)
            if current >= total:
                logger.info("[Botasaurus] Got %d listings (target %d)", current, total)
                break
            if current == previous_count:
                rounds_without_new += 1
                if rounds_without_new >= 6:
                    logger.info("[Botasaurus] Stopped scrolling: no new listings in %d rounds", rounds_without_new)
                    break
            else:
                rounds_without_new = 0
            previous_count = current

            # Scroll the results panel
            driver.scroll(LISTINGS_PANEL, by=random.randint(600, 900), smooth_scroll=False)
            driver.sleep(random.uniform(*SCROLL_PAUSE_RANGE))

        listings_now = driver.count(LISTING_LINK, wait=2)
        logger.info("[Botasaurus] Have %d listing links", listings_now)
        if listings_now == 0:
            return business_list

        # 4. Click each listing and extract details
        # Use JS to click by index — avoids stale-element issues
        # and works regardless of DOM mutations.
        to_visit = min(listings_now, total)
        logger.info("[Botasaurus] Clicking %d listings to extract details", to_visit)
        for idx in range(to_visit):
            try:
                # Click the nth result link via JS. NOTE: run_js wraps in IIFE,
                # so the script body must `return` a value (None otherwise).
                clicked = driver.run_js(
                    f"var els = document.querySelectorAll('{LISTING_LINK}');\n"
                    f"var el = els[{idx}];\n"
                    f"if (!el) return false;\n"
                    f"el.click();\n"
                    f"return true;"
                )
                if not clicked:
                    logger.info("[Botasaurus] Listing %d not clickable (skipping)", idx)
                    continue
                driver.sleep(random.uniform(*CLICK_PAUSE_RANGE))

                # Extract details from the side-panel that opens
                try:
                    biz = _extract_details(driver)
                    if biz and biz.name:
                        business_list.add(biz)
                        logger.info("[Botasaurus] Got: %s", biz.name[:60])
                except Exception as exc:
                    logger.warning("[Botasaurus] Extract failed for listing %d: %s", idx, exc)

                # Go back to the list so we can click the next listing
                if idx < to_visit - 1:
                    driver.run_js(
                        "var backBtn = document.querySelector('button[aria-label*=\"Back\" i]');"
                        "if (backBtn) backBtn.click();"
                        "return true;"
                    )
                    driver.sleep(random.uniform(0.6, 1.2))

            except Exception as exc:  # noqa: BLE001
                logger.warning("[Botasaurus] Listing %d failed: %s", idx, exc)
                continue

        logger.info("[Botasaurus] Done: %d businesses extracted", len(business_list))

        return business_list
    finally:
        try:
            driver.close()
        except Exception:  # noqa: BLE001
            pass


def _clean_text(value: Optional[str]) -> Optional[str]:
    """Strip leading/trailing whitespace and any non-printable icon noise."""
    if not value:
        return None
    # Google Maps embeds icon characters (private-use codepoints) in field values.
    # Remove anything before the first printable phone/digit/letter.
    cleaned = re.sub(r"^[^0-9A-Za-z+]+", "", value).strip()
    # Collapse internal newlines + spaces
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or None


def _extract_details(driver) -> Optional[Business]:
    """Pull name/phone/website/etc. from the open detail side-panel."""
    try:
        # Wait briefly for the panel to render
        driver.wait_for_element(PLACE_NAME, wait=4)
    except Exception:
        return None

    name_raw = driver.get_text(PLACE_NAME) or None
    if not name_raw:
        return None
    # Some Maps designs embed the category next to the name, e.g.
    # "Tan Coffee | Hyderabad, Telangana". Keep just the first line / before "|".
    name = _clean_text(name_raw.split("|")[0].split("\n")[0])
    if not name:
        return None

    address = None
    if driver.is_element_present(PLACE_ADDRESS):
        address = _clean_text(driver.get_text(PLACE_ADDRESS))

    phone = None
    if driver.is_element_present(PLACE_PHONE):
        phone = _clean_text(driver.get_text(PLACE_PHONE))

    website = None
    if driver.is_element_present(PLACE_WEBSITE):
        website = driver.get_attribute(PLACE_WEBSITE, "href") or None

    rating = _safe_float(driver.get_text(PLACE_RATING)) if driver.is_element_present(PLACE_RATING) else None
    reviews_count = None
    if driver.is_element_present(PLACE_REVIEWS):
        reviews_text = driver.get_text(PLACE_REVIEWS)
        # "152 reviews" or "(152)" — extract the number
        reviews_count = _safe_int(reviews_text)
    if reviews_count is None:
        # Fallback: parse from page text — Google Maps always shows the count
        # somewhere on the detail panel, e.g. "4.6\n(2,089) reviews"
        page_text = driver.page_text or ""
        m = re.search(r"\(([\d,]+)\)\s*reviews?", page_text)
        if m:
            reviews_count = _safe_int(m.group(1))

    category = driver.get_text(PLACE_CATEGORY) if driver.is_element_present(PLACE_CATEGORY) else None

    return Business(
        name=name,
        address=address,
        website=website,
        phone_number=phone,
        category=category.strip() if category else None,
        reviews_average=rating,
        reviews_count=reviews_count,
        latitude=None,
        longitude=None,
        google_maps_url=driver.current_url,
    )


# ---------------------------------------------------------------------------
# Async backend — same interface as the rest of MapLead
# ---------------------------------------------------------------------------
class BotasaurusBackend:
    """Free, anti-detect Google Maps scraper via botasaurus-driver.

    Why this beats plain Playwright:
    - botasaurus-driver patches 50+ fingerprint signals (Canvas, WebGL,
      AudioContext, Permissions, WebRTC, CDP, timezone, etc.) so Google's
      anti-bot treats us as a regular user.
    - Tested against bot.sannysoft.com, browserscan.net, fingerprint.com —
      all return "not detected".
    - No API key, no signup, no proxy needed.

    Install:  pip install botasaurus-driver
    """

    name = "Botasaurus (free, anti-detect)"
    requires_api_key = False

    def __init__(self) -> None:
        try:
            import botasaurus_driver  # noqa: F401  (import check)
        except ImportError as exc:
            raise RuntimeError(
                "Botasaurus not installed. Run:  pip install botasaurus"
            ) from exc

    async def scrape(
        self,
        search_term: str,
        total: int = 30,
        progress_callback: Optional[ProgressCallback] = None,
        **kwargs: Any,
    ) -> BusinessList:
        headless = kwargs.get("headless", True)
        locale = kwargs.get("locale", "en")

        # Wrap sync scraper in to_thread so it doesn't block the event loop
        result = await asyncio.to_thread(
            _scrape_sync,
            search_term=search_term,
            total=total,
            headless=headless,
            locale=locale,
            progress_callback=progress_callback,
        )

        if progress_callback and result.business_list:
            await progress_callback(len(result.business_list), total)

        return result