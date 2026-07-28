"""
MapLead — Google Maps Business Scraper
=======================================

Async scraping engine built on Playwright with:
- Multiple CSS selector fallbacks (Google rotates them)
- Locale-aware rating & review-count parsing
- Randomized, humanized scroll & click behaviour
- Retry with exponential backoff
- Progress callbacks for live UI updates
- Dedupe by (name, address)
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable, Optional

from playwright.async_api import Browser, Page, async_playwright

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Business:
    """One business lead."""

    name: Optional[str] = None
    address: Optional[str] = None
    website: Optional[str] = None
    phone_number: Optional[str] = None
    category: Optional[str] = None
    reviews_average: Optional[float] = None
    reviews_count: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    google_maps_url: Optional[str] = None
    is_closed: Optional[bool] = None  # True = permanently closed; None = unknown

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def completeness_score(self) -> int:
        """0-4: how many key fields are populated (name counts, plus phone/website/rating/address)."""
        score = 0
        if self.name:
            score += 1
        if self.phone_number:
            score += 1
        if self.website:
            score += 1
        if self.reviews_average is not None:
            score += 1
        if self.address:
            score += 1
        return min(score, 4)


@dataclass
class BusinessList:
    """Holds a deduplicated list of Business objects."""

    business_list: list[Business] = field(default_factory=list)

    def add(self, business: Business) -> None:
        # Dedupe by (name, address) — survives across pagination
        if not business.name:
            return
        key = (business.name.strip().lower(), (business.address or "").strip().lower())
        for existing in self.business_list:
            existing_key = (
                (existing.name or "").strip().lower(),
                (existing.address or "").strip().lower(),
            )
            if existing_key == key:
                return
        self.business_list.append(business)

    def to_dataframe(self):
        import pandas as pd

        return pd.DataFrame([b.to_dict() for b in self.business_list])

    def __len__(self) -> int:
        return len(self.business_list)


# ---------------------------------------------------------------------------
# Selector fallbacks — Google changes these every few months
# ---------------------------------------------------------------------------

SELECTORS = {
    "search_input": [
        "#searchboxinput",
        'input[name="q"]',
    ],
    "consent_accept": [
        'button[aria-label*="Accept"]',
        'button:has-text("Accept all")',
        'button:has-text("Alle akzeptieren")',
        'button:has-text("Tout accepter")',
        'button:has-text("Aceptar todo")',
        'form[action*="consent"] button',
    ],
    "listings": [
        'a[href^="https://www.google.com/maps/place"]',
        'a[href*="/maps/place/"]',
    ],
    "name": [
        "h1.DUwDvf.lfPIob",
        'h1[class*="DUwDvf"]',
        'div[role="main"] h1',
    ],
    "address": [
        'button[data-item-id="address"] div.fontBodyMedium',
        'button[aria-label*="Address"] div.fontBodyMedium',
        'button[data-item-id="address"]',
        'div[data-item-id="address"]',
    ],
    "website": [
        'a[data-item-id="authority"] div.fontBodyMedium',
        'a[aria-label*="Website"] div',
        'a[data-item-id="authority"]',
    ],
    "phone": [
        'button[data-item-id^="phone"] div.fontBodyMedium',
        'button[data-item-id*="phone"] div.fontBodyMedium',
        'button[aria-label*="Phone"] div',
    ],
    "category": [
        'button[jsaction="pane.category"]',
        'div[class*="category"]',
        'button[aria-label*="ategory"]',
    ],
    "reviews_count": [
        'button[jsaction="pane.reviewChart.moreReviews"] span',
        'span[aria-label*="review"]',
        'button[aria-label*="review"] span',
    ],
    "reviews_average": [
        'div[jsaction="pane.reviewChart.moreReviews"] div[role="img"]',
        'span[role="img"][aria-label*="star"]',
        'div[role="img"][aria-label*="star"]',
    ],
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


# ---------------------------------------------------------------------------
# Locale-aware parsing helpers
# ---------------------------------------------------------------------------


def parse_rating(text: Optional[str]) -> Optional[float]:
    """Parse a star-rating value from any locale format.

    Handles:
      - "4.5 stars"          (en-US)
      - "4,5 Sterne"         (de-DE)
      - "4・5 つ星"           (ja-JP)
      - "4.5 out of 5"
      - "★ 4.5"
    """
    if not text:
        return None
    # Strip unicode stars/whitespace; first numeric wins
    m = re.search(r"(\d+)\s*[.,\u30FB\uFF65]\s*(\d+)", text)
    if m:
        try:
            return float(f"{m.group(1)}.{m.group(2)}")
        except ValueError:
            pass
    m = re.search(r"(\d+)", text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def parse_review_count(text: Optional[str]) -> Optional[int]:
    """Parse a review-count value with thousands/decimal separators and K/M suffixes.

    Handles:
      - "1,234 reviews"     (en-US thousands)
      - "1.234 Bewertungen" (de-DE thousands)
      - "4,5"               (de-DE decimal — should NOT be a count, but defensively handled)
      - "1.2K reviews"
      - "2.3M reviews"
    """
    if not text:
        return None
    text = text.strip()

    # Strip everything except digits, separators, and K/M suffix
    m = re.match(r"^\s*([\d.,]+)\s*([KkMm]?)\b", text)
    if not m:
        # No leading number — try to find one in the string
        m2 = re.search(r"([\d.,]+\s*[KkMm]?)", text)
        if not m2:
            return None
        return parse_review_count(m2.group(1))

    num_str, suffix = m.groups()
    has_comma = "," in num_str
    has_dot = "." in num_str

    # Normalise separators
    if has_comma and has_dot:
        # Whichever is rightmost is the decimal separator
        if num_str.rfind(",") > num_str.rfind("."):
            num_str = num_str.replace(".", "").replace(",", ".")
        else:
            num_str = num_str.replace(",", "")
    elif has_comma:
        parts = num_str.split(",")
        # 3 digits after the comma => thousands separator (1,234)
        # otherwise => decimal separator (4,5)
        if len(parts[-1]) == 3 and len(parts) > 1:
            num_str = num_str.replace(",", "")
        else:
            num_str = num_str.replace(",", ".")
    elif has_dot:
        parts = num_str.split(".")
        if len(parts[-1]) == 3 and len(parts) > 1:
            # Looks like thousands separator (1.234)
            num_str = num_str.replace(".", "")
        # else already correct decimal

    try:
        value = float(num_str)
    except ValueError:
        return None

    if suffix.upper() == "K":
        value *= 1_000
    elif suffix.upper() == "M":
        value *= 1_000_000

    return int(value)


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


async def _try_selectors(
    page: Page, kind: str, attr: str = "inner_text"
) -> Optional[str]:
    """Try multiple selector candidates, return first non-empty match."""
    for selector in SELECTORS.get(kind, []):
        try:
            count = await page.locator(selector).count()
            if count == 0:
                continue
            if attr == "inner_text":
                value = await page.locator(selector).first.inner_text(timeout=2000)
            elif attr == "aria":
                value = await page.locator(selector).first.get_attribute(
                    "aria-label", timeout=2000
                )
            else:
                value = await page.locator(selector).first.get_attribute(
                    attr, timeout=2000
                )
            if value and value.strip():
                return value.strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Selector %s failed: %s", selector, exc)
    return None


async def _dismiss_consent(page: Page) -> None:
    """Dismiss Google's consent dialog if it appears (mostly EU locales)."""
    try:
        for selector in SELECTORS["consent_accept"]:
            try:
                if await page.locator(selector).count() > 0:
                    await page.locator(selector).first.click(timeout=2000)
                    await page.wait_for_timeout(random.randint(800, 1500))
                    logger.info("Dismissed consent dialog via %s", selector)
                    return
            except Exception:  # noqa: BLE001
                continue
    except Exception as exc:  # noqa: BLE001
        logger.debug("Consent dismissal skipped: %s", exc)


