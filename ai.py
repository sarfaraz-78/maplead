"""
MapLead \u2014 AI helpers (OpenAI-compatible APIs)
===============================================

Optional features that need an API key. Works with any OpenAI-compatible
endpoint \u2014 OpenAI, DeepSeek, Together, Groq, Ollama, LM Studio, etc.

Set via env var or st.secrets:
    export MAPLEAD_OPENAI_API_KEY=sk-...
    export MAPLEAD_OPENAI_BASE_URL=https://api.deepseek.com/v1   # default OpenAI if unset
    export MAPLEAD_OPENAI_MODEL=deepseek-chat                    # default gpt-4o-mini

Features
--------
- score_lead(business) \u2014 returns 1\u201310 conversion likelihood + reasoning
- generate_whatsapp_message(business, city) \u2014 Hinglish-friendly opener
- generate_cold_call_script(business, city) \u2014 personalized pitch
- research_lead(business) \u2014 quick company summary

All functions degrade gracefully: if no key is configured, they return
sensible fallbacks so the UI still works.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from scraper import Business

logger = logging.getLogger("maplead.ai")

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def get_api_key() -> str:
    """Return the API key from env / st.secrets, or empty string."""
    try:
        import streamlit as st
        key = (st.secrets.get("MAPLEAD_OPENAI_API_KEY", "") if hasattr(st, "secrets") else "")
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("MAPLEAD_OPENAI_API_KEY", "")


def get_base_url() -> str:
    try:
        import streamlit as st
        v = st.secrets.get("MAPLEAD_OPENAI_BASE_URL", "") if hasattr(st, "secrets") else ""
        if v:
            return v
    except Exception:
        pass
    return os.environ.get("MAPLEAD_OPENAI_BASE_URL", DEFAULT_BASE_URL)


def get_model() -> str:
    try:
        import streamlit as st
        v = st.secrets.get("MAPLEAD_OPENAI_MODEL", "") if hasattr(st, "secrets") else ""
        if v:
            return v
    except Exception:
        pass
    return os.environ.get("MAPLEAD_OPENAI_MODEL", DEFAULT_MODEL)


def is_configured() -> bool:
    return bool(get_api_key())


# ---------------------------------------------------------------------------
# Core call
# ---------------------------------------------------------------------------
def _call_llm(system: str, user: str, *, temperature: float = 0.4, max_tokens: int = 600) -> str:
    """Make a chat completion call. Returns '' on any failure."""
    key = get_api_key()
    if not key:
        return ""
    try:
        import httpx
        base = get_base_url()
        model = get_model()
        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"{base.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
        if r.status_code != 200:
            logger.warning("LLM call failed: %s %s", r.status_code, r.text[:200])
            return ""
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning("LLM call exception: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Lead scoring
# ---------------------------------------------------------------------------
HEURISTIC_SCORE_PROMPT = """\
You are a B2B sales coach. Score this business lead for likelihood to buy \
signage services (storefront signs, menu boards, LED displays, hoarding, \
interior office signage) on a 1\u201310 scale.

Consider:
- Category fit (restaurants, retail, hotels, salons, jewellery = high; \
  hospitals, schools = medium; tech companies = low unless new office)
- Rating & review count (more = established, more likely to spend)
- Has website / phone (more = easier to reach, more legit)
- City (tier-1 cities = more budget for branding)

Reply with ONLY a JSON object, no prose, in this exact format:
{"score": <integer 1-10>, "reason": "<one short sentence>"}

Business:
"""


def score_lead(b: Business) -> dict:
    """Score a lead 1\u201310. Returns dict with 'score', 'reason', and 'source'.

    Falls back to a heuristic score if no API key is set.
    """
    fallback = _heuristic_score(b)
    if not is_configured():
        return {**fallback, "source": "heuristic"}
    biz_json = json.dumps({
        "name": b.name, "category": b.category, "rating": b.reviews_average,
        "reviews": b.reviews_count, "has_phone": bool(b.phone_number),
        "has_website": bool(b.website), "address": b.address,
    }, indent=2)
    out = _call_llm(
        "Reply with only valid JSON.",
        HEURISTIC_SCORE_PROMPT + biz_json,
        temperature=0.1,
        max_tokens=120,
    )
    if not out:
        return {**fallback, "source": "heuristic"}
    try:
        # Strip code fences if present
        cleaned = out.strip().strip("`").strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        parsed = json.loads(cleaned)
        score = int(parsed.get("score", fallback["score"]))
        return {"score": max(1, min(10, score)),
                "reason": parsed.get("reason", fallback["reason"]),
                "source": "ai"}
    except (ValueError, json.JSONDecodeError):
        return {**fallback, "source": "heuristic"}


def _heuristic_score(b: Business) -> dict:
    """Quick rule-based fallback when no API key."""
    score = 5
    cat = (b.category or "").lower()
    high_fit = {"restaurant", "cafe", "coffee", "hotel", "salon", "jewelry",
                "jewellery", "boutique", "retail", "shop", "bar", "pub",
                "clinic", "pharmacy", "bakery", "gym", "school"}
    if any(k in cat for k in high_fit):
        score += 2
    if b.reviews_average and b.reviews_average >= 4.0:
        score += 1
    if b.reviews_count and b.reviews_count >= 100:
        score += 1
    if b.website:
        score += 1
    score = max(1, min(10, score))
    reason = f"{cat or 'business'} \u2014 heuristic"
    if any(k in cat for k in high_fit):
        reason = "Visible storefront category \u2014 strong signage fit"
    elif b.reviews_average and b.reviews_average >= 4.5:
        reason = "High rating \u2014 established business"
    elif not b.phone_number:
        reason = "No phone \u2014 hard to reach"
    return {"score": score, "reason": reason}


# ---------------------------------------------------------------------------
# WhatsApp message generator
# ---------------------------------------------------------------------------
WA_PROMPT = """\
You write short WhatsApp business messages for a local Indian signage company.
Write a 2\u20133 sentence opener in Hinglish-friendly English (the way Indians \
actually text on WhatsApp). Mention their business by name and one specific \
thing (their category, rating, or location) so it doesn't feel like spam.

