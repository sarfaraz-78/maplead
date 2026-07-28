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
# OpenStreetMap (via Overpass + Nominatim) — 100% free, no API key, no signup
# ---------------------------------------------------------------------------


# Map of common business keywords → OSM amenity/shop tags
# https://wiki.openstreetmap.org/wiki/Key:amenity
_OSM_TAG_MAP: dict[str, list[tuple[str, str]]] = {
    # keyword → list of (key, value) tag filters
    "restaurant": [("amenity", "restaurant")],
    "restaurants": [("amenity", "restaurant")],
    "cafe": [("amenity", "cafe")],
    "cafes": [("amenity", "cafe")],
    "coffee": [("amenity", "cafe")],
    "coffee shop": [("amenity", "cafe")],
    "coffee shops": [("amenity", "cafe")],
    "bar": [("amenity", "bar")],
    "bars": [("amenity", "bar")],
    "pub": [("amenity", "pub")],
    "pubs": [("amenity", "pub")],
    "hotel": [("tourism", "hotel")],
    "hotels": [("tourism", "hotel")],
    "pharmacy": [("amenity", "pharmacy")],
    "pharmacies": [("amenity", "pharmacy")],
    "hospital": [("amenity", "hospital")],
    "hospitals": [("amenity", "hospital")],
    "clinic": [("amenity", "clinic")],
    "doctor": [("amenity", "doctors")],
    "dentist": [("amenity", "dentist")],
    "bank": [("amenity", "bank")],
    "banks": [("amenity", "bank")],
    "atm": [("amenity", "atm")],
    "atms": [("amenity", "atm")],
    "supermarket": [("shop", "supermarket")],
    "supermarkets": [("shop", "supermarket")],
    "grocery": [("shop", "supermarket"), ("shop", "convenience")],
    "bakery": [("shop", "bakery")],
    "bakeries": [("shop", "bakery")],
    "butcher": [("shop", "butcher")],
    "florist": [("shop", "florist")],
    "florists": [("shop", "florist")],
    "bookstore": [("shop", "books")],
    "bookstores": [("shop", "books")],
    "clothing": [("shop", "clothes")],
    "shoe": [("shop", "shoes")],
    "shoes": [("shop", "shoes")],
    "jewelry": [("shop", "jewelry")],
    "jewellery": [("shop", "jewelry")],
    "hairdresser": [("shop", "hairdresser")],
    "salon": [("shop", "hairdresser")],
    "beauty": [("shop", "beauty")],
    "car repair": [("shop", "car_repair")],
    "auto repair": [("shop", "car_repair")],
    "gas station": [("amenity", "fuel")],
    "petrol": [("amenity", "fuel")],
    "petrol pump": [("amenity", "fuel")],
    "parking": [("amenity", "parking")],
    "school": [("amenity", "school")],
    "schools": [("amenity", "school")],
    "university": [("amenity", "university")],
    "gym": [("leisure", "fitness_centre")],
    "fitness": [("leisure", "fitness_centre")],
    "museum": [("tourism", "museum")],
    "museums": [("tourism", "museum")],
    "library": [("amenity", "library")],
    "libraries": [("amenity", "library")],
    "church": [("amenity", "place_of_worship")],
    "plumber": [("craft", "plumber")],
    "plumbers": [("craft", "plumber")],
    "electrician": [("craft", "electrician")],
    "lawyer": [("office", "lawyer")],
    "accountant": [("office", "accountant")],
    "real estate": [("office", "estate_agent")],
    # ──────── India-specific categories ────────
    "chai": [("amenity", "cafe")],
    "tea stall": [("amenity", "cafe")],
    "tea shop": [("amenity", "cafe")],
    "dhaba": [("amenity", "restaurant")],
    "kirana": [("shop", "convenience")],
    "kirana store": [("shop", "convenience")],
    "general store": [("shop", "convenience")],
    "medical": [("amenity", "pharmacy")],
    "medical store": [("amenity", "pharmacy")],
    "chemist": [("amenity", "pharmacy")],
    "provision store": [("shop", "convenience")],
    "sweet shop": [("shop", "confectionery")],
    "sweets": [("shop", "confectionery")],
    "mithai": [("shop", "confectionery")],
    "juice": [("amenity", "cafe")],
    "juice center": [("amenity", "cafe")],
    "tiffin": [("amenity", "restaurant")],
    "tiffin center": [("amenity", "restaurant")],
    "veg restaurant": [("amenity", "restaurant")],
    "non veg": [("amenity", "restaurant")],
    "fast food": [("amenity", "fast_food")],
    "street food": [("amenity", "fast_food")],
    "ice cream": [("amenity", "ice_cream")],
    "ice cream parlor": [("amenity", "ice_cream")],
    "auto": [("amenity", "taxi")],
    "auto stand": [("amenity", "taxi")],
    "rickshaw": [("amenity", "taxi")],
    "car rental": [("amenity", "car_rental")],
    "car hire": [("amenity", "car_rental")],
    "travel": [("office", "travel_agent")],
    "travel agent": [("office", "travel_agent")],
    "courier": [("amenity", "post_office")],
    "xerox": [("shop", "stationery")],
    "photocopy": [("shop", "stationery")],
    "cyber cafe": [("amenity", "internet_cafe")],
    "mobile": [("shop", "mobile_phone")],
    "mobile shop": [("shop", "mobile_phone")],
    "mobile repair": [("shop", "mobile_phone")],
    "saree": [("shop", "clothes")],
    "saree shop": [("shop", "clothes")],
    "tailor": [("shop", "tailor")],
    "boutique": [("shop", "clothes")],
    "jewellers": [("shop", "jewelry")],
    "optician": [("shop", "optician")],
    "opticals": [("shop", "optician")],
    "watch": [("shop", "watches")],
    "watch repair": [("shop", "watches")],
    "electrical": [("shop", "electrical")],
    "hardware": [("shop", "hardware")],
    "paint": [("shop", "paint")],
    "tiles": [("shop", "tiles")],
    "tyre": [("shop", "tyres")],
    "tire": [("shop", "tyres")],
    "car wash": [("amenity", "car_wash")],
    "temple": [("amenity", "place_of_worship")],
    "mosque": [("amenity", "place_of_worship")],
    "masjid": [("amenity", "place_of_worship")],
    "gurudwara": [("amenity", "place_of_worship")],
    "coaching": [("amenity", "school")],
    "tuition": [("amenity", "school")],
    "training institute": [("amenity", "school")],
    "it company": [("office", "it")],
    "software": [("office", "it")],
    "it park": [("office", "it")],
    "coworking": [("office", "coworking")],
    "co-working": [("office", "coworking")],
    "mall": [("shop", "mall")],
    "shopping mall": [("shop", "mall")],
}


class OSMBackend:
    """OpenStreetMap via Nominatim + Overpass API.

    ✅ 100% free — no API key, no signup, no browser, no CAPTCHAs
    ✅ Worldwide coverage
    ⚠️  No ratings / reviews (OSM doesn't have them)
    ⚠️  Smaller dataset than Google Maps (~80% coverage for businesses in urban areas)
    ⚠️  Phone/website data is hit-or-miss (depends on volunteer mappers)

    Usage limits (courtesy, not hard limits):
        Nominatim: 1 request/second
        Overpass:  ~10 GB/day per IP

    Best for: "give me a list of <business type> in <city>" where you need
    name, address, lat/lon — not Google-quality reviews data.
    """

    name = "OpenStreetMap (free, no key)"
    requires_api_key = False

    NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
    OVERPASS_URLS = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.osm.ch/api/interpreter",
    ]
    DEFAULT_TIMEOUT = 60.0

    def __init__(self) -> None:
        # OSM requires a User-Agent per their usage policy
        self._headers = {
            "User-Agent": "MapLead/1.0 (https://github.com/sabsar42/Google-Map-Scrapper-Streamlit-Web)",
        }

    async def scrape(
        self,
        search_term: str,
        total: int = 30,
        progress_callback: Optional[ProgressCallback] = None,
        **kwargs: Any,
    ) -> BusinessList:
        import urllib.parse

        # Split "coffee shops in Brooklyn" → category="coffee shops", location="Brooklyn"
        category, location = self._parse_query(search_term)
        if not location:
            raise ValueError(
                "OpenStreetMap backend requires a location. "
                'Use a query like "coffee shops in Brooklyn" — not just "coffee".'
            )

        logger.info("OSM query: category=%r location=%r", category, location)

        async with httpx.AsyncClient(
            timeout=self.DEFAULT_TIMEOUT, headers=self._headers
        ) as client:
            # 1. Geocode the location → bounding box
            bbox = await self._geocode(client, location)
            if bbox is None:
                raise RuntimeError(f"Could not geocode location: {location!r}")

            # 2. Resolve category → OSM tags
            tags = self._resolve_category(category)
            if not tags:
                # Fallback: free-text name search within bbox
                results = await self._overpass_name_search(
                    client, bbox, category, total
                )
            else:
                # 3. Query Overpass for matching businesses
                results = await self._overpass_tag_search(client, bbox, tags, total)

        business_list = BusinessList()
        for idx, item in enumerate(results):
            business_list.add(self._parse_osm(item))
            if progress_callback:
                await progress_callback(idx + 1, len(results))

        logger.info("OSM returned %d results", len(business_list))
        return business_list

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _parse_query(query: str) -> tuple[str, str]:
        """Split 'coffee shops in Brooklyn, NY' → ('coffee shops', 'Brooklyn, NY')."""
        query = query.strip()
        for sep in (" in ", " near ", " around "):
            if sep in query.lower():
                idx = query.lower().find(sep)
                category = query[:idx].strip()
                location = query[idx + len(sep):].strip()
                return category, location
        # No location found
        return query, ""

    async def _geocode(
        self, client: httpx.AsyncClient, location: str
    ) -> Optional[tuple[float, float, float, float]]:
        """Return (south, west, north, east) bounding box for a place name."""
        params = {
            "q": location,
            "format": "json",
            "limit": 1,
            "addressdetails": 0,
        }
        try:
            r = await client.get(self.NOMINATIM_URL, params=params)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("Nominatim geocode failed: %s", exc)
            return None

        if not data:
            return None

        bbox = data[0].get("boundingbox")
        if not bbox or len(bbox) != 4:
            return None
        # Nominatim returns [south, north, west, east] as strings
        return tuple(float(x) for x in bbox)  # type: ignore[return-value]

    @staticmethod
    def _resolve_category(category: str) -> list[tuple[str, str]]:
        """Map a free-text category to OSM tag filters."""
        cat = category.lower().strip()

        # Exact match
        if cat in _OSM_TAG_MAP:
            return _OSM_TAG_MAP[cat]

        # Substring match (longest first)
        for key in sorted(_OSM_TAG_MAP.keys(), key=len, reverse=True):
            if key in cat:
                return _OSM_TAG_MAP[key]

        return []

    async def _overpass_tag_search(
        self,
        client: httpx.AsyncClient,
        bbox: tuple[float, float, float, float],
        tags: list[tuple[str, str]],
        limit: int,
    ) -> list[dict]:
        """Query Overpass for nodes + ways + relations matching any of the tag pairs."""
        south, north, west, east = bbox
        bbox_str = f"{south},{west},{north},{east}"

        # Build union of tag queries: node["amenity"="cafe"](bbox); way["amenity"="cafe"](bbox); ...
        type_filters = []
        for key, value in tags:
            for osm_type in ("node", "way", "relation"):
                type_filters.append(
                    f'{osm_type}["{key}"="{value}"]({bbox_str});'
                )

        query = f"""
[out:json][timeout:25];
(
  {chr(10).join("  " + f for f in type_filters)}
);
out center tags {limit};
"""
        return await self._run_overpass(client, query)

    async def _overpass_name_search(
        self,
        client: httpx.AsyncClient,
        bbox: tuple[float, float, float, float],
        name: str,
        limit: int,
    ) -> list[dict]:
        """Fallback: search by name substring within bbox."""
        south, north, west, east = bbox
        bbox_str = f"{south},{west},{north},{east}"
        query = f"""
[out:json][timeout:25];
(
  node["name"~"{name}", i]({bbox_str});
  way["name"~"{name}", i]({bbox_str});
  relation["name"~"{name}", i]({bbox_str});
);
out center tags {limit};
"""
        return await self._run_overpass(client, query)

    async def _run_overpass(
        self, client: httpx.AsyncClient, query: str
    ) -> list[dict]:
        """POST the query to Overpass with multi-endpoint fallback."""
        last_error: Optional[Exception] = None
        for url in self.OVERPASS_URLS:
            try:
                r = await client.post(url, data={"data": query})
                if r.status_code == 429:
                    logger.warning("Overpass rate-limited on %s, trying next", url)
                    await asyncio.sleep(1)
                    continue
                r.raise_for_status()
                data = r.json()
                return data.get("elements", [])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Overpass %s failed: %s", url, exc)
                last_error = exc
                continue
        if last_error:
            raise last_error
        return []

    @staticmethod
    def _parse_osm(item: dict) -> Business:
        tags = item.get("tags") or {}
        # For ways/relations, "center" holds lat/lon; for nodes, "lat"/"lon"
        lat = item.get("lat") or (item.get("center") or {}).get("lat")
        lon = item.get("lon") or (item.get("center") or {}).get("lon")

        # Build address from addr:* tags
        addr_parts = [
            tags.get("addr:housenumber"),
            tags.get("addr:street"),
            tags.get("addr:suburb"),
            tags.get("addr:city"),
            tags.get("addr:state"),
            tags.get("addr:postcode"),
            tags.get("addr:country"),
        ]
        address = ", ".join(p for p in addr_parts if p) or None

        # OSM has no ratings/reviews
        return Business(
            name=tags.get("name") or tags.get("name:en"),
            address=address,
            website=tags.get("website") or tags.get("contact:website"),
            phone_number=tags.get("phone") or tags.get("contact:phone"),
            category=tags.get("amenity") or tags.get("shop") or tags.get("tourism") or tags.get("craft") or tags.get("office") or tags.get("leisure"),
            reviews_average=None,
            reviews_count=None,
            latitude=_safe_float(lat),
            longitude=_safe_float(lon),
            google_maps_url=None,
        )


# ---------------------------------------------------------------------------
# Yelp Fusion
# ---------------------------------------------------------------------------


class YelpBackend:
    """Yelp Fusion API backend.

    Free tier: 5,000 API calls/day = ~150,000 businesses/month.
    No credit card needed for free tier. Best cost-vs-data ratio.

    Sign up:  https://www.yelp.com/developers/v3/manage_app
    Docs:     https://www.yelp.com/developers/documentation/v3/business_search

    Returns: name, address, phone, Yelp URL, rating, review_count,
             category, lat/lon, price tier.

    Limitations:
    - Only returns businesses that have a Yelp listing
    - Max 50 results per page, 1000 per query (pagination via offset)
    - ``url`` field is the Yelp page, not the business's own website
    """

    name = "Yelp Fusion"
    requires_api_key = True

    BASE_URL = "https://api.yelp.com/v3/businesses/search"
    PER_PAGE = 50  # Yelp's max per page
    MAX_OFFSET = 1000  # Yelp's max offset (50 * 20 pages)
    DEFAULT_TIMEOUT = 30.0

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.environ.get("YELP_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "Yelp API key missing. "
                "Set YELP_API_KEY or pass api_key=...  "
                "Get a free key at https://www.yelp.com/developers/v3/manage_app"
            )

    async def scrape(
        self,
        search_term: str,
        total: int = 30,
        progress_callback: Optional[ProgressCallback] = None,
        **kwargs: Any,
    ) -> BusinessList:
        term, location = self._parse_query(search_term)
        if not location:
            raise ValueError(
                "Yelp backend needs a location. "
                'Use a query like "plumbers in Brooklyn" — not just "plumbers".'
            )

        logger.info("Yelp search: term=%r location=%r total=%d", term, location, total)

        headers = {"Authorization": f"Bearer {self.api_key}"}
        business_list = BusinessList()
        offset = 0

        async with httpx.AsyncClient(timeout=self.DEFAULT_TIMEOUT) as client:
            while len(business_list.business_list) < total and offset < self.MAX_OFFSET:
                page_size = min(self.PER_PAGE, total - len(business_list.business_list))
                params = {
                    "term": term,
                    "location": location,
                    "limit": page_size,
                    "offset": offset,
                    "sort_by": "best_match",
                }
                # Map locale (en, de, ja...) to Yelp's ``locale`` param where supported
                locale = kwargs.get("locale", "en")
                yelp_locale = self._map_locale(locale)
                if yelp_locale:
                    params["locale"] = yelp_locale

                response = await client.get(
                    self.BASE_URL, params=params, headers=headers
                )

                if response.status_code == 401:
                    raise PermissionError("Yelp API key invalid or revoked")
                if response.status_code == 429:
                    raise PermissionError(
                        "Yelp rate limit hit (5,000 calls/day). "
                        "Wait until tomorrow or upgrade."
                    )
                response.raise_for_status()
                data = response.json()

                results = data.get("businesses", [])
                if not results:
                    break

                for item in results:
                    business_list.add(self._parse(item))
                    if progress_callback:
                        await progress_callback(
                            len(business_list.business_list), total
                        )
                    if len(business_list.business_list) >= total:
                        break

                if len(results) < page_size:
                    break  # last page

                offset += self.PER_PAGE

        logger.info("Yelp returned %d results", len(business_list))
        return business_list

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _parse_query(query: str) -> tuple[str, str]:
        """Split 'plumbers in Brooklyn' → ('plumbers', 'Brooklyn')."""
        query = query.strip()
        for sep in (" in ", " near ", " around "):
            lower = query.lower()
            if sep in lower:
                idx = lower.find(sep)
                return query[:idx].strip(), query[idx + len(sep):].strip()
        return query, ""

    @staticmethod
    def _map_locale(locale: str) -> Optional[str]:
        """Map our locale codes to Yelp's supported locales."""
        # Yelp supports: cs_CZ, da_DK, de_AT, de_CH, de_DE, en_AU, en_BE,
        # en_CA, en_CH, en_GB, en_HK, en_IE, en_MY, en_NZ, en_PH, en_SG,
        # en_US, en_ZA, es_AR, es_CL, es_ES, es_MX, es_VE, fr_BE, fr_CA,
        # fr_CH, fr_FR, it_CH, it_IT, ja_JP, nl_BE, nl_NL, pl_PL, pt_BR,
        # pt_PT, sv_FI, sv_SE, tr_TR, zh_CN, zh_HK, zh_TW
        mapping = {
            "en": "en_US",
            "en-GB": "en_GB",
            "de": "de_DE",
            "fr": "fr_FR",
            "es": "es_ES",
            "it": "it_IT",
            "ja": "ja_JP",
            "ko": None,  # not supported by Yelp
            "pt-BR": "pt_BR",
            "zh-CN": "zh_CN",
            "ar": None,  # not supported
        }
        return mapping.get(locale)

    @staticmethod
    def _parse(item: dict) -> Business:
        loc = item.get("location") or {}
        coords = item.get("coordinates") or {}
        cats = item.get("categories") or []
        category = cats[0].get("title") if cats else None

        # Build full address from display_address if available
        address = None
        if loc.get("display_address"):
            address = ", ".join(loc["display_address"])
        elif loc:
            parts = [
                loc.get("address1"),
                loc.get("city"),
                loc.get("state"),
                loc.get("zip_code"),
            ]
            address = ", ".join(p for p in parts if p) or None

        return Business(
            name=item.get("name"),
            address=address,
            # Yelp returns the Yelp page URL; the business's own website isn't in v3
            website=item.get("url"),
            phone_number=item.get("display_phone") or item.get("phone"),
            category=category,
            reviews_average=_safe_float(item.get("rating")),
            reviews_count=_safe_int(item.get("review_count")),
            latitude=_safe_float(coords.get("latitude")),
            longitude=_safe_float(coords.get("longitude")),
            google_maps_url=None,
        )


# ---------------------------------------------------------------------------
# Apify-backed JustDial & IndiaMART scrapers
# ---------------------------------------------------------------------------


class ApifyBackend:
    """Generic Apify actor runner.

    Calls an Apify actor, waits for completion, and returns the dataset
    items as Business objects. Subclasses configure the actor ID, input
    shape, and result parser.

    Pricing model: actors on Apify charge per-event (per-result). The user
    pays Apify directly; we never touch their card. They get $5/month
    free credit which covers ~2,500 free leads.

    Sign up: https://console.apify.com/
    Get key: Settings -> Integrations -> Personal API tokens
    """

    BASE_URL = "https://api.apify.com/v2"
    POLL_INTERVAL = 4  # seconds between status checks
    MAX_WAIT = 600  # 10 min total

    ACTOR_ID: str = ""
    RESULT_PARSER = staticmethod(lambda item: Business())

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.environ.get("APIFY_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "Apify API key missing. "
                "Set APIFY_API_KEY or pass api_key=...  "
                "Get a free one at https://console.apify.com/"
            )

    def _build_input(self, search_term: str, total: int, **kwargs) -> dict:
        """Override in subclasses to define actor-specific input."""
        return {"search": search_term, "maxItems": total}

    async def scrape(
        self,
        search_term: str,
        total: int = 30,
        progress_callback: Optional[ProgressCallback] = None,
        **kwargs: Any,
    ) -> BusinessList:
        actor_input = self._build_input(search_term, total, **kwargs)
        headers = {"Authorization": f"Bearer {self.api_key}"}

        logger.info("Apify run: actor=%s input=%s", self.ACTOR_ID, actor_input)

        async with httpx.AsyncClient(timeout=60) as client:
            # Start the run
            response = await client.post(
                f"{self.BASE_URL}/acts/{self.ACTOR_ID}/runs",
                json=actor_input,
                headers=headers,
                params={"waitForFinish": str(self.MAX_WAIT // 1000)},  # seconds
            )
            if response.status_code == 401:
                raise PermissionError("Apify API token invalid or revoked")
            if response.status_code == 402:
                raise PermissionError(
                    "Apify account out of credits. "
                    "Top up at https://console.apify.com/billing"
                )
            response.raise_for_status()
            run_data = response.json()["data"]
            dataset_id = run_data.get("defaultDatasetId")
            run_status = run_data.get("status")
            run_id = run_data.get("id")

            # If not finished yet (waitForFinish timed out), poll
            wait = 0
            while run_status not in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT") and wait < self.MAX_WAIT:
                await asyncio.sleep(self.POLL_INTERVAL)
                wait += self.POLL_INTERVAL
                status_response = await client.get(
                    f"{self.BASE_URL}/actor-runs/{run_id}", headers=headers
                )
                status_response.raise_for_status()
                run_status = status_response.json()["data"]["status"]

            if run_status != "SUCCEEDED":
                raise RuntimeError(
                    f"Apify actor run ended with status: {run_status}. "
                    f"Check https://console.apify.com/actors/runs/{run_id}"
                )

            # Fetch dataset items
            items_response = await client.get(
                f"{self.BASE_URL}/datasets/{dataset_id}/items",
                headers=headers,
                params={"limit": total},
            )
            items_response.raise_for_status()
            items = items_response.json()

        logger.info("Apify actor returned %d items", len(items))

        business_list = BusinessList()
        for idx, item in enumerate(items[:total]):
            business_list.add(self.RESULT_PARSER(item))
            if progress_callback:
                await progress_callback(idx + 1, min(len(items), total))

        return business_list


class JustDialBackend(ApifyBackend):
    """JustDial business listings via Apify.

    Cost: ~$2 per 1,000 results (thirdwatch actor, pay-per-event).
    Free $5/month Apify credit = ~2,500 free leads.

    Returns: name, decoded phone (real numbers, not blurred), full address,
             rating, review count, category, working hours, website,
             listing URL. 30M+ listings across 1,000+ Indian cities.

    Actor: thirdwatch/justdial-business-scraper
    Actor input schema:
        queries: list[str]              # categories to search (e.g. ["restaurants"])
        city: str                       # city (e.g. "Mumbai")
        maxResultsPerQuery: int         # 25/50/100 — total = queries * this
    Actor output schema (per item):
        business_name, category, location, address, phone,
        rating, review_count, timing, website, photos_count, listing_url
    """

    name = "JustDial (Apify)"
    requires_api_key = True

    ACTOR_ID = "Rnln0C6qaFWuidVl5"  # thirdwatch/justdial-business-scraper

    def _build_input(self, search_term: str, total: int, **kwargs: Any) -> dict:
        # Parse query like "restaurants in Mumbai" → city=Mumbai, category=restaurants
        city, category = self._parse_query(search_term)
        if not city:
            raise ValueError(
                "JustDial backend needs a city. "
                'Use a query like "restaurants in Mumbai".'
            )
        # Actor expects `queries` (list) + `city` + `maxResultsPerQuery`
        # Each entry in `queries` × maxResultsPerQuery = total results.
        # We send a single query and let maxResultsPerQuery handle the count.
        per_query = min(total, 500)
        return {
            "queries": [category or "all"],
            "city": city,
            "maxResultsPerQuery": per_query,
        }

    @staticmethod
    def _parse_query(query: str) -> tuple[str, str]:
        """Split 'restaurants in Mumbai' → ('Mumbai', 'restaurants')."""
        query = query.strip()
        for sep in (" in ", " near ", " around "):
            lower = query.lower()
            if sep in lower:
                idx = lower.find(sep)
                category = query[:idx].strip()
                city = query[idx + len(sep):].strip()
                return city, category
        return "", query  # treat as category only, no city

    @staticmethod
    def RESULT_PARSER(item: dict) -> Business:
        # Actor returns flat strings, not nested dicts
        address = item.get("address") or item.get("location")
        phone = item.get("phone") or item.get("mobile") or item.get("phone_number")
        listing_url = (
            item.get("listing_url")
            or item.get("justdial_url")
            or item.get("url")
        )
        return Business(
            name=item.get("business_name") or item.get("name"),
            address=address,
            website=item.get("website") or item.get("site"),
            phone_number=phone,
            category=item.get("category") or item.get("main_category"),
            reviews_average=_safe_float(item.get("rating")),
            reviews_count=_safe_int(
                item.get("review_count")
                or item.get("rating_count")
                or item.get("reviews")
            ),
            latitude=None,  # JustDial actor does not return GPS coords
            longitude=None,
            google_maps_url=listing_url,
        )


class IndiaMARTBackend(ApifyBackend):
    """IndiaMART B2B supplier listings via Apify.

    Cost: ~$2 per 1,000 results (thirdwatch actor).
    Free $5/month Apify credit = ~2,500 free leads.

    Returns: company name, product name, price, MOQ, GST number,
             supplier rating, member since, phone, city/state.
             10M+ suppliers across India. Each row = (supplier, product) pair.

    Actor: thirdwatch/indiamart-supplier-scraper
    Actor input schema:
        queries: list[str]              # search terms (e.g. ["stainless steel pipes"])
        location: str                   # optional (e.g. "Mumbai" or "Maharashtra")
        maxResultsPerQuery: int         # 25/50/100 — total = queries * this
    Actor output schema (per item):
        company_name, product_name, price, moq,
        product_url, catalog_url, image_url,
        location (str), city, state, phone,
        gst_number, supplier_rating, rating_count, member_since
    """

    name = "IndiaMART (Apify)"
    requires_api_key = True

    ACTOR_ID = "VIGpnYPrbIJgZp49F"  # thirdwatch/indiamart-supplier-scraper

    def _build_input(self, search_term: str, total: int, **kwargs: Any) -> dict:
        # IndiaMART accepts a free-form search term + optional location.
        # Smart-parse: "<product> in <city>" → queries=["<product>"], location="<city>"
        # Otherwise send the whole thing as a single query.
        text = search_term.strip()
        loc = ""
        query = text
        lower = text.lower()
        for sep in (" in ", " near ", " around "):
            if sep in lower:
                idx = lower.find(sep)
                query = text[:idx].strip()
                loc = text[idx + len(sep):].strip()
                break
        payload: dict[str, Any] = {
            "queries": [query or "suppliers"],
            "maxResultsPerQuery": min(total, 500),
        }
        if loc:
            payload["location"] = loc
        return payload

    @staticmethod
    def RESULT_PARSER(item: dict) -> Business:
        # Build a readable address from city + state (actor returns these
        # as separate flat fields, not nested).
        city = item.get("city") or ""
        state = item.get("state") or ""
        loc_str = item.get("location") or ""
        if city or state:
            address = ", ".join(p for p in (city, state) if p) or None
        else:
            address = loc_str or None
        # Build a useful "category" by combining category + product name
        products = item.get("products") or []
        first_product = products[0] if products else None
        category = (
            item.get("category")
            or item.get("product_name")
            or first_product
        )
        website = (
            item.get("catalog_url")
            or item.get("product_url")
            or item.get("website")
            or item.get("url")
        )
        return Business(
            name=item.get("company_name") or item.get("supplier") or item.get("name"),
            address=address,
            website=website,
            phone_number=item.get("phone") or item.get("mobile") or item.get("phone_number"),
            category=category,
            reviews_average=_safe_float(
                item.get("supplier_rating") or item.get("rating")
            ),
            reviews_count=_safe_int(
                item.get("rating_count") or item.get("reviews")
            ),
            latitude=None,  # IndiaMART actor does not return GPS coords
            longitude=None,
            google_maps_url=item.get("product_url") or item.get("indiamart_url") or item.get("url"),
        )


# ---------------------------------------------------------------------------
# Foursquare Places API
# ---------------------------------------------------------------------------


class FoursquareBackend:
    """Foursquare Places API v3.

    Free tier: 100,000 calls/month (no credit card required).
    Works in India (decent metro coverage, weaker in tier-2/3 cities).
    Includes: name, address, phone, website, category, lat/lon,
              Foursquare rating (0-10 scale), review count, verified flag.

    Sign up:  https://foursquare.com/developers/
    Docs:     https://docs.foursquare.com/v3/

    Note: Foursquare's free tier is for personal/dev use — commercial use
    requires a paid plan starting around $99/mo. But for 100k leads/month
    casual use, free works.
    """

    name = "Foursquare Places"
    requires_api_key = True

    BASE_URL = "https://api.foursquare.com/v3/places/search"
    PER_PAGE = 50  # Foursquare max per call
    DEFAULT_TIMEOUT = 30.0

    def __init__(self, api_key: Optional[str] = None) -> None:
        # Foursquare v3 uses the API key directly in the Authorization header (no Bearer prefix)
        self.api_key = api_key or os.environ.get("FOURSQUARE_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "Foursquare API key missing. "
                "Set FOURSQUARE_API_KEY or pass api_key=...  "
                "Get a free key at https://foursquare.com/developers/"
            )

    async def scrape(
        self,
        search_term: str,
        total: int = 30,
        progress_callback: Optional[ProgressCallback] = None,
        **kwargs: Any,
    ) -> BusinessList:
        # Foursquare doesn't have a generic "term + location" combined search;
        # we use ``query`` (term) and ``near`` (location). Split if possible.
        query, location = self._parse_query(search_term)
        if not location:
            raise ValueError(
                "Foursquare backend needs a location. "
                'Use a query like "plumbers in Mumbai" — not just "plumbers".'
            )

        headers = {
            "Authorization": self.api_key,  # v3 uses direct key, no Bearer
            "Accept": "application/json",
        }
        business_list = BusinessList()

        async with httpx.AsyncClient(timeout=self.DEFAULT_TIMEOUT) as client:
            # v3 uses cursor-based pagination via the ``cursor`` parameter
            cursor: Optional[str] = None

            while len(business_list.business_list) < total:
                page_size = min(self.PER_PAGE, total - len(business_list.business_list))
                params = {
                    "query": query or None,
                    "near": location,
                    "limit": page_size,
                    "sort": "RELEVANCE",
                }
                if cursor:
                    params["cursor"] = cursor

                logger.info(
                    "Foursquare request: query=%r near=%r limit=%d cursor=%s",
                    query, location, page_size, bool(cursor),
                )

                response = await client.get(
                    self.BASE_URL, params=params, headers=headers
                )

                if response.status_code == 401:
                    raise PermissionError(
                        "Foursquare API key invalid or revoked. "
                        "Get a new one at https://foursquare.com/developers/"
                    )
                if response.status_code == 410:
                    raise PermissionError(
                        "Foursquare deprecated their free API (HTTP 410 Gone). "
                        "The free tier no longer works. "
                        "Switch to OSM (sidebar) for free India data, "
                        "or use Outscraper for paid Google-quality data."
                    )
                if response.status_code == 429:
                    raise PermissionError(
                        "Foursquare rate limit hit. Free tier is 100k/month — "
                        "wait or upgrade."
                    )
                response.raise_for_status()
                data = response.json()

                results = data.get("results", [])
                if not results:
                    break

                for item in results:
                    business_list.add(self._parse(item))
                    if progress_callback:
                        await progress_callback(
                            len(business_list.business_list), total
                        )
                    if len(business_list.business_list) >= total:
                        break

                # Get cursor for next page
                context = data.get("context", {}) or {}
                cursor = context.get("next", {}).get("cursor") if context else None
                if not cursor or len(results) < page_size:
                    break

        logger.info("Foursquare returned %d results", len(business_list))
        return business_list

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _parse_query(query: str) -> tuple[str, str]:
        """Split 'plumbers in Mumbai' → ('plumbers', 'Mumbai')."""
        query = query.strip()
        for sep in (" in ", " near ", " around "):
            lower = query.lower()
            if sep in lower:
                idx = lower.find(sep)
                return query[:idx].strip(), query[idx + len(sep):].strip()
        return query, ""

    @staticmethod
    def _parse(item: dict) -> Business:
        geo = (item.get("geocodes") or {}).get("main") or {}
        loc = item.get("location") or {}
        cats = item.get("categories") or []
        category = cats[0].get("name") if cats else None

        # Foursquare rating is 0-10; convert to 0-5 to match other backends
        raw_rating = _safe_float(item.get("rating"))
        rating_5 = (raw_rating / 2.0) if raw_rating is not None else None

        # Address — prefer formatted, fall back to pieces
        address = (
            loc.get("formatted_address")
            or loc.get("address")
        )
        if not address:
            parts = [
                loc.get("address"),
                loc.get("locality"),
                loc.get("region"),
                loc.get("postcode"),
                loc.get("country"),
            ]
            address = ", ".join(p for p in parts if p) or None

        return Business(
            name=item.get("name"),
            address=address,
            website=item.get("website"),
            phone_number=item.get("tel"),
            category=category,
            reviews_average=rating_5,
            reviews_count=_safe_int(
                (item.get("stats") or {}).get("total_ratings")
            ),
            latitude=_safe_float(geo.get("latitude")),
            longitude=_safe_float(geo.get("longitude")),
            google_maps_url=None,  # Foursquare doesn't provide Maps link
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


# Lazy loader for BotasaurusBackend — keeps import-time cost zero so users
# without botasaurus installed don't break at app startup.
class _LazyBotasaurusBackend:
    """Proxy that defers importing botasaurus_backend until first use."""

    name = "Botasaurus (free, anti-detect)"
    requires_api_key = False

    def __init__(self) -> None:
        try:
            from botasaurus_backend import BotasaurusBackend as _Real
        except ImportError as exc:
            raise RuntimeError(
                "Botasaurus backend requested but not installed. "
                "Run:  pip install botasaurus  "
                "Then restart the app."
            ) from exc
        self._real = _Real()

    def __getattr__(self, item: str) -> Any:
        return getattr(self._real, item)


_BACKEND_REGISTRY: dict[str, type[ScraperBackend]] = {
    "playwright": PlaywrightBackend,
    "botasaurus": _LazyBotasaurusBackend,
    "outscraper": OutscraperBackend,
    "serpapi": SerpApiBackend,
    "osm": OSMBackend,
    "openstreetmap": OSMBackend,
    "yelp": YelpBackend,
    "foursquare": FoursquareBackend,
    "fsq": FoursquareBackend,  # alias
    "justdial": JustDialBackend,
    "indiamart": IndiaMARTBackend,
    "apify": JustDialBackend,  # alias
    "out": OutscraperBackend,  # alias
    "serp": SerpApiBackend,  # alias
    "omkar": _LazyBotasaurusBackend,  # alias
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