async def humanize_scroll(page: Page, target: int, max_rounds: int = 150) -> int:
    """Scroll the listings panel in a human-like pattern until ``target`` items are loaded.

    Google Maps results live in a scrollable sidebar — if the mouse isn't hovering over
    that panel, ``mouse.wheel`` scrolls the map (or nothing). So we:

    1. Locate the actual scrollable container
    2. Hover the cursor over it
    3. Scroll that container directly via JS (most reliable)
    4. Fall back to mouse.wheel + keyboard if JS scroll fails

    Returns the actual number of listing links currently on the page.
    """
    listings_selector = SELECTORS["listings"][0]
    previous_count = 0
    no_change_rounds = 0

    # Try to find the scrollable panel container
    # Google Maps uses several possible selectors across redesigns
    panel_selectors = [
        'div[role="feed"]',                              # current design (2024+)
        'div[aria-label*="Results"]',
        'div.m6QErb.DxyBCb',                             # legacy class
        'div.m6QErb[aria-label]',                        # generic scrollable results
        'div[aria-label*="results" i]',
        'div.section-scrollbox',                         # legacy
        'div[role="region"]',
    ]

    panel_handle = None
    for sel in panel_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                # Make sure it's actually scrollable (scrollHeight > clientHeight)
                is_scrollable = await loc.evaluate(
                    """el => el.scrollHeight > el.clientHeight + 10"""
                )
                if is_scrollable:
                    panel_handle = loc
                    logger.info("Using panel selector: %s", sel)
                    break
        except Exception:  # noqa: BLE001
            continue

    # Always hover the cursor over the panel before wheel-scrolling
    if panel_handle is not None:
        try:
            await panel_handle.hover()
            await page.wait_for_timeout(200)
        except Exception:  # noqa: BLE001
            pass
    else:
        # Fallback: hover the first listing link
        try:
            await page.locator(listings_selector).first.hover()
            await page.wait_for_timeout(200)
        except Exception:  # noqa: BLE001
            pass

    for _ in range(max_rounds):
        current = await page.locator(listings_selector).count()

        if current >= target:
            logger.info("Reached target: %d listings", current)
            return current

        if current == previous_count:
            no_change_rounds += 1
            if no_change_rounds >= 10:
                logger.info("No more new listings after %d rounds (%d total)", no_change_rounds, current)
                return current
        else:
            no_change_rounds = 0
            logger.debug("Loaded %d listings so far", current)

        # Strategy 1: scroll the panel container directly via JS (most reliable)
        scrolled = False
        if panel_handle is not None:
            try:
                await panel_handle.evaluate(
                    """el => { el.scrollBy({ top: 800, behavior: 'instant' }); }"""
                )
                scrolled = True
            except Exception:  # noqa: BLE001
                pass

        # Strategy 2: mouse.wheel if JS scroll didn't apply
        if not scrolled:
            try:
                # Re-hover in case the cursor drifted
                if panel_handle is not None:
                    await panel_handle.hover()
                # Multiple small wheel events (more human-like, registers more reliably)
                for _ in range(random.randint(6, 10)):
                    await page.mouse.wheel(0, random.randint(300, 700))
                    await page.wait_for_timeout(random.randint(180, 380))
            except Exception:  # noqa: BLE001
                pass

        # Strategy 3: keyboard End/PageDown on the panel (works in some Google Maps versions)
        if panel_handle is not None:
            try:
                await panel_handle.focus()
                await page.keyboard.press("End")
                await page.wait_for_timeout(150)
            except Exception:  # noqa: BLE001
                pass

        await page.wait_for_timeout(random.uniform(1400, 2400))

        previous_count = current

    return previous_count