Constraints:
- Under 60 words
- 1 emoji max
- No hashtags
- No exclamation marks at the end (looks desperate)
- Include one specific offer (free quote / site visit / sample catalogue)

Business:
"""


def generate_whatsapp_message(b: Business, city: str = "your city") -> str:
    """Return a 2\u20133 sentence WhatsApp opener. Falls back to a template."""
    if not b.name:
        return ""
    if not is_configured():
        return _template_wa(b, city)
    biz = f"name={b.name}, category={b.category}, rating={b.reviews_average}, city={city}"
    out = _call_llm(WA_PROMPT + "Output only the message, no preamble.", biz,
                    temperature=0.6, max_tokens=150)
    if not out or len(out) > 500:
        return _template_wa(b, city)
    return out


def _template_wa(b: Business, city: str) -> str:
    cat = b.category or "business"
    return (
        f"Hi! I'm a local signage vendor in {city}. Came across {b.name} "
        f"({cat}) and thought you might be interested in our storefront signs / "
        f"menu boards. Would a free site visit + quote be useful?"
    )


# ---------------------------------------------------------------------------
# Cold-call script generator
# ---------------------------------------------------------------------------
SCRIPT_PROMPT = """\
You are a friendly cold-call coach for a small Indian signage business.
Write a 30-second cold-call script (under 80 words) for a salesperson to read \
on the phone. The script must:
- Open by naming the business and the city
- Mention one specific thing about their category or rating
- Pivot quickly to a concrete offer (free quote, sample visit)
- End with a soft ask (\u201ccan I drop by Thursday?\u201d)

Business:
"""


def generate_cold_call_script(b: Business, city: str = "your city") -> str:
    """Return a 30-second cold-call script. Falls back to a template."""
    if not b.name:
        return ""
    if not is_configured():
        return _template_script(b, city)
    biz = f"name={b.name}, category={b.category}, rating={b.reviews_average}, city={city}"
    out = _call_llm(SCRIPT_PROMPT + "Output only the script, no preamble.", biz,
                    temperature=0.6, max_tokens=200)
    if not out or len(out) > 700:
        return _template_script(b, city)
    return out


def _template_script(b: Business, city: str) -> str:
    cat = b.category or "business"
    return (
        f"\u201cHi, may I speak with the owner? I'm calling from a local signage "
        f"company here in {city}. I noticed {b.name} ({cat}) on Google Maps and "
        f"wanted to ask \u2014 are you happy with your current exterior signage, or "
        f"have you been thinking about an upgrade? We do storefront signs, menu "
        f"boards and LED displays for businesses like yours. Could I drop off a "
        f"sample catalogue this Thursday?\u201d"
    )


# ---------------------------------------------------------------------------
# Lead research (one-shot summary)
# ---------------------------------------------------------------------------
RESEARCH_PROMPT = """\
In 3\u20134 bullet points, summarize what you know about this business and what \
their signage/visibility needs likely are, based on the limited data below. \
Be specific and actionable. Output ONLY the bullets, no preamble.

"""


def research_lead(b: Business) -> str:
    """Return 3\u20134 bullet summary of the lead. Empty string if no API key."""
    if not is_configured():
        return ""
    biz = f"name={b.name}, category={b.category}, rating={b.reviews_average}, reviews={b.reviews_count}, address={b.address}, has_website={'yes' if b.website else 'no'}"
    return _call_llm("Be concise and specific.", RESEARCH_PROMPT + biz,
                    temperature=0.3, max_tokens=250)