"""
MapLead — Utilities
===================

Pure-Python helpers for stats, parsing, and multi-format export.
No Streamlit / Playwright imports here so this stays easy to test.
"""

from __future__ import annotations

import io
import json
import re
from datetime import datetime
from typing import Iterable, Optional

from scraper import Business


# ---------------------------------------------------------------------------
# Filename helper — used by every download button so saved files are
# self-describing (query name + date + lead count + backend tag).
# ---------------------------------------------------------------------------
_BAD_FN_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def make_filename(
    search_term: str = "",
    backend: str = "",
    pack_name: str = "",
    ext: str = "csv",
    lead_count: int = 0,
    include_date: bool = True,
) -> str:
    """Build a clean, sortable filename for an export.

    Examples:
        make_filename('restaurants in Hyderabad', 'botasaurus', ext='csv')
        → 'restaurants_in_hyderabad_botasaurus_180leads_2026-07-29.csv'

        make_filename('', 'botasaurus', pack_name='Signage — Hyderabad',
                      ext='xlsx', lead_count=180)
        → 'leadpack_signage_hyderabad_botasaurus_180leads_2026-07-29.xlsx'
    """
    parts: list[str] = []

    if pack_name:
        slug = _BAD_FN_CHARS.sub("_", pack_name).strip("_").lower()
        # Collapse repeated underscores
        slug = re.sub(r"_+", "_", slug)
        parts.append(f"leadpack_{slug}")
    elif search_term:
        slug = _BAD_FN_CHARS.sub("_", search_term).strip("_").lower()
        slug = re.sub(r"_+", "_", slug)
        parts.append(slug)

    if backend:
        parts.append(backend)

    if lead_count > 0:
        parts.append(f"{lead_count}leads")

    if include_date:
        parts.append(datetime.now().strftime("%Y-%m-%d"))

    base = "_".join(p for p in parts if p) or "maplead"
    return f"{base}.{ext.lstrip('.')}"


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


# ---------------------------------------------------------------------------
# Phone-only CSV — for cold-call sheets / WhatsApp broadcast
# ---------------------------------------------------------------------------
def export_phones_csv(businesses: Iterable[Business]) -> bytes:
    """Just Name + Phone, one row per lead. Skips leads without a phone."""
    rows = []
    for b in businesses:
        if not b.phone_number:
            continue
        # Strip non-digit chars from phone for the dedicated `tel` column
        digits = re.sub(r"[^\d+]", "", b.phone_number)
        rows.append({
            "Name": b.name,
            "Phone (display)": b.phone_number,
            "Phone (digits)": digits,
            "tel_link": f"tel:{digits}",
            "Category": b.category,
            "Address": b.address,
        })
    import pandas as pd
    df = pd.DataFrame(rows)
    return df.to_csv(index=False).encode("utf-8-sig")


# ---------------------------------------------------------------------------
# vCard (.vcf) — import directly into phone contacts / WhatsApp / Truecaller
# ---------------------------------------------------------------------------
def _vcard_escape(value: str) -> str:
    """Escape commas, semicolons, and newlines per RFC 6350."""
    return (
        value.replace("\\", "\\\\")
             .replace(",", "\\,")
             .replace(";", "\\;")
             .replace("\n", "\\n")
    )


def export_vcard(businesses: Iterable[Business]) -> bytes:
    """Return a .vcf file (vCard 3.0) with one entry per business that has a phone."""
    lines: list[str] = []
    for i, b in enumerate(businesses, start=1):
        if not b.phone_number:
            continue
        org = _vcard_escape(b.name or "Unknown")
        tel = _vcard_escape(b.phone_number)
        addr = _vcard_escape(b.address or "")
        url = b.google_maps_url or b.website or ""
        lines.append("BEGIN:VCARD")
        lines.append("VERSION:3.0")
        lines.append(f"FN:{org}")
        lines.append(f"ORG:{org}")
        lines.append(f"TEL;TYPE=VOICE,WORK:{tel}")
        if b.category:
            lines.append(f"TITLE:{_vcard_escape(b.category)}")
        if addr:
            # ADR field uses ; separators: PO Box; ext; street; city; region; postal; country
            lines.append(f"ADR;TYPE=WORK:;;{addr};;;;")
        if url:
            lines.append(f"URL:{url}")
        if b.reviews_average is not None:
            lines.append(f"NOTE:Rating {_vcard_escape(str(b.reviews_average))} on Google Maps")
        lines.append(f"UID:maplead-{i}-{re.sub(r'[^a-zA-Z0-9]', '', org)[:30]}")
        lines.append("END:VCARD")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Excel with one sheet per source query — for lead packs
# ---------------------------------------------------------------------------
def export_excel_by_source(businesses: Iterable[Business]) -> bytes:
    """Excel workbook with one sheet per `source_query` tag + a combined sheet."""
    import pandas as pd
    from collections import defaultdict

    by_source: dict[Optional[str], list[Business]] = defaultdict(list)
    for b in businesses:
        src = getattr(b, "source_query", None) or "All leads"
        by_source[src].append(b)

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        # Combined sheet first
        all_df = pd.DataFrame(_rows(businesses))
        all_df.to_excel(writer, index=False, sheet_name="All leads"[:31])
        # Then one sheet per source
        for source, items in by_source.items():
            sheet_name = source[:31] if source else "All leads"
            # Sanitize sheet name: Excel disallows : \ / ? * [ ] '
            sheet_name = re.sub(r"[:\\/\?\*\[\]']", "_", sheet_name)
            if not sheet_name.strip():
                sheet_name = "leads"
            pd.DataFrame(_rows(items)).to_excel(writer, index=False, sheet_name=sheet_name)
    buffer.seek(0)
    return buffer.getvalue()
