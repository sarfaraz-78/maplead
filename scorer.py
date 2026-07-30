"""
MapLead — Heuristic lead scorer (no LLM required)
==================================================

When AI enrichment fails or isn't configured, we still want to RANK
the scraped leads by quality. This module scores businesses on a 0-10
scale using only the structured data we already have:

    - has_phone              +2
    - has_website            +2
    - has_rating             +2  (only if >= 3.5)
    - has_review_count       +1  (only if >= 5)
    - has_latlon             +1
    - has_full_address       +1
    - latlon_in_india        +0.5  (only when locale=en-IN/hi)

Quality tiers:
    8-10  hot     -> contact NOW
    5-7   warm    -> worth a call
    1-4   cold    -> bulk outreach only
    0     skip    -> unusable

This is intentionally fast, deterministic, and offline. It costs
nothing and never fails. Use it as the baseline; let AI ENHANCE the
scores when available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from scraper import Business


@dataclass
class Score:
    score: int            # 0-10
    tier: str             # "hot" / "warm" / "cold" / "skip"
    reason: str           # one-liner why

    def to_dict(self) -> dict:
        return {"score": self.score, "tier": self.tier, "reason": self.reason}


def heuristic_score(b: Business) -> Score:
    """Score a business 0-10 using only its structured fields."""
    score = 0.0
    parts: list[str] = []

    # Phone (most important for outreach)
    if b.phone_number:
        # Normalize and check length
        digits = "".join(c for c in b.phone_number if c.isdigit() or c == "+")
        if 8 <= len(digits) <= 15:
            score += 2
            parts.append("phone")
        elif len(digits) >= 6:
            score += 1
            parts.append("phone(partial)")

    # Website
    if b.website:
        if b.website.startswith(("http://", "https://")):
            score += 2
            parts.append("website")
        else:
            score += 1
            parts.append("website(weird)")

    # Rating (proxy for active + reputable business)
    if b.reviews_average is not None and b.reviews_average >= 3.5:
        score += 2
        parts.append(f"rating={b.reviews_average:.1f}")
    elif b.reviews_average is not None and b.reviews_average >= 2.0:
        score += 1
        parts.append(f"low-rating={b.reviews_average:.1f}")

    # Review count (volume = active)
    if b.reviews_count is not None and b.reviews_count >= 5:
        score += 1
        if b.reviews_count >= 50:
            score += 0.5  # bonus for very popular
            parts.append(f"reviews={b.reviews_count}")
        else:
            parts.append(f"reviews={b.reviews_count}")
    elif b.reviews_count is not None and b.reviews_count >= 1:
        score += 0.5

    # Lat/Lon (we can map/mail to them)
    if b.latitude is not None and b.longitude is not None:
        score += 1
        parts.append("coords")

    # Address (full postal address helps with outreach)
    if b.address:
        # Multi-part address = higher quality
        if "," in b.address and len(b.address) > 20:
            score += 1
            parts.append("full-address")
        else:
            score += 0.5

    # Penalties
    if b.is_closed is True:
        score = 0
        parts = ["CLOSED"]
    elif b.name and any(w in b.name.lower() for w in ["closed", "shut", "defunct"]):
        score = max(0, score - 3)
        parts.append("closed-in-name")

    # Clamp
    final = min(int(round(score)), 10)

    # Tier
    if final >= 8:
        tier = "hot"
    elif final >= 5:
        tier = "warm"
    elif final >= 1:
        tier = "cold"
    else:
        tier = "skip"

    reason = ", ".join(parts[:4]) if parts else "no usable data"
    return Score(score=final, tier=tier, reason=reason)


def score_batch(businesses: list[Business]) -> list[tuple[Business, Score]]:
    """Score a list of businesses and return sorted (Business, Score) tuples.

    Sorted hot -> cold so the UI can show best leads first.
    """
    scored = [(b, heuristic_score(b)) for b in businesses]
    scored.sort(key=lambda x: x[1].score, reverse=True)
    return scored


# Visually distinct tier colors for the UI
TIER_EMOJI = {"hot": "🔥", "warm": "🟡", "cold": "🔵", "skip": "⚫"}
TIER_COLORS = {
    "hot":  "#DC2626",  # red-600
    "warm": "#F59E0B",  # amber-500
    "cold": "#3B82F6",  # blue-500
    "skip": "#6B7280",  # gray-500
}
