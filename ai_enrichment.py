"""
MapLead — AI lead enrichment via OpenRouter
============================================

Uses any OpenRouter-compatible model (Qwen, Llama, GPT, etc.) to add
intelligence on top of scraped leads:

  • score_lead()      — 0-10 quality score + hot/warm/cold tier
  • generate_outreach() — personalized 50-word B2B intro
  • categorize()      — short business-type tag

The API key is NEVER stored — pass via ``api_key=`` or set the
``OPENROUTER_API_KEY`` environment variable.

Cost: ~$0.0001-0.001 per business with Qwen 2.5 7B (basically free).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

import httpx

from scraper import Business
from provider_detect import detect_provider, mask_key

logger = logging.getLogger(__name__)


# Available models on OpenRouter. Update as new ones appear.
AVAILABLE_MODELS = {
    "Qwen 3.7 Flash (fast, $0.03/M in)":   "qwen/qwen3.7-flash",
    "Qwen 3.7 Plus (balanced)":            "qwen/qwen3.7-plus",
    "Qwen 3.7 Max (best quality)":         "qwen/qwen3.7-max",
    "Qwen 2.5 7B (legacy, very cheap)":    "qwen/qwen-2.5-7b-instruct",
    "Qwen 2.5 72B (legacy, larger)":       "qwen/qwen-2.5-72b-instruct",
    "Qwen 2.5 Coder 32B (best for JSON)":  "qwen/qwen-2.5-coder-32b-instruct",
    "Llama 3.1 8B (free)":                 "meta-llama/llama-3.1-8b-instruct:free",
    "Llama 3.3 70B":                       "meta-llama/llama-3.3-70b-instruct",
    "GPT-4o Mini":                         "openai/gpt-4o-mini",
    "GPT-3.5 Turbo":                       "openai/gpt-3.5-turbo",
}

DEFAULT_MODEL = "qwen/qwen3.7-flash"

BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT = 30.0


class AIEnricher:
    """Add AI smarts to scraped leads.

    Supports any OpenAI-compatible provider via auto-detection of the
    API key prefix (OpenRouter, Anthropic, OpenAI, Groq, Together, Fireworks).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        # Read key from multiple possible env vars
        self.api_key = (
            api_key
            or os.environ.get("MAPLEAD_OPENAI_API_KEY", "")
            or os.environ.get("OPENROUTER_API_KEY", "")
        )
        if not self.api_key:
            raise ValueError(
                "API key missing. Set MAPLEAD_OPENAI_API_KEY env var, paste in sidebar, "
                "or pass api_key=...  Get an OpenRouter key at https://openrouter.ai/keys"
            )

        # Auto-detect provider from key prefix
        provider = detect_provider(self.api_key)
        if provider is None:
            # Unknown prefix — default to OpenRouter which accepts any OpenAI-format key
            self.base_url = base_url or os.environ.get(
                "MAPLEAD_OPENAI_BASE_URL", "https://openrouter.ai/api/v1"
            )
            self.model = model or os.environ.get("MAPLEAD_OPENAI_MODEL", DEFAULT_MODEL)
            logger.warning(
                "Unknown API key prefix; defaulting to base_url=%s model=%s",
                self.base_url, self.model,
            )
        else:
            self.base_url = base_url or os.environ.get(
                "MAPLEAD_OPENAI_BASE_URL", provider.base_url
            )
            self.model = model or os.environ.get(
                "MAPLEAD_OPENAI_MODEL", provider.default_model
            )
            logger.info(
                "AI provider: %s | model: %s | key: %s",
                provider.name, self.model, mask_key(self.api_key),
            )

    # ----------------------------------------------------------------- public

    async def score_lead(self, business: Business) -> dict:
        """Score a business 0-10 with a short reason and tier (hot/warm/cold)."""
        prompt = self._score_prompt(business)
        raw = await self._call_llm(prompt, json_mode=True, max_tokens=200)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
        return {
            "score": int(data.get("score", 0)),
            "tier": data.get("tier", "cold"),
            "reason": data.get("reason", "").strip(),
        }

    async def generate_outreach(self, business: Business) -> str:
        """Generate a short personalized B2B outreach message."""
        prompt = self._outreach_prompt(business)
        text = await self._call_llm(prompt, max_tokens=120)
        return text.strip().strip('"').strip("'")

    async def categorize(self, business: Business) -> str:
        """One short tag describing the business type."""
        prompt = self._categorize_prompt(business)
        text = await self._call_llm(prompt, max_tokens=30)
        return text.strip().strip('"').strip("'").split("\n")[0]

    async def enrich_batch(
        self,
        businesses: list[Business],
        operations: list[str],
        progress_callback=None,
    ) -> list[Business]:
        """Run multiple enrichment ops on a list of businesses.

        Args:
            businesses: list of Business objects (mutated in-place)
            operations: subset of ``["score", "outreach", "category"]``
            progress_callback: optional ``async def(i, total, biz)`` hook
        """
        total = len(businesses)
        for i, biz in enumerate(businesses):
            try:
                if "score" in operations:
                    result = await self.score_lead(biz)
                    biz.ai_score = result["score"]
                    biz.ai_tier = result["tier"]
                    biz.ai_reason = result["reason"]
                if "outreach" in operations:
                    biz.ai_outreach = await self.generate_outreach(biz)
                if "category" in operations:
                    biz.ai_category = await self.categorize(biz)
            except Exception as exc:  # noqa: BLE001
                logger.warning("AI enrichment failed for %s: %s", biz.name, exc)
            if progress_callback:
                await progress_callback(i + 1, total, biz)
        return businesses

    # ----------------------------------------------------------------- prompts

    @staticmethod
    def _score_prompt(b: Business) -> str:
        return f"""You are a B2B lead-qualification analyst. Score this business 0-10 as a sales lead.

Scoring guide:
- 8-10 (hot):   has phone + website + clear category + ratings evidence of active business
- 5-7  (warm):  has 2+ contact fields + reasonable category
- 1-4  (cold):  sparse data, vague category, or no contact info
- 0    (skip):  wrong type (e.g. closed, government, irrelevant)

Business:
- Name: {b.name}
- Category: {b.category or "(unknown)"}
- Address: {b.address or "(unknown)"}
- Phone: {b.phone_number or "(none)"}
- Website: {b.website or "(none)"}
- Rating: {b.reviews_average} ({b.reviews_count} reviews)

Respond with ONLY a JSON object, no prose:
{{"score": <0-10 integer>, "tier": "hot|warm|cold|skip", "reason": "<=15 words why>"}}"""

    @staticmethod
    def _outreach_prompt(b: Business) -> str:
        city = ""
        if b.address:
            parts = [p.strip() for p in b.address.split(",")]
            city = parts[-2] if len(parts) >= 2 else parts[-1]
        return f"""Write a friendly, personalized B2B outreach message (40-60 words) to this business.
Mention their name and city. Reference their category. Offer one concrete benefit. End with a soft question.

Business: {b.name}
Category: {b.category or "local business"}
City: {city or "your area"}

Just the message, no preamble, no subject line."""

    @staticmethod
    def _categorize_prompt(b: Business) -> str:
        return f"""Classify this business into ONE short tag (1-3 words, lowercase).
Use plain types like: restaurant, cafe, plumber, dentist, gym, hotel, retail,
salon, auto_repair, medical, legal, real_estate, education, manufacturing,
wholesale, technology, other.

Business: {b.name}
Existing category: {b.category or "(none)"}
Address: {b.address or "(none)"}

Reply with ONLY the tag, nothing else."""

    # ----------------------------------------------------------------- LLM

    async def _call_llm(
        self,
        prompt: str,
        json_mode: bool = False,
        max_tokens: int = 200,
    ) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # OpenRouter requires these headers for app attribution
        if "openrouter.ai" in self.base_url:
            headers["HTTP-Referer"] = "https://github.com/sabsar42/maplead"
            headers["X-Title"] = "MapLead AI Enrichment"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        logger.debug("AI call: model=%s base=%s json=%s", self.model, self.base_url, json_mode)

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                json=body,
                headers=headers,
            )

        if response.status_code == 401:
            raise PermissionError(
                f"API key invalid or revoked (provider returned 401). "
                f"Check the key is correct for {self.base_url}. "
                f"Key in use: {mask_key(self.api_key)}"
            )
        if response.status_code == 402:
            raise PermissionError("Account out of credits / payment required")
        if response.status_code == 429:
            raise PermissionError("Rate limit hit — slow down or upgrade")
        if response.status_code == 404:
            raise PermissionError(
                f"Model '{self.model}' not found on {self.base_url}. "
                f"Try a different model name from the provider's model list."
            )
        response.raise_for_status()

        data = response.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected response shape: {data}") from exc