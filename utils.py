"""
MapLead — Utilities
===================

Pure-Python helpers for stats, parsing, and multi-format export.
No Streamlit / Playwright imports here so this stays easy to test.
"""

from __future__ import annotations

import io
import json
from typing import Iterable

from scraper import Business


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def compute_stats(businesses: Iterable[Business]) -> dict:
    """Compute headline stats shown in the dashboard."""
    businesses = list(businesses)
    total = len(businesses)
    if total == 0:
        return {
            "total": 0,
            "avg_rating": None,
            "avg_reviews": None,
            "with_website": 0,
            "with_phone": 0,
            "website_pct": 0.0,
            "phone_pct": 0.0,
            "categories": {},
        }

    ratings = [b.reviews_average for b in businesses if b.reviews_average is not None]
    counts = [b.reviews_count for b in businesses if b.reviews_count is not None]
    with_website = sum(1 for b in businesses if b.website)
    with_phone = sum(1 for b in businesses if b.phone_number)

    cat_counts: dict[str, int] = {}
    for b in businesses:
        if b.category:
            cat_counts[b.category] = cat_counts.get(b.category, 0) + 1

    return {
        "total": total,
        "avg_rating": sum(ratings) / len(ratings) if ratings else None,
        "avg_reviews": sum(counts) / len(counts) if counts else None,
        "with_website": with_website,
        "with_phone": with_phone,
        "website_pct": 100 * with_website / total,
        "phone_pct": 100 * with_phone / total,
        "categories": dict(
            sorted(cat_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        ),
    }


# ---------------------------------------------------------------------------
# Exporters — all return BytesIO so Streamlit can stream downloads
# ---------------------------------------------------------------------------


def _rows(businesses: Iterable[Business]) -> list[dict]:
    """Convert Business objects to flat dicts for tabular export."""
    rows = []
    for b in businesses:
        rows.append(
            {
                "Name": b.name,
                "Category": b.category,
                "Address": b.address,
                "Phone": b.phone_number,
                "Website": b.website,
                "Rating": b.reviews_average,
                "Reviews": b.reviews_count,
                "Latitude": b.latitude,
                "Longitude": b.longitude,
                "Google Maps URL": b.google_maps_url,
            }
        )
    return rows


def export_excel(businesses: Iterable[Business], sheet_name: str = "Leads") -> bytes:
    """Return an in-memory .xlsx file."""
    import pandas as pd

    df = pd.DataFrame(_rows(businesses))
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
    buffer.seek(0)
    return buffer.getvalue()


def export_csv(businesses: Iterable[Business]) -> bytes:
    """Return UTF-8-with-BOM CSV (so Excel opens it correctly)."""
    import pandas as pd

    df = pd.DataFrame(_rows(businesses))
    return df.to_csv(index=False).encode("utf-8-sig")


def export_json(businesses: Iterable[Business], pretty: bool = True) -> bytes:
    """Return JSON bytes."""
    payload = _rows(businesses)
    indent = 2 if pretty else None
    return json.dumps(payload, ensure_ascii=False, indent=indent).encode("utf-8")
