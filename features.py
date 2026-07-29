"""
MapLead — Lead-generation & outreach utilities
==============================================

Helpers that don't belong in scraper.py or utils.py:
- Indian phone number formatting
- WhatsApp pre-filled URL generator
- Dedupe by phone number
- Cold-call script templates
- Persistent lead-status tracker (SQLite)
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from scraper import Business


# ---------------------------------------------------------------------------
# Phone formatting
# ---------------------------------------------------------------------------
def phone_digits_only(phone: Optional[str]) -> str:
    """Strip everything except digits. Empty string for None."""
    if not phone:
        return ""
    return re.sub(r"\D", "", phone)


def normalize_phone(phone: Optional[str]) -> str:
    """Normalize phone digits so '+91 98765 43210' and '9876543210' match.

    Strips everything non-digit, then strips a leading '91' (country code)
    or '0' (trunk prefix) so 10-digit Indian numbers all collapse to the
    same key.
    """
    d = phone_digits_only(phone)
    if not d:
        return ""
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return d


def format_phone_in(phone: Optional[str]) -> str:
    """Format an Indian phone as +91 XXXXX XXXXX (best-effort).

    Accepts raw 10-digit, 11-digit (0-prefix), 12-digit (91-prefix),
    or already formatted strings like '+91 98765 43210'.
    Returns the original string (lightly trimmed) if it doesn't look Indian.
    """
    if not phone:
        return ""
    d = normalize_phone(phone)
    if len(d) == 10 and d[0] in "6789":
        return f"+91 {d[:5]} {d[5:]}"
    if len(d) == 10 and d[0] in "2345":
        # Likely STD landline (e.g. 040 for Hyderabad)
        return f"+91 {d[:4]} {d[4:]}"
    return phone.strip()


# ---------------------------------------------------------------------------
# WhatsApp pre-filled URL
# ---------------------------------------------------------------------------
def whatsapp_url(phone: Optional[str], message: str = "") -> str:
    """Build a wa.me URL that opens WhatsApp with a pre-filled message.

    Phone is normalized to digits only. If it doesn't look Indian (no 91
    prefix), we add 91. Returns '' if phone is missing.
    """
    d = normalize_phone(phone)
    if not d:
        return ""
    if len(d) == 10 and d[0] in "6789":
        d = "91" + d
    if not message:
        message = (
            "Hi! I'm from a local signage business in your area. "
            "I'd love to chat about your signage needs."
        )
    from urllib.parse import quote
    return f"https://wa.me/{d}?text={quote(message)}"


# ---------------------------------------------------------------------------
# Dedupe by phone
# ---------------------------------------------------------------------------
def dedupe_by_phone(businesses: Iterable[Business]) -> list[Business]:
    """Keep the first occurrence per phone number; drop businesses with no phone.

    Useful when scraping overlapping categories (e.g. 'restaurants in Mumbai'
    and 'cafes in Mumbai' return some of the same shops).
    """
    seen: set[str] = set()
    out: list[Business] = []
    for b in businesses:
        d = normalize_phone(b.phone_number)
        if not d:
            out.append(b)  # no phone — keep, can't dedupe
            continue
        if d in seen:
            continue
        seen.add(d)
        out.append(b)
    return out


# ---------------------------------------------------------------------------
# Cold-call / outreach script templates
# ---------------------------------------------------------------------------
SCRIPT_TEMPLATES: dict[str, str] = {
    "Cold call (signage intro)": (
        "Hi {name}, I'm calling from a local signage business here in {city}. "
        "I came across your {category} and wanted to check \u2014 are you happy with "
        "your current exterior signage, or have you been thinking about an upgrade? "
        "We do storefront signs, menu boards, LED displays and hoarding for "
        "businesses like yours. Could I drop off a sample catalogue this week?"
    ),
    "WhatsApp opener": (
        "Hi {name}! \ud83d\udc4b I'm a local signage vendor here in {city}. I noticed your "
        "{category} on Google Maps and thought you might be interested in our "
        "storefront signage / menu boards / LED displays. Happy to share our "
        "recent work and a free quote \u2014 would that be useful? \ud83d\ude4f"
    ),
    "Email follow-up": (
        "Subject: Signage ideas for {name}\n\n"
        "Hi,\n\nI came across {name} while looking at top-rated {category} in "
        "{city}. Your business looks great and I'd love to share a few signage "
        "ideas that could help you stand out even more.\n\n"
        "We design and install storefront signs, menu boards, LED displays and "
        "hoardings for businesses like yours. I'd be happy to:\n"
        "  \u2022 Share a few before/after photos from similar projects\n"
        "  \u2022 Offer a free site visit and quote\n\n"
        "Would any of this be useful? Either way, thanks for the great work "
        "you're doing at {name}.\n\nBest,\n[Your name]\n[Your business]"
    ),
}


def render_script(template_key: str, name: str, category: str, city: str) -> str:
    """Fill in a script template with the business's actual data."""
    tpl = SCRIPT_TEMPLATES.get(template_key) or SCRIPT_TEMPLATES["Cold call (signage intro)"]
    return tpl.format(
        name=name or "your business",
        category=category or "business",
        city=city or "the city",
    )


# ---------------------------------------------------------------------------
# Persistent lead-status tracker (SQLite)
# ---------------------------------------------------------------------------
STATUSES: list[str] = ["New", "Contacted", "Interested", "Quoted", "Won", "Lost"]


class LeadStore:
    """Tiny SQLite-backed status store. Survives browser restarts.

    Usage:
        store = LeadStore()                    # opens leads.db in cwd
        store.upsert(key, name, addr, phone, status="Contacted", note="...")
        store.get(key)
        store.all()
    """

    def __init__(self, db_path: str | Path = "leads.db") -> None:
        self.db_path = Path(db_path)
        # Use a check_same_thread=False connection so tests / multi-thread
        # access is safe; close explicitly after each op.
        c = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS lead_status (
                    lead_key TEXT PRIMARY KEY,
                    name TEXT,
                    address TEXT,
                    phone TEXT,
                    status TEXT NOT NULL DEFAULT 'New',
                    note TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            c.commit()
        finally:
            c.close()

    @staticmethod
    def make_key(b: Business) -> str:
        """Stable key for a Business. Prefers phone (most stable) then address+name."""
        d = normalize_phone(b.phone_number)
        if d:
            return f"phone:{d}"
        slug = re.sub(r"\s+", " ", f"{(b.name or '').strip().lower()}|{(b.address or '').strip().lower()}")
        return f"id:{slug}"

    def upsert(self, key: str, *, name: str, address: str, phone: str,
               status: str, note: str = "") -> None:
        c = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            c.execute(
                """
                INSERT INTO lead_status (lead_key, name, address, phone, status, note, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lead_key) DO UPDATE SET
                    status=excluded.status,
                    note=excluded.note,
                    updated_at=excluded.updated_at
                """,
                (key, name, address, phone, status, note,
                 datetime.now().isoformat(timespec="seconds")),
            )
            c.commit()
        finally:
            c.close()

    def get(self, key: str) -> Optional[dict]:
        c = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT * FROM lead_status WHERE lead_key = ?", (key,)).fetchone()
        finally:
            c.close()
        return dict(row) if row else None

    def all(self) -> list[dict]:
        c = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT * FROM lead_status ORDER BY updated_at DESC").fetchall()
        finally:
            c.close()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, int]:
        c = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT status, COUNT(*) AS n FROM lead_status GROUP BY status"
            ).fetchall()
        finally:
            c.close()
        out = {s: 0 for s in STATUSES}
        for r in rows:
            out[r["status"]] = r["n"]
        return out


# ---------------------------------------------------------------------------
# Per-source stats for lead packs
# ---------------------------------------------------------------------------
def source_stats(businesses: Iterable[Business]) -> list[dict]:
    """Group leads by source_query and return counts/with-phone for each."""
    from collections import defaultdict
    groups: dict[Optional[str], list[Business]] = defaultdict(list)
    for b in businesses:
        src = getattr(b, "source_query", None) or "Direct search"
        groups[src].append(b)
    out = []
    for src, items in groups.items():
        with_phone = sum(1 for b in items if b.phone_number)
        with_rating = sum(1 for b in items if b.reviews_average is not None)
        out.append({
            "source": src,
            "total": len(items),
            "with_phone": with_phone,
            "with_rating": with_rating,
        })
    return sorted(out, key=lambda r: -r["total"])