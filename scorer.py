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
    outreach: str         # templated outreach msg (always populated)
    category: str         # short tag (always populated)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "tier": self.tier,
            "reason": self.reason,
            "outreach": self.outreach,
            "category": self.category,
        }


def heuristic_score(b: Business) -> Score:
    """Score a business 0-10 using only its structured fields."""
    score = 0.0
    parts: list[str] = []

    # Phone (most important for outreach)
    if b.phone_number:
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
            score += 0.5
            parts.append(f"reviews={b.reviews_count}")
        else:
            parts.append(f"reviews={b.reviews_count}")
    elif b.reviews_count is not None and b.reviews_count >= 1:
        score += 0.5

    # Lat/Lon
    if b.latitude is not None and b.longitude is not None:
        score += 1
        parts.append("coords")

    # Address
    if b.address:
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

    # ---- Templated outreach (no LLM needed) ----
    outreach = _template_outreach(b, tier)
    category = _template_category(b)

    return Score(score=final, tier=tier, reason=reason, outreach=outreach, category=category)


def _template_outreach(b: Business, tier: str) -> str:
    """Generate a templated outreach message from business fields.

    No LLM needed - works offline, instantly, costs nothing.
    Personalised enough to be usable as a starting point.
    """
    name = b.name or "your business"
    cat = b.category or "local business"

    # City from address (second-to-last comma-separated part usually)
    city = "your area"
    if b.address:
        parts = [p.strip() for p in b.address.split(",")]
        if len(parts) >= 2:
            city = parts[-2]
        elif len(parts) == 1:
            city = parts[0]

    # Opening varies by tier
    openers = {
        "hot": "Hi {name} team,",
        "warm": "Hello {name},",
        "cold": "Hi {name},",
        "skip": "Hi {name},",
    }

    opener = openers.get(tier, "Hi {name},").format(name=name)

    # Mention what we noticed (proof we did homework)
    observations = []
    if b.reviews_average and b.reviews_average >= 4.0:
        observations.append(f"your strong {b.reviews_average:.1f}-star reputation")
    if b.category and b.category.lower() not in ("unknown", ""):
        observations.append(f"your work in {cat.lower()}")
    if len(observations) == 0:
        observations.append(f"your business")

    # Mention a concrete offer
    offer = (
        "I work with similar {cat} businesses on practical improvements — "
        "things like response times, customer follow-ups, or simple automation "
        "that pays back within weeks."
    ).format(cat=cat)

    # Soft close
    close = "Worth a quick 10-minute call this week?"

    msg_parts = [opener, ""]
    msg_parts.append(f"Came across {name} in {city} and was impressed by " + observations[0] + ".")
    msg_parts.append("")
    msg_parts.append(offer)
    msg_parts.append("")
    msg_parts.append(close)

    return "\n".join(msg_parts).strip()


def _template_category(b: Business) -> str:
    """One-word category tag derived from existing category or name."""
    if b.category:
        return b.category.lower().replace(" ", "_")[:32]
    if b.name:
        n = b.name.lower()
        # Common word-level hints
        for kw, tag in [
            ("cafe", "cafe"), ("coffee", "cafe"),
            ("restaurant", "restaurant"), ("food", "restaurant"),
            ("hotel", "hotel"), ("lodging", "hotel"),
            ("gym", "gym"), ("fitness", "gym"),
            ("school", "education"), ("academy", "education"),
            ("hospital", "medical"), ("clinic", "medical"),
            ("pharmacy", "pharmacy"), ("medical", "pharmacy"),
            ("salon", "salon"), ("beauty", "salon"),
            ("shop", "retail"), ("store", "retail"), ("mart", "retail"),
            ("plumber", "plumber"), ("electric", "electrician"),
            ("auto", "auto"), ("motor", "auto"),
            ("lawyer", "legal"), ("advocate", "legal"),
            ("bank", "finance"), ("finan", "finance"),
        ]:
            if kw in n:
                return tag
    return "other"


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
