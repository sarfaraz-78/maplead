"""
MapLead AI Core — rebuilt from scratch
========================================

ONE clean module for AI lead enrichment:

    from ai_core import AICore, heuristic_score, heuristic_outreach

    ai = AICore(api_key="sk-or-v1-...")   # OpenRouter, Anthropic, etc.
    print(ai.test())                     # {"ok": True, "provider": ..., "model": ...}

    # Always returns SOMETHING. Uses AI if available, heuristic otherwise.
    score_obj = ai.score_business(business)
    outreach_text = ai.outreach_for_business(business)

    # Pure heuristic (no AI ever needed):
    fallback = heuristic_score(business)

Design rules:
- One class, one config, no multi-source sprawl.
- is_working() pings the provider; never claim "configured" without proof.
- Every method has a working path AND a fallback path.
- No silent failures — returns plain dicts with 'source' key.
- Never hardcodes provider — auto-detects from key prefix.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from scraper import Business

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 60.0
HTTP_REFERRER = "https://github.com/sabsar42/maplead"
APP_TITLE = "MapLead AI"


# ---------------------------------------------------------------------------
# Provider registry — minimal, single source of truth
# ---------------------------------------------------------------------------

PROVIDERS: list[dict[str, Any]] = [
    {
        "key_prefixes": ("sk-or-v1-", "sk-or-"),
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "qwen/qwen3.7-flash",
        "extra_headers": {
            "HTTP-Referer": HTTP_REFERRER,
            "X-Title": APP_TITLE,
        },
    },
    {
        "key_prefixes": ("sk-ant-",),
        "name": "Anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-5-sonnet-latest",
        "extra_headers": {},
    },
    {
        "key_prefixes": ("sk-proj-",),
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "extra_headers": {},
    },
    {
        "key_prefixes": ("gsk_",),
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "extra_headers": {},
    },
    {
        "key_prefixes": ("fw_",),
        "name": "Fireworks",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "default_model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
        "extra_headers": {},
    },
]


def detect_provider(api_key: str) -> dict[str, Any] | None:
    """Return provider config dict if api_key matches a known prefix."""
    if not api_key:
        return None
    for prov in PROVIDERS:
        if any(api_key.startswith(p) for p in prov["key_prefixes"]):
            return prov
    return None


def mask_key(api_key: str) -> str:
    """First 7 + last 4 chars, rest masked. Empty -> 'empty'."""
    if not api_key:
        return "(empty)"
    if len(api_key) < 12:
        return f"{api_key[:3]}***"
    return f"{api_key[:7]}...{api_key[-4:]}"


# ---------------------------------------------------------------------------
# Heuristic scoring + templated outreach (always available)
# ---------------------------------------------------------------------------

@dataclass
class ScoreResult:
    """The result of any scoring call - AI or heuristic."""
    score: int = 0
    tier: str = "skip"     # "hot" / "warm" / "cold" / "skip"
    reason: str = ""
    outreach: str = ""
    category: str = "other"
    source: str = "heuristic"  # "ai" / "heuristic"

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "tier": self.tier,
            "reason": self.reason,
            "outreach": self.outreach,
            "category": self.category,
            "source": self.source,
        }


# Names → category mapping
CATEGORY_KEYWORDS: list[tuple[str, str]] = [
    ("cafe", "cafe"), ("coffee", "cafe"), ("tea stall", "cafe"),
    ("restaurant", "restaurant"), ("dhaba", "restaurant"), ("food", "restaurant"),
    ("hotel", "hotel"), ("lodging", "hotel"), ("resort", "hotel"),
    ("gym", "gym"), ("fitness", "gym"), ("yoga", "gym"),
    ("school", "education"), ("academy", "education"), ("coaching", "education"),
    ("tuition", "education"), ("college", "education"), ("institute", "education"),
    ("hospital", "medical"), ("clinic", "medical"), ("doctor", "medical"),
    ("dental", "medical"), ("diagnostic", "medical"),
    ("pharmacy", "pharmacy"), ("medical", "pharmacy"), ("chemist", "pharmacy"),
    ("salon", "salon"), ("beauty", "salon"), ("spa", "salon"), ("parlour", "salon"),
    ("shop", "retail"), ("store", "retail"), ("mart", "retail"), ("bazaar", "retail"),
    ("kirana", "retail"), ("general store", "retail"),
    ("plumber", "plumber"), ("electric", "electrician"),
    ("auto", "auto"), ("motor", "auto"), ("garage", "auto"), ("workshop", "auto"),
    ("lawyer", "legal"), ("advocate", "legal"), ("legal", "legal"),
    ("bank", "finance"), ("finan", "finance"), ("insurance", "finance"),
    ("bakery", "bakery"), ("sweet", "bakery"), ("confection", "bakery"),
    ("tailor", "tailor"), ("boutique", "boutique"),
    ("florist", "florist"), ("flower", "florist"),
    ("jewel", "jeweler"), ("jewel", "jeweler"),
    ("mobile", "mobile"), ("phone", "mobile"),
    ("computer", "tech"), ("it ", "tech"), ("software", "tech"),
    ("real estate", "realestate"), ("property", "realestate"),
    ("travel", "travel"), ("tour", "travel"),
    ("car rental", "autorental"), ("taxi", "autorental"), ("cab", "autorental"),
    ("temple", "religious"), ("mosque", "religious"), ("church", "religious"),
    ("ashram", "religious"), ("gurudwara", "religious"), ("masjid", "religious"),
]


def _guess_category(name: str, category: str | None) -> str:
    """Best-effort category tag from name or explicit category."""
    if category:
        c = category.lower().strip().replace(" ", "_")
        if c:
            return c[:32]
    if name:
        n = name.lower()
        for kw, tag in CATEGORY_KEYWORDS:
            if kw in n:
                return tag
    return "other"


def _extract_city(address: str | None) -> str:
    """Pull a likely city name out of a comma-separated address.

    Address-format heuristic (Indian / OSM / general):
    - 5+ parts: [street, area, city, state, country] -> city is parts[-3]
    - 4 parts:  [street, area, city+pin, state]     -> city is parts[-2]
    - 3 parts:  [street, area, city]                -> city is parts[-1]
    - 2 parts:  [area, city]                        -> city is parts[-1]
    - 1 part:   [city]                              -> parts[0]
    """
    if not address:
        return "your area"
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if len(parts) >= 5:
        return parts[-3]
    if len(parts) >= 4:
        return parts[-2]
    if parts:
        return parts[-1]
    return "your area"


def _build_outreach(b: Business, tier: str, src: str = "heuristic") -> str:
    """Generate a templated outreach message (no LLM needed).

    Args:
        b: Business
        tier: "hot" / "warm" / "cold" / "skip"
        src: marker ("ai" or "heuristic")
    """
    name = (b.name or "your business").strip()
    cat_raw = _guess_category(b.name, b.category)
    cat_nice = cat_raw.replace("_", " ")
    city = _extract_city(b.address)

    if tier == "hot":
        opener = f"Hi {name} team,"
        impression_bits = []
        if b.reviews_average and b.reviews_average >= 4.0:
            impression_bits.append(f"your strong {b.reviews_average:.1f}-star reputation")
        if b.reviews_count and b.reviews_count >= 50:
            impression_bits.append(f"your {b.reviews_count}+ happy customers")
        if not impression_bits:
            impression_bits.append(f"your presence in {city}")
        impression = " and ".join(impression_bits)
    elif tier == "warm":
        opener = f"Hello {name},"
        impression = f"your work in {city}"
    elif tier == "cold":
        opener = f"Hi {name},"
        impression = "your listing"
    else:  # skip
        opener = f"Hi {name},"
        impression = "your business"

    msg = (
        f"{opener}\n\n"
        f"Came across {name} in {city} and was impressed by {impression}.\n\n"
        f"I work with {cat_nice} businesses on practical improvements — "
        f"things like faster response times, smarter follow-ups, and simple automation "
        f"that usually pays back within a few weeks.\n\n"
        f"Would a 10-minute call this week be worth it?"
    )
    return msg


def heuristic_score(b: Business) -> ScoreResult:
    """Pure-heuristic score. Always succeeds, costs nothing, zero latency."""
    score = 0.0
    parts: list[str] = []

    if b.phone_number:
        digits = "".join(c for c in b.phone_number if c.isdigit() or c == "+")
        if 8 <= len(digits) <= 15:
            score += 2
            parts.append("phone")
        elif len(digits) >= 6:
            score += 1
            parts.append("phone(partial)")

    if b.website:
        if b.website.startswith(("http://", "https://")):
            score += 2
            parts.append("website")
        else:
            score += 1
            parts.append("website(weird)")

    if b.reviews_average is not None:
        if b.reviews_average >= 3.5:
            score += 2
            parts.append(f"rating={b.reviews_average:.1f}")
        elif b.reviews_average >= 2.0:
            score += 1
            parts.append(f"low-rating={b.reviews_average:.1f}")

    if b.reviews_count is not None:
        if b.reviews_count >= 5:
            score += 1
            if b.reviews_count >= 50:
                score += 0.5
                parts.append(f"reviews={b.reviews_count}")
            else:
                parts.append(f"reviews={b.reviews_count}")
        elif b.reviews_count >= 1:
            score += 0.5

    if b.latitude is not None and b.longitude is not None:
        score += 1
        parts.append("coords")

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
    elif b.name and any(w in b.name.lower() for w in ("closed", "shut", "defunct")):
        score = max(0, score - 3)
        parts.append("closed-in-name")

    final = min(int(round(score)), 10)

    if final >= 8:
        tier = "hot"
    elif final >= 5:
        tier = "warm"
    elif final >= 1:
        tier = "cold"
    else:
        tier = "skip"

    return ScoreResult(
        score=final,
        tier=tier,
        reason=", ".join(parts[:4]) or "no usable data",
        outreach=_build_outreach(b, tier),
        category=_guess_category(b.name, b.category),
        source="heuristic",
    )


# ---------------------------------------------------------------------------
# AI Core - one class, clean surface
# ---------------------------------------------------------------------------


class AICore:
    """All-in-one AI helper.

    Usage:
        ai = AICore(api_key="sk-or-v1-...")  # or None
        test = ai.test()                     # verify key works
        if ai.is_working():
            result = ai.score_business(b)
        else:
            result = heuristic_score(b)     # fallback
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        # Accept key from constructor, env var, or session_state
        self.api_key = (
            api_key
            or os.environ.get("MAPLEAD_OPENAI_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY")
            or ""
        )
        self.timeout = timeout

        if not self.api_key:
            self.provider: dict | None = None
            self.base_url = base_url or ""
            self.model = model or ""
            self._working = False
            return

        # Auto-detect provider from key prefix
        prov = detect_provider(self.api_key)
        if prov is None:
            # Unknown prefix - default to OpenRouter (most permissive)
            prov = PROVIDERS[0]

        self.provider = prov
        self.base_url = (
            base_url
            or os.environ.get("MAPLEAD_OPENAI_BASE_URL")
            or prov["base_url"]
        )
        self.model = (
            model
            or os.environ.get("MAPLEAD_OPENAI_MODEL")
            or prov["default_model"]
        )
        # Verify on init only if explicitly requested (cheap network call otherwise)
        self._working: bool | None = None  # unknown until test() called

    # ---- introspection ---------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def is_working(self) -> bool:
        """Cached result from last test() call. None if not tested yet."""
        return bool(self._working)

    def provider_name(self) -> str:
        return self.provider["name"] if self.provider else "none"

    def masked_key(self) -> str:
        return mask_key(self.api_key)

    def describe(self) -> dict[str, Any]:
        """One-liner snapshot of current state for debugging."""
        return {
            "configured": self.is_configured(),
            "working": self.is_working(),
            "provider": self.provider_name(),
            "model": self.model or "",
            "key": self.masked_key(),
        }

    # ---- LLM call ---------------------------------------------------------

    def _call(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        max_tokens: int = 300,
        temperature: float = 0.3,
    ) -> str:
        """Single completion call. Raises on failure."""
        if not self.api_key or not self.base_url or not self.model:
            raise RuntimeError("AI not configured (missing key / url / model)")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.provider and self.provider.get("extra_headers"):
            headers.update(self.provider["extra_headers"])

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
        if r.status_code != 200:
            raise RuntimeError(
                f"{self.provider_name()} returned {r.status_code}: {r.text[:200]}"
            )
        data = r.json()
        return data["choices"][0]["message"]["content"]

    # ---- verification -----------------------------------------------------

    def test(self) -> dict[str, Any]:
        """Ping the provider with a tiny call. Returns a status dict.

        Returns: {"ok": bool, "provider": str, "model": str, "error": str|None}
        """
        if not self.is_configured():
            return {
                "ok": False,
                "provider": "none",
                "model": "",
                "key": self.masked_key(),
                "error": "no key provided",
            }
        try:
            out = self._call(
                "You are a connectivity test.",
                "Reply with the word OK only.",
                max_tokens=5,
                temperature=0,
            )
            ok = bool(out) and "ok" in out.lower()[:20]
            self._working = ok
            return {
                "ok": ok,
                "provider": self.provider_name(),
                "model": self.model,
                "key": self.masked_key(),
                "error": None if ok else "empty response",
            }
        except Exception as exc:
            self._working = False
            return {
                "ok": False,
                "provider": self.provider_name(),
                "model": self.model,
                "key": self.masked_key(),
                "error": f"{type(exc).__name__}: {exc}"[:200],
            }

    # ---- public API -------------------------------------------------------

    def score_business(self, b: Business) -> ScoreResult:
        """Score a business. AI if working, heuristic otherwise."""
        if not self.is_working():
            hs = heuristic_score(b)
            return hs

        prompt = self._build_score_prompt(b)
        try:
            raw = self._call(prompt, "Score as JSON.", json_mode=True, max_tokens=120)
            data = self._parse_json_safe(raw)
            score = max(0, min(10, int(data.get("score", 0))))
            tier = data.get("tier", "cold")
            if tier not in ("hot", "warm", "cold", "skip"):
                tier = "hot" if score >= 8 else "warm" if score >= 5 else "cold" if score >= 1 else "skip"
            reason = (data.get("reason") or "")[:140]
            return ScoreResult(
                score=score,
                tier=tier,
                reason=reason + "  (AI)",
                outreach=_build_outreach(b, tier, src="ai"),
                category=_guess_category(b.name, b.category),
                source="ai",
            )
        except Exception as exc:
            logger.warning("AI score failed, falling back to heuristic: %s", exc)
            return heuristic_score(b)

    def outreach_for_business(self, b: Business) -> str:
        """Generate a personalized outreach message. AI if working, template otherwise."""
        if not self.is_working():
            return _build_outreach(b, "warm", src="heuristic")

        prompt = self._build_outreach_prompt(b)
        try:
            text = self._call(prompt, "Write the message.", max_tokens=200, temperature=0.5)
            return text.strip().strip('"').strip("'")
        except Exception as exc:
            logger.warning("AI outreach failed, falling back to template: %s", exc)
            return _build_outreach(b, "warm", src="heuristic")

    def categorize_business(self, b: Business) -> str:
        """One-word category. AI if working, heuristic otherwise."""
        # Heuristic is already pretty good; only use AI if user insists
        if not self.is_working():
            return _guess_category(b.name, b.category)
        try:
            text = self._call(
                "Output exactly one short lowercase tag.",
                f"Business: {b.name}\nCategory hint: {b.category or 'none'}\nReply with the tag only.",
                max_tokens=20,
                temperature=0,
            ).strip().split()[0]
            return text.lower().strip(".,;:")[:32] or _guess_category(b.name, b.category)
        except Exception:
            return _guess_category(b.name, b.category)

    # ---- batch -------------------------------------------------------------

    def enrich(self, businesses: list[Business], ops: list[str] | None = None) -> list[Business]:
        """Score/outreach/categorize a batch. Updates businesses in place.

        ops: subset of {"score", "outreach", "category"}. None = ["score"].
        """
        ops = ops or ["score"]
        for biz in businesses:
            if "score" in ops:
                res = self.score_business(biz)
                biz.ai_score = res.score
                biz.ai_tier = res.tier
                biz.ai_reason = res.reason
            if "outreach" in ops:
                biz.ai_outreach = self.outreach_for_business(biz)
            if "category" in ops:
                biz.ai_category = self.categorize_business(biz)
        return businesses

    # ---- prompts ---------------------------------------------------------

    @staticmethod
    def _build_score_prompt(b: Business) -> str:
        return (
            "You are a B2B lead-qualification analyst. Score this business 0-10 as a sales lead. "
            "Use JSON only.\n\n"
            "Scoring:\n"
            "- 8-10 (hot):   phone + website + clear category + rating evidence\n"
            "- 5-7  (warm):  2+ contact fields\n"
            "- 1-4  (cold):  sparse data\n"
            "- 0    (skip):  closed or irrelevant\n\n"
            f"Business:\n"
            f"- Name: {b.name}\n"
            f"- Category: {b.category or '(unknown)'}\n"
            f"- Address: {b.address or '(unknown)'}\n"
            f"- Phone: {b.phone_number or '(none)'}\n"
            f"- Website: {b.website or '(none)'}\n"
            f"- Rating: {b.reviews_average} ({b.reviews_count} reviews)\n\n"
            'JSON: {"score": <0-10 int>, "tier": "hot|warm|cold|skip", "reason": "<=15 words>"}'
        )

    @staticmethod
    def _build_outreach_prompt(b: Business) -> str:
        city = _extract_city(b.address)
        return (
            "Write a friendly, personalized B2B outreach message (40-60 words) to this business. "
            "Mention their name and city. Reference their category. Offer one concrete benefit. "
            "End with a soft question. Just the message, no preamble, no subject line.\n\n"
            f"Business: {b.name}\n"
            f"Category: {b.category or 'local business'}\n"
            f"City: {city}"
        )

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _parse_json_safe(raw: str) -> dict[str, Any]:
        """Parse JSON from a model that may wrap it in prose/code-fences."""
        if not raw:
            return {}
        # Strip common wrappers
        cleaned = raw.strip().strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
        # Try direct
        try:
            return json.loads(cleaned)
        except (ValueError, json.JSONDecodeError):
            pass
        # Fallback: extract first {...} block
        m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except (ValueError, json.JSONDecodeError):
                pass
        # Last-ditch: try to find any JSON object with nested braces
        depth = 0
        start = -1
        for i, c in enumerate(raw):
            if c == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        return json.loads(raw[start:i + 1])
                    except (ValueError, json.JSONDecodeError):
                        pass
                    start = -1
        return {}
