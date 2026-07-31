"""
MapLead \u2014 AI helpers (OpenRouter + any OpenAI-compatible API)
=============================================================

Works with:
  - OpenRouter (https://openrouter.ai) \u2014 100+ models via one key
  - OpenAI, Anthropic, Google, Groq, Together, Mistral, Ollama, LM Studio

Configure via env vars / st.secrets:
    export MAPLEAD_OPENAI_API_KEY=sk-or-v1-...
    export MAPLEAD_OPENAI_BASE_URL=https://openrouter.ai/api/v1
    export MAPLEAD_OPENAI_MODEL=anthropic/claude-3.5-sonnet

If unset, the app falls back to heuristic templates so the UI still works.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from scraper import Business

logger = logging.getLogger("maplead.ai")

# ---------------------------------------------------------------------------
# Defaults \u2014 sensible for OpenRouter
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash-0731"

# OpenRouter-recommended headers for attribution (helps with rate limits)
OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://maplead.local",
    "X-Title": "MapLead",
}


# Curated list of popular OpenRouter models with rough pricing (USD per 1M tokens)
# Pricing is approximate \u2014 see https://openrouter.ai/models for current rates.
# `free` = $0; prices are input_cost/output_cost per 1M tokens.
POPULAR_MODELS: list[dict] = [
    {
        "id": "anthropic/claude-3.5-sonnet",
        "label": "Claude 3.5 Sonnet (top quality)",
        "tier": "top",
        "input": 3.0,
        "output": 15.0,
    },
    {
        "id": "openai/gpt-4o",
        "label": "GPT-4o (top quality, fast)",
        "tier": "top",
        "input": 2.5,
        "output": 10.0,
    },
    {
        "id": "openai/gpt-4o-mini",
        "label": "GPT-4o mini (great value)",
        "tier": "mid",
        "input": 0.15,
        "output": 0.60,
    },
    {
        "id": "anthropic/claude-3-haiku",
        "label": "Claude 3 Haiku (cheap + good)",
        "tier": "mid",
        "input": 0.25,
        "output": 1.25,
    },
    {
        "id": "google/gemini-2.0-flash-001",
        "label": "Gemini 2.0 Flash (fast + smart)",
        "tier": "mid",
        "input": 0.10,
        "output": 0.40,
    },
    {
        "id": "meta-llama/llama-3.3-70b-instruct",
        "label": "Llama 3.3 70B (open weights)",
        "tier": "mid",
        "input": 0.59,
        "output": 0.79,
    },
    {
        "id": "meta-llama/llama-3.2-3b-instruct:free",
        "label": "Llama 3.2 3B (FREE)",
        "tier": "free",
        "input": 0.0,
        "output": 0.0,
    },
    {
        "id": "deepseek/deepseek-chat",
        "label": "DeepSeek V3 (cheap, strong)",
        "tier": "cheap",
        "input": 0.14,
        "output": 0.28,
    },
    {
        "id": "deepseek/deepseek-v4-flash-0731",
        "label": "DeepSeek V4 Flash 0731 (NEW DEFAULT, top quality)",
        "tier": "top",
        "input": 0.14,
        "output": 0.28,
    },
    {
        "id": "deepseek/deepseek-v4-pro",
        "label": "DeepSeek V4 Pro (premium)",
        "tier": "top",
        "input": 0.44,
        "output": 0.87,
    },
    {
        "id": "deepseek/deepseek-r1",
        "label": "DeepSeek R1 (reasoning model)",
        "tier": "mid",
        "input": 0.70,
        "output": 2.50,
    },
    {
        "id": "qwen/qwen3.7-flash",
        "label": "Qwen 3.7 Flash (ultra budget)",
        "tier": "cheap",
        "input": 0.03,
        "output": 0.13,
    },
]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def get_api_key() -> str:
    """Return the API key.

    Priority order:
      1. st.session_state (Streamlit-native, survives reruns in the same session)
      2. MAPLEAD_OPENAI_API_KEY env var
      3. Streamlit secrets.toml
    """
    try:
        import streamlit as st
        if hasattr(st, "session_state") and st.session_state.get("MAPLEAD_OPENAI_API_KEY"):
            return st.session_state["MAPLEAD_OPENAI_API_KEY"]
    except Exception:
        pass
    env_key = os.environ.get("MAPLEAD_OPENAI_API_KEY", "")
    if env_key:
        return env_key
    try:
        import streamlit as st
        v = st.secrets.get("MAPLEAD_OPENAI_API_KEY", "") if hasattr(st, "secrets") else ""
        if v:
            return v
    except Exception:
        pass
    return ""


def get_base_url() -> str:
    """Return the API base URL. Defaults to OpenRouter for sk-or-* keys."""
    # 1. session state
    try:
        import streamlit as st
        if hasattr(st, "session_state") and st.session_state.get("MAPLEAD_OPENAI_BASE_URL"):
            return st.session_state["MAPLEAD_OPENAI_BASE_URL"]
    except Exception:
        pass
    # 2. env var
    env = os.environ.get("MAPLEAD_OPENAI_BASE_URL", "")
    if env:
        return env
    # 3. secrets
    try:
        import streamlit as st
        v = st.secrets.get("MAPLEAD_OPENAI_BASE_URL", "") if hasattr(st, "secrets") else ""
        if v:
            return v
    except Exception:
        pass
    # 4. Default based on key prefix
    if get_api_key().startswith("sk-or-"):
        return DEFAULT_BASE_URL
    return "https://api.openai.com/v1"


def get_model() -> str:
    """Return the active model id."""
    try:
        import streamlit as st
        if hasattr(st, "session_state") and st.session_state.get("MAPLEAD_OPENAI_MODEL"):
            return st.session_state["MAPLEAD_OPENAI_MODEL"]
    except Exception:
        pass
    env = os.environ.get("MAPLEAD_OPENAI_MODEL", "")
    if env:
        return env
    try:
        import streamlit as st
        v = st.secrets.get("MAPLEAD_OPENAI_MODEL", "") if hasattr(st, "secrets") else ""
        if v:
            return v
    except Exception:
        pass
    return DEFAULT_MODEL


def is_configured() -> bool:
    return bool(get_api_key())


def is_openrouter() -> bool:
    return "openrouter" in get_base_url().lower()


# ---------------------------------------------------------------------------
# Core call
# ---------------------------------------------------------------------------
def _call_llm(
    system: str,
    user: str,
    *,
    temperature: float = 0.4,
    max_tokens: int = 600,
    model: Optional[str] = None,
    json_mode: bool = False,
) -> str:
    """Make a chat completion call. Returns '' on any failure (silent fail)."""
    key = get_api_key()
    if not key:
        return ""
    try:
        import httpx
        base = get_base_url()
        use_model = model or get_model()
        headers = {"Authorization": f"Bearer {key}"}
        if is_openrouter():
            headers.update(OPENROUTER_HEADERS)
        payload = {
            "model": use_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        with httpx.Client(timeout=60) as client:
            r = client.post(
                f"{base.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
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
# Lead scoring (heuristic + AI)
# ---------------------------------------------------------------------------
SCORE_PROMPT = """You are a B2B sales coach scoring leads for an Indian signage vendor. \
Score this business 1\u201310 for likelihood to buy signage services \
(storefront signs, menu boards, LED displays, hoarding, interior signage).

Consider:
- Category fit (restaurants, retail, hotels, salons, jewellery = high; \
  hospitals/schools = medium; tech = low unless new office)
- Rating & reviews (more established = more spend)
- Has phone / website (reach + legitimacy)
- City (tier-1 = more budget)
- Any signals in the address/name that suggest visible storefront

Reply ONLY with valid JSON in this exact format:
{"score": <integer 1-10>, "reason": "<one short sentence>"}
"""


def score_lead(b: Business) -> dict:
    """Score a lead 1\u201310 + one-line reasoning. Falls back to heuristic if no API key."""
    fallback = _heuristic_score(b)
    if not is_configured():
        return {**fallback, "source": "heuristic"}
    biz_json = json.dumps({
        "name": b.name, "category": b.category, "rating": b.reviews_average,
        "reviews": b.reviews_count, "has_phone": bool(b.phone_number),
        "has_website": bool(b.website), "address": b.address,
    }, indent=2)
    out = _call_llm(SCORE_PROMPT, biz_json, temperature=0.1, max_tokens=120, json_mode=True)
    if not out:
        return {**fallback, "source": "heuristic"}
    try:
        cleaned = out.strip().strip("`").strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        parsed = json.loads(cleaned)
        score = int(parsed.get("score", fallback["score"]))
        return {
            "score": max(1, min(10, score)),
            "reason": parsed.get("reason", fallback["reason"]),
            "source": "ai",
        }
    except (ValueError, json.JSONDecodeError):
        return {**fallback, "source": "heuristic"}


def _heuristic_score(b: Business) -> dict:
    score = 5
    cat = (b.category or "").lower()
    high_fit = {
        "restaurant", "cafe", "coffee", "hotel", "salon", "jewelry", "jewellery",
        "boutique", "retail", "shop", "bar", "pub", "clinic", "pharmacy", "bakery",
        "gym", "school", "kirana", "general store", "sweet", "mithai",
    }
    if any(k in cat for k in high_fit):
        score += 2
    if b.reviews_average and b.reviews_average >= 4.0:
        score += 1
    if b.reviews_count and b.reviews_count >= 100:
        score += 1
    if b.website:
        score += 1
    score = max(1, min(10, score))
    if any(k in cat for k in high_fit):
        reason = "Visible storefront category \u2014 strong signage fit"
    elif b.reviews_average and b.reviews_average >= 4.5:
        reason = "High rating \u2014 established business"
    elif not b.phone_number:
        reason = "No phone \u2014 hard to reach"
    else:
        reason = "Average fit \u2014 generic heuristic"
    return {"score": score, "reason": reason}


# ---------------------------------------------------------------------------
# Message generators
# ---------------------------------------------------------------------------
WA_PROMPT = """You write short WhatsApp business messages for a local Indian signage company. \
Write a single WhatsApp opener in Hinglish-friendly English (the way Indians \
actually text on WhatsApp). Mention their business by name and one specific \
thing (category, rating, or location) so it doesn't feel like spam.

Constraints:
- Under 60 words
- 1 emoji max
- No hashtags
- No exclamation at the end (looks desperate)
- One specific offer (free quote / site visit / sample catalogue)
- Output ONLY the message, no preamble.
"""


def generate_whatsapp_message(b: Business, city: str = "your city") -> str:
    if not b.name:
        return ""
    if not is_configured():
        return _template_wa(b, city)
    biz = f"name={b.name}, category={b.category}, rating={b.reviews_average}, city={city}"
    out = _call_llm(WA_PROMPT, biz, temperature=0.7, max_tokens=150)
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


SCRIPT_PROMPT = """You are a friendly cold-call coach for a small Indian signage business. \
Write a 30-second cold-call script (under 80 words) for a salesperson to read \
on the phone. The script must:
- Open by naming the business and the city
- Mention one specific thing about their category or rating
- Pivot quickly to a concrete offer (free quote, sample visit)
- End with a soft ask (\u201ccan I drop by Thursday?\u201d)
Output ONLY the script, no preamble.
"""


def generate_cold_call_script(b: Business, city: str = "your city") -> str:
    if not b.name:
        return ""
    if not is_configured():
        return _template_script(b, city)
    biz = f"name={b.name}, category={b.category}, rating={b.reviews_average}, city={city}"
    out = _call_llm(SCRIPT_PROMPT, biz, temperature=0.7, max_tokens=200)
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


EMAIL_PROMPT = """You write short, professional outreach emails for an Indian signage vendor. \
Compose an email with a compelling subject line and a 4-6 sentence body.

Requirements:
- Subject: 6-9 words, references their business or category, no clickbait
- Body: address them by business name, one specific detail (rating/category), \
  one concrete offer (free site visit / sample portfolio), one clear call-to-action
- Tone: professional but warm, the way an Indian SME owner would write to another
- Output ONLY the email \u2014 subject on line 1, then a blank line, then body.
"""


def generate_email(b: Business, city: str = "your city") -> str:
    """Return full email (subject + body) for the lead."""
    if not b.name:
        return ""
    if not is_configured():
        return _template_email(b, city)
    biz = f"name={b.name}, category={b.category}, rating={b.reviews_average}, city={city}"
    out = _call_llm(EMAIL_PROMPT, biz, temperature=0.6, max_tokens=400)
    if not out or len(out) > 1500:
        return _template_email(b, city)
    return out


def _template_email(b: Business, city: str) -> str:
    cat = b.category or "business"
    subject = f"Signage ideas for {b.name}"
    body = (
        f"Hi,\n\nI came across {b.name} while looking at top-rated {cat} in "
        f"{city}. Your business looks great and I'd love to share a few signage "
        f"ideas that could help you stand out even more.\n\n"
        f"We design and install storefront signs, menu boards, LED displays and "
        f"hoardings for businesses like yours. I'd be happy to:\n"
        f"  \u2022 Share a few before/after photos from similar projects\n"
        f"  \u2022 Offer a free site visit and quote\n\n"
        f"Would any of this be useful? Either way, thanks for the great work "
        f"you're doing at {b.name}.\n\nBest,\n[Your name]\n[Your business]"
    )
    return f"Subject: {subject}\n\n{body}"


# ---------------------------------------------------------------------------
# Multi-variant generator (3 versions, pick the best)
# ---------------------------------------------------------------------------
VARIANTS_PROMPT = """Generate 3 distinct WhatsApp message variants for an Indian signage \
vendor reaching out to a local business. Each must:
- Be under 60 words
- Sound Hinglish-friendly (the way Indians text on WhatsApp)
- Reference the business by name and one specific detail
- Have a different angle: (1) free site visit offer, (2) recent work portfolio \
  showcase, (3) special launch discount

Reply ONLY with valid JSON:
{
  "variants": [
    {"angle": "<short label>", "message": "<message text>"},
    {"angle": "<short label>", "message": "<message text>"},
    {"angle": "<short label>", "message": "<message text>"}
  ]
}
"""


def generate_variants(b: Business, kind: str = "whatsapp", city: str = "your city") -> list[dict]:
    """Return 3 message variants. Falls back to a single template if no key."""
    if not is_configured() or not b.name:
        msg = _template_wa(b, city) if kind == "whatsapp" else _template_email(b, city)
        return [{"angle": "template fallback", "message": msg}]
    biz = f"name={b.name}, category={b.category}, rating={b.reviews_average}, city={city}"
    out = _call_llm(VARIANTS_PROMPT, biz, temperature=0.9, max_tokens=600, json_mode=True)
    if not out:
        return [{"angle": "template fallback", "message": _template_wa(b, city)}]
    try:
        cleaned = out.strip().strip("`").strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        parsed = json.loads(cleaned)
        variants = parsed.get("variants", [])
        return variants[:3] if variants else []
    except (ValueError, json.JSONDecodeError):
        return [{"angle": "template fallback", "message": _template_wa(b, city)}]


# ---------------------------------------------------------------------------
# Lead research + qualification
# ---------------------------------------------------------------------------
RESEARCH_PROMPT = """In 3\u20134 bullet points, summarize what this business likely does, \
who their customers are, and what their signage/visibility needs probably are. \
Base it on the limited data below. Be specific and actionable. Output ONLY bullets.
"""


def research_lead(b: Business) -> str:
    if not is_configured():
        return ""
    biz = (
        f"name={b.name}, category={b.category}, rating={b.reviews_average}, "
        f"reviews={b.reviews_count}, address={b.address}, has_website={'yes' if b.website else 'no'}"
    )
    return _call_llm("Be concise and specific.", RESEARCH_PROMPT + biz,
                    temperature=0.3, max_tokens=250)


QUALIFIER_PROMPT = """You are a B2B lead qualifier. Analyze this business for a signage \
salesperson and return structured JSON with:

{
  "qualified": "hot" | "warm" | "cold",
  "score": <1-10>,
  "best_pitch": "<30-second angle>",
  "objection_likely": "<what they'd push back on>",
  "best_time_to_call": "<e.g. weekday 11am-1pm>",
  "estimated_signage_budget": "<small/medium/large or INR range>",
  "follow_up_in_days": <integer 3-14>
}

Reply ONLY with the JSON object.
"""


def qualify_lead(b: Business, city: str = "") -> dict:
    """Return a structured qualification for the lead. Empty dict on failure."""
    if not b.name:
        return {}
    if not is_configured():
        return {"qualified": "unknown", "score": 0, "best_pitch": "", "note": "no API key"}
    biz = (
        f"name={b.name}, category={b.category}, rating={b.reviews_average}, "
        f"reviews={b.reviews_count}, address={b.address}, city={city}"
    )
    out = _call_llm(QUALIFIER_PROMPT, biz, temperature=0.3, max_tokens=400, json_mode=True)
    if not out:
        return {"qualified": "unknown", "note": "AI call failed"}
    try:
        cleaned = out.strip().strip("`").strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        return json.loads(cleaned)
    except (ValueError, json.JSONDecodeError):
        return {"qualified": "unknown", "note": "AI returned unparseable JSON"}


# ---------------------------------------------------------------------------
# Campaign strategist
# ---------------------------------------------------------------------------
STRATEGIST_PROMPT = """You are a lead-generation strategist for an Indian signage vendor. \
The user wants to find signage customers in <CITY> for <INDUSTRY>. \
Suggest the 5 BEST Google Maps search queries to run, optimized for finding \
businesses with high signage-spend potential.

Each query must be specific, return many results, and target businesses likely \
to need new/updated signage. Examples of good queries:
- "jewellery shops in <CITY>"
- "restaurants in <CITY>"
- "shopping malls in <CITY>"

Reply ONLY with valid JSON:
{
  "queries": [
    {"query": "<google maps search>", "why": "<one-line rationale>", "expected_volume": "low/medium/high"}
  ],
  "advice": "<2-3 sentence strategy note>"
}
"""


def suggest_queries(city: str, industry: str) -> dict:
    """Return campaign-strategy suggestions for a city + industry focus."""
    if not is_configured():
        return _fallback_queries(city, industry)
    user = f"Suggest 5 best Google Maps queries for a signage vendor targeting {industry} in {city}."
    out = _call_llm(STRATEGIST_PROMPT.replace("<CITY>", city).replace("<INDUSTRY>", industry),
                    user, temperature=0.6, max_tokens=500, json_mode=True)
    if not out:
        return _fallback_queries(city, industry)
    try:
        cleaned = out.strip().strip("`").strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
        return json.loads(cleaned)
    except (ValueError, json.JSONDecodeError):
        return _fallback_queries(city, industry)


def _fallback_queries(city: str, industry: str) -> dict:
    """Default query suggestions when AI is not available.

    Uses known high-value Google Maps query patterns for common Indian
    signage-customer industries. Better than a single generic query.
    """
    city = city.strip() or "your city"
    industry_lc = (industry or "").strip().lower() or "business"
    is_signage = "sign" in industry_lc

    signage_queries = [
        {"query": f"shopping malls in {city}",
         "why": "Malls buy huge exterior + interior signage (big contracts)",
         "expected_volume": "high"},
        {"query": f"jewellery shops in {city}",
         "why": "Jewellers spend BIG on gold-lit, decorative signage",
         "expected_volume": "high"},
        {"query": f"restaurants in {city}",
         "why": "Restaurants need menu boards + glowing storefront signs",
         "expected_volume": "high"},
        {"query": f"car dealerships in {city}",
         "why": "Showrooms need large exterior + interior signage",
         "expected_volume": "medium"},
        {"query": f"hotels in {city}",
         "why": "Hotels need facade, wayfinding + room signage",
         "expected_volume": "medium"},
        {"query": f"advertising agencies in {city}",
         "why": "Agencies sub-contract signage work to vendors (recurring)",
         "expected_volume": "low"},
        {"query": f"event organisers in {city}",
         "why": "Banners, standees, backdrops (recurring orders)",
         "expected_volume": "low"},
    ]
    generic_queries = [
        {"query": f"{industry} in {city}",
         "why": "Broad coverage", "expected_volume": "high"},
        {"query": f"top rated {industry} in {city}",
         "why": "Filters to established businesses with marketing budget",
         "expected_volume": "high"},
        {"query": f"new {industry} openings in {city}",
         "why": "New openings need fresh signage — very high intent",
         "expected_volume": "medium"},
        {"query": f"popular {industry} in {city}",
         "why": "High-traffic = high-visibility = signage-conscious",
         "expected_volume": "medium"},
        {"query": f"{industry} shops in {city}",
         "why": "Shops have storefronts = visible signage needs",
         "expected_volume": "high"},
    ]
    chosen = signage_queries if is_signage else generic_queries
    return {
        "queries": chosen,
        "advice": (
            f"No AI configured — using built-in {len(chosen)} curated query patterns "
            f"for '{industry}'. Add an OpenRouter key in ⚙ Settings for AI-tailored "
            f"suggestions specific to your city and goals."
        ),
    }



def estimate_cost(input_tokens: int, output_tokens: int) -> dict:
    """Rough USD cost for a call given the current model. Returns dict or None."""
    cur = next((m for m in POPULAR_MODELS if m["id"] == get_model()), None)
    if not cur:
        return None
    return {
        "input_usd": round(input_tokens / 1_000_000 * cur["input"], 6),
        "output_usd": round(output_tokens / 1_000_000 * cur["output"], 6),
        "total_usd": round(
            input_tokens / 1_000_000 * cur["input"]
            + output_tokens / 1_000_000 * cur["output"],
            6,
        ),
    }