"""
MapLead — Multi-provider AI key auto-detection
==============================================

Detects which provider an API key belongs to based on prefix, returns
the correct base URL + default model. Used by ai_enrichment.py and any
future AI integration so the user can paste any provider's key without
configuring endpoints manually.

Supported providers (auto-detected by key prefix):
  * OpenRouter   sk-or-v1-... or sk-or-...
  * Anthropic    sk-ant-...
  * OpenAI       sk-proj-... or sk-... (48 chars)
  * Groq         gsk_...
  * Together AI  sk-... (long, sometimes other formats)
  * Fireworks    fw_...
  * Mistral      (custom)
  * Cohere       (custom)
  * Custom       any key + explicit base URL
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Provider:
    name: str
    base_url: str
    default_model: str
    key_prefixes: tuple[str, ...] = ()
    notes: str = ""


# Registry of known providers and their identifiers
PROVIDERS: dict[str, Provider] = {
    "openrouter": Provider(
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        default_model="qwen/qwen3.7-flash",
        key_prefixes=("sk-or-",),
        notes="Best value — free tier, hundreds of models, pay-as-you-go",
    ),
    "anthropic": Provider(
        name="Anthropic",
        base_url="https://api.anthropic.com/v1",
        default_model="claude-3-5-sonnet-latest",
        key_prefixes=("sk-ant-",),
        notes="Claude models, separate Anthropic Console billing",
    ),
    "openai": Provider(
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        key_prefixes=("sk-proj-",),
        notes="GPT-4o, GPT-3.5 — direct OpenAI billing",
    ),
    "groq": Provider(
        name="Groq",
        base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
        key_prefixes=("gsk_",),
        notes="Very fast inference, free tier, Llama + Mixtral",
    ),
    "fireworks": Provider(
        name="Fireworks AI",
        base_url="https://api.fireworks.ai/inference/v1",
        default_model="accounts/fireworks/models/llama-v3p3-70b-instruct",
        key_prefixes=("fw_",),
        notes="Fast open-source model hosting",
    ),
    "together": Provider(
        name="Together AI",
        base_url="https://api.together.xyz/v1",
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        notes="Together-hosted open models (no specific prefix)",
    ),
}


def detect_provider(api_key: str) -> Provider | None:
    """Auto-detect provider from API key prefix.

    Returns Provider or None if unknown prefix.
    """
    if not api_key:
        return None
    key = api_key.strip()

    for prov in PROVIDERS.values():
        for prefix in prov.key_prefixes:
            if key.startswith(prefix):
                return prov

    # Heuristic for bare sk- keys: check length to guess
    if key.startswith("sk-") and not key.startswith(("sk-or-", "sk-ant-", "sk-proj-")):
        # Could be OpenAI legacy or Together — default to OpenAI which is most common
        # but only if length matches OpenAI pattern (~51 chars)
        if 40 <= len(key) <= 60:
            return PROVIDERS["openai"]

    return None


def list_providers() -> list[dict]:
    """Return a list of all known providers for UI display."""
    return [
        {
            "key": k,
            "name": v.name,
            "base_url": v.base_url,
            "default_model": v.default_model,
            "prefixes": v.key_prefixes,
            "notes": v.notes,
        }
        for k, v in PROVIDERS.items()
    ]


def mask_key(api_key: str) -> str:
    """Show first 7 + last 4 chars of a key, mask the rest."""
    if not api_key or len(api_key) < 12:
        return "(too short)" if api_key else "(empty)"
    return f"{api_key[:7]}...{api_key[-4:]}"