async def extract_business_details(page: Page) -> Optional[Business]:
    """Extract structured fields from the currently-open business side-panel."""
    # Give the panel a moment to settle after click
    await page.wait_for_timeout(random.randint(1500, 2500))

    business = Business()
    business.name = await _try_selectors(page, "name")
    business.address = await _try_selectors(page, "address")
    business.website = await _try_selectors(page, "website")
    business.phone_number = await _try_selectors(page, "phone")
    business.category = await _try_selectors(page, "category")

    rating_text = await _try_selectors(page, "reviews_average", attr="aria")
    business.reviews_average = parse_rating(rating_text) if rating_text else None

    count_text = await _try_selectors(page, "reviews_count")
    business.reviews_count = parse_review_count(count_text) if count_text else None

    business.google_maps_url = page.url

    # Try to extract coordinates from the URL (?ll= or @lat,lng)
    if business.google_maps_url:
        m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", business.google_maps_url)
        if m:
            try:
                business.latitude = float(m.group(1))
                business.longitude = float(m.group(2))
            except ValueError:
                pass

    return business


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


ProgressCallback = Callable[[int, int], Awaitable[None]]


async def scrape_businesses(
    search_term: str,
    total: int = 30,
    headless: bool = True,
    locale: str = "en",
    progress_callback: Optional[ProgressCallback] = None,
    max_retries: int = 3,
) -> BusinessList:
    """Scrape up to ``total`` businesses matching ``search_term``.

    Args:
        search_term: e.g. "Coffee Shops in New York"
        total: cap on number of results to scrape
        headless: True to run Chromium headless; False for real visible window
        locale: BCP-47 language tag, e.g. "en", "de", "ja"
        progress_callback: optional async ``(current, total)`` hook for live UI
        max_retries: retry attempts for network failures
    """
    search_term = search_term.strip()
    if not search_term:
        raise ValueError("search_term cannot be empty")
    if total < 1:
        raise ValueError("total must be >= 1")

    business_list = BusinessList()

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )

        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1366, "height": 900},
            locale=locale,
            timezone_id="America/New_York",
            ignore_https_errors=True,
        )

        # Anti-bot: hide webdriver flag + spoof common fingerprint signals
        # Google's modern anti-bot checks Canvas, WebGL, AudioContext, etc.
        # Note: full bypass requires botasaurus-driver (see `botasaurus_backend.py`).
        # These init scripts improve things incrementally for plain Playwright.
        await context.add_init_script(
            """
            // Hide webdriver
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            // Spoof languages (don't leak automation's default 'en-US')
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            // Spoof plugins length (headless = 0; real browsers have more)
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
            window.chrome = { runtime: {} };
            """
        )

        page = await context.new_page()

        try:
            # ---- 1. Navigate -------------------------------------------------
            for attempt in range(max_retries):
                try:
                    await page.goto("https://www.google.com/maps", timeout=60_000)
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.warning("goto attempt %d failed: %s", attempt + 1, exc)
                    if attempt == max_retries - 1:
                        raise
                    await asyncio.sleep(2 ** attempt)

            await _dismiss_consent(page)
            await page.wait_for_timeout(random.randint(1200, 2200))

            # ---- 2. Search ---------------------------------------------------
            # Try each known search-input selector until one works
            filled = False
            for selector in SELECTORS["search_input"]:
                try:
                    if await page.locator(selector).count() > 0:
                        await page.locator(selector).first.fill(search_term)
                        filled = True
                        break
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Search selector %s failed: %s", selector, exc)

            if not filled:
                # Fallback: click the "Search" toolbar button to reveal the input
                try:
                    await page.locator('button[aria-label="Search"]').first.click()
                    await page.wait_for_timeout(800)
                    for selector in SELECTORS["search_input"]:
                        if await page.locator(selector).count() > 0:
                            await page.locator(selector).first.fill(search_term)
                            filled = True
                            break
                except Exception:  # noqa: BLE001
                    pass

            if not filled:
                raise RuntimeError(
                    "Could not find the Google Maps search input. "
                    "Google may have redesigned the page — check the SELECTORS in scraper.py."
                )

            await page.wait_for_timeout(random.randint(800, 1600))
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(random.randint(3500, 5500))

            # ---- 3. Scroll to load enough listings ---------------------------
            await humanize_scroll(page, total)
            if progress_callback:
                await progress_callback(0, total)

            # ---- 4. Collect and click each listing --------------------------
            listings_selector = SELECTORS["listings"][0]
            all_listings = await page.locator(listings_selector).all()
            listings = all_listings[:total]
            logger.info("Processing %d listings", len(listings))

            if not listings:
                logger.warning("No listings found for: %s", search_term)

            for idx, listing in enumerate(listings):
                try:
                    await listing.scroll_into_view_if_needed(timeout=3000)
                    await listing.click(timeout=5000)
                    details = await extract_business_details(page)
                    if details:
                        business_list.add(details)

                    if progress_callback:
                        await progress_callback(idx + 1, len(listings))

                    # Tiny breath between cards to avoid hammering
                    await page.wait_for_timeout(random.randint(400, 900))

                except Exception as exc:  # noqa: BLE001
                    logger.error("Listing %d failed: %s", idx, exc)
                    continue

        except Exception as exc:  # noqa: BLE001
            logger.error("Scrape aborted: %s", exc, exc_info=True)
            raise
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass

    return business_list
