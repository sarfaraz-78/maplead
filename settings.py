"""
MapLead — Persistent user settings
====================================

Stores / retrieves user preferences in the SQLite `app_settings` table.

Use case: keep your name, company, message tone, etc. persisted across
browser refreshes, server restarts, and machines (so long as they share
the same maplead.db).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from database import LeadDB


# Setting key prefixes
MSG_CFG_PREFIX = "msg_"


# Sensible defaults applied to EVERY user on first run.
# Each default can be customized; the *values* are pre-filled so the
# UI never looks empty on day one.
DEFAULT_MSG_CFG: dict[str, str] = {
    "sender_name": "",
    "sender_company": "",
    "sender_role": "",
    "primary_channel": "email",
    "tone": "friendly",
    "industry_context": "I help local businesses grow with practical, fast-to-implement improvements.",
    "product_offer": "a free 1-page audit specific to your business",
    "custom_offer": "",
    "custom_cta": "Got 10 minutes this week to compare notes?",
}


def load_msg_cfg(db: LeadDB) -> dict[str, str]:
    """Load message-cfg from DB, filling missing keys with defaults."""
    saved = db.get_setting_dict(prefix=MSG_CFG_PREFIX)
    # Merge: defaults first, then saved values overwrite
    cfg = dict(DEFAULT_MSG_CFG)
    for k, v in saved.items():
        if v not in ("", None):
            cfg[k] = v
    return cfg


def save_msg_cfg(db: LeadDB, cfg: dict[str, str]) -> None:
    """Persist message-cfg to DB."""
    db.set_settings(cfg, prefix=MSG_CFG_PREFIX)


def reset_msg_cfg(db: LeadDB) -> dict[str, str]:
    """Wipe all saved msg_* settings and return the defaults."""
    with db._conn() as c:
        c.execute(
            "DELETE FROM app_settings WHERE key LIKE ?",
            (MSG_CFG_PREFIX + "%",),
        )
    return dict(DEFAULT_MSG_CFG)


# ---------------------------------------------------------------------------
# Sample presets - loadable from UI
# ---------------------------------------------------------------------------

SAMPLE_PRESETS: dict[str, dict[str, str]] = {
    "Real estate agent (Mumbai)": {
        "sender_name": "Priya Sharma",
        "sender_company": "Mumbai Homes Realty",
        "sender_role": "real estate advisor",
        "industry_context": (
            "I help Mumbai families find the right home — "
            "rental or purchase — in 30-45 days, with full paperwork handling."
        ),
        "product_offer": (
            "a free property shortlist based on your budget, location, and family size"
        ),
        "custom_offer": "",
        "custom_cta": "Got 15 minutes Saturday to chat about what you're looking for?",
        "tone": "friendly",
        "primary_channel": "whatsapp",
    },
    "SaaS founder (B2B)": {
        "sender_name": "Alex",
        "sender_company": "QuickReply AI",
        "sender_role": "co-founder",
        "industry_context": (
            "We make an AI receptionist for service businesses — "
            "missed-call recovery that texts the caller back in 30 seconds, 24/7."
        ),
        "product_offer": "a free 14-day trial, no credit card needed",
        "custom_offer": "",
        "custom_cta": "Would Tuesday at 2pm work for a 15-minute demo?",
        "tone": "direct",
        "primary_channel": "email",
    },
    "Local marketing agency": {
        "sender_name": "Jordan",
        "sender_company": "LocalLift Marketing",
        "sender_role": "marketing consultant",
        "industry_context": (
            "We run local SEO + Google Maps optimization for service-area businesses. "
            "Most clients double their inbound calls within 60 days."
        ),
        "product_offer": "a free 1-page SEO audit + 3 specific fixes",
        "custom_offer": "",
        "custom_cta": "Want me to send the audit before we get on a call?",
        "tone": "curious",
        "primary_channel": "email",
    },
    "Freelance designer": {
        "sender_name": "Sam",
        "sender_company": "Sam Design Studio",
        "sender_role": "freelance brand designer",
        "industry_context": (
            "I design logos, brand systems, and websites for early-stage businesses."
        ),
        "product_offer": "a free portfolio review + 2 specific suggestions",
        "custom_offer": "",
        "custom_cta": "Would next Tuesday work for a quick intro call?",
        "tone": "storytelling",
        "primary_channel": "email",
    },
    "Manufacturing / B2B sales": {
        "sender_name": "Ravi",
        "sender_company": "Precision Parts Pvt Ltd",
        "sender_role": "sales lead",
        "industry_context": (
            "We supply precision-machined parts to OEM manufacturers "
            "across automotive, aerospace, and industrial automation. ISO 9001 certified."
        ),
        "product_offer": "a sample shipment + capability deck",
        "custom_offer": "",
        "custom_cta": "Should I send our capability deck and a sample spec?",
        "tone": "formal",
        "primary_channel": "email",
    },
    "Just say hi (minimal)": {
        "sender_name": "",
        "sender_company": "",
        "sender_role": "",
        "industry_context": "",
        "product_offer": "",
        "custom_offer": "",
        "custom_cta": "",
        "tone": "friendly",
        "primary_channel": "email",
    },
}


def get_presets() -> list[tuple[str, dict[str, str]]]:
    """Return all presets as (name, config) tuples."""
    return list(SAMPLE_PRESETS.items())


def apply_preset(db: LeadDB, preset_name: str) -> dict[str, str]:
    """Replace saved msg-cfg with a preset. Returns the preset dict."""
    if preset_name not in SAMPLE_PRESETS:
        return load_msg_cfg(db)
    cfg = dict(SAMPLE_PRESETS[preset_name])
    save_msg_cfg(db, cfg)
    return cfg
