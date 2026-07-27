"""
MapLead — API-based scraper backends
====================================

Drop-in replacements for the Playwright engine. Use these when:

- You're scraping at scale (100s+ queries/day)
- You need reliability (no CAPTCHAs, no bot detection)
- You have a small budget ($1-50/mo gets you a LOT of data)

Backends
--------
1. Outscraper — https://outscraper.com
   • Free tier: ~100 records/month (no credit card)
   • Paid:      ~$1 per 1000 records
   • Best Google Maps coverage, returns rich fields

2. SerpApi — https://serpapi.com
   • $50/mo for 5000 searches, then $0.01/search
   • Also supports Bing, Yahoo, Yelp, etc.

Switch via the SCRAPER_BACKEND env var or the UI dropdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional, Protocol

import httpx

from scraper import Business, BusinessList, ProgressCallback

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------


class ScraperBackend(Protocol):
    """All backends must implement this async interface."""

    name: str
    requires_api_key: bool

    async def scrape(
        self,
        search_term: str,
        total: int = 30,
        progress_callback: Optional[ProgressCallback] = None,
        **kwargs: Any,
    ) -> BusinessList: ...


# ---------------------------------------------------------------------------
# Outscraper
# ---------------------------------------------------------------------------


class OutscraperBackend:
    """Outscraper Google Maps API backend.

    Get a key:  https://app.outscraper.com/profile
    Free tier:  ~100 records/month (no credit card)
    """

    name = "Outscraper"
    requires_api_key = True

    BASE_URL = "https://api.app.outscraper.com/maps/search-v3"
    DEFAULT_TIMEOUT = 300.0  # large queries can take a while

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.environ.get("OUTSCRAPER_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "Outscraper API key missing. "
                "Set OUTSCRAPER_API_KEY or pass api_key=..."
            )

    async def scrape(
        self,
        search_term: str,
        total: int = 30,
        progress_callback: Optional[ProgressCallback] = None,
        **kwargs: Any,
    ) -> BusinessList:
        params = {
            "query": search_term,
            "limit": min(total, 500),  # Outscraper hard cap per call
            "language": kwargs.get("locale", "en"),
            "region": kwargs.get("country", "US"),
            "skip": 0,
            "async": "false",  # synchronous request; Outscraper does the polling
        }
        headers = {"X-API-KEY": self.api_key}

        logger.info("Outscraper request: %s (limit=%d)", search_term, params["limit"])

        async with httpx.AsyncClient(timeout=self.DEFAULT_TIMEOUT) as client:
            response = await client.get(self.BASE_URL, params=params, headers=headers)

        if response.status_code == 401:
            raise PermissionError("Outscraper API key invalid or expired")
        if response.status_code == 402:
            raise PermissionError("Outscraper account out of credits")
        response.raise_for_status()
        data = response.json()

        # Outscraper wraps its result: {"data": [[ {...}, {...} ]]}
        results = data.get("data", [])
        if results and isinstance(results[0], list):
            results = results[0]

        logger.info("Outscraper returned %d results", len(results))

        business_list = BusinessList()
        for idx, item in enumerate(results):
            business_list.add(self._parse(item))
            if progress_callback:
                await progress_callback(idx + 1, len(results))

        return business_list

    @staticmethod
    def _parse(item: dict) -> Business:
        coords = item.get("coordinates") or {}
        lat = item.get("latitude") or coords.get("lat")
        lng = item.get("longitude") or coords.get("lng")
        place_id = item.get("place_id") or item.get("google_id")
        url = (
            f"https://www.google.com/maps/place/?q=place_id:{place_id}"
            if place_id
            else None
        )
        return Business(
            name=item.get("name"),
            address=item.get("full_address") or item.get("address"),
            website=item.get("site") or item.get("website"),
            phone_number=item.get("phone") or item.get("phone_number"),
            category=item.get("category") or item.get("type"),
            reviews_average=_safe_float(item.get("rating")),
            reviews_count=_safe_int(item.get("reviews") or item.get("reviews_count")),
            latitude=_safe_float(lat),
            longitude=_safe_float(lng),
            google_maps_url=url,
        )


# ---------------------------------------------------------------------------
# SerpApi
# ---------------------------------------------------------------------------


class SerpApiBackend:
    """SerpApi Google Maps engine backend.

    Get a key:  https://serpapi.com/manage-api-key
    Pricing:    $50/mo for 5000 searches, then $0.01/search
    """

    name = "SerpApi"
    requires_api_key = True

    BASE_URL = "https://serpapi.com/search.json"
    PER_PAGE = 20  # SerpApi's max per page for local_results
    DEFAULT_TIMEOUT = 60.0

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.environ.get("SERPAPI_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "SerpApi API key missing. Set SERPAPI_API_KEY or pass api_key=..."
            )

    async def scrape(
        self,
        search_term: str,
        total: int = 30,
        progress_callback: Optional[ProgressCallback] = None,
        **kwargs: Any,
    ) -> BusinessList:
        business_list = BusinessList()
        start = 0

        async with httpx.AsyncClient(timeout=self.DEFAULT_TIMEOUT) as client:
            while start < total:
                params = {
                    "engine": "google_maps",
                    "q": search_term,
                    "start": start,
                    "api_key": self.api_key,
                    "hl": kwargs.get("locale", "en"),
                    "gl": kwargs.get("country", "us"),
                }
                logger.info("SerpApi request: %s (start=%d)", search_term, start)

                response = await client.get(self.BASE_URL, params=params)
                response.raise_for_status()
                data = response.json()

                if error := data.get("error"):
                    raise RuntimeError(f"SerpApi error: {error}")

                results = data.get("local_results") or data.get("places_results") or []
                if not results:
                    break

                for item in results:
                    business_list.add(self._parse(item))
                    if progress_callback:
                        await progress_callback(
                            len(business_list.business_list), total
                        )

                if len(results) < self.PER_PAGE:
                    break  # no more pages
                start += self.PER_PAGE

        logger.info("SerpApi returned %d results", len(business_list))
        return business_list

    @staticmethod
    def _parse(item: dict) -> Business:
        gps = item.get("gps_coordinates") or {}
        return Business(
            name=item.get("title"),
            address=item.get("address"),
            website=item.get("website"),
            phone_number=item.get("phone"),
            category=item.get("type"),
            reviews_average=_safe_float(item.get("rating")),
            reviews_count=_safe_int(item.get("reviews")),
            latitude=_safe_float(gps.get("latitude")),
            longitude=_safe_float(gps.get("longitude")),
            google_maps_url=item.get("link"),
        )


# ---------------------------------------------------------------------------
# Playwright adapter (wraps the original function for the factory)
# ---------------------------------------------------------------------------


class PlaywrightBackend:
    """Adapter that exposes the original Playwright scrape_businesses as a ScraperBackend."""

    name = "Playwright (free, self-hosted)"
    requires_api_key = False

    def __init__(self) -> None:
        from scraper import scrape_businesses  # local import to avoid loading Playwright when not used

        self._scrape_fn = scrape_businesses

    async def scrape(
        self,
        search_term: str,
        total: int = 30,
        progress_callback: Optional[ProgressCallback] = None,
        **kwargs: Any,
    ) -> BusinessList:
        return await self._scrape_fn(
            search_term=search_term,
            total=total,
            headless=kwargs.get("headless", True),
            locale=kwargs.get("locale", "en"),
            progress_callback=progress_callback,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


_BACKEND_REGISTRY: dict[str, type[ScraperBackend]] = {
    "playwright": PlaywrightBackend,
    "outscraper": OutscraperBackend,
    "serpapi": SerpApiBackend,
    "out": OutscraperBackend,  # alias
    "serp": SerpApiBackend,  # alias
}


def get_backend(name: Optional[str] = None) -> ScraperBackend:
    """Pick a backend by name, env var, or default to playwright."""
    name = (
        name
        or os.environ.get("SCRAPER_BACKEND", "playwright")
    ).lower().strip()

    cls = _BACKEND_REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown backend '{name}'. "
            f"Choices: {sorted(set(_BACKEND_REGISTRY.keys()))}"
        )

    logger.info("Using scraper backend: %s", cls.__name__)
    return cls()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
