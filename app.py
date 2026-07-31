"""
MapLead — Streamlit UI
======================

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime

import pandas as pd
import streamlit as st

from scraper import Business, BusinessList
from utils import compute_stats, export_csv, export_excel, export_json, export_phones_csv, export_vcard, export_excel_by_source, make_filename

import ai as ai_mod

try:
    from api_backends import get_backend, ScraperBackend
except ImportError:
    get_backend = None  # type: ignore[assignment]
    ScraperBackend = None  # type: ignore[assignment, misc]

try:
    from ai_core import (
        AICore,
        heuristic_score,
        heuristic_outreach,
        score_batch,
        TIER_EMOJI,
        TIER_COLORS,
        mask_key,
        detect_provider,
    )
    AI_OK = True
except ImportError:
    AI_OK = False
    AICore = None  # type: ignore[assignment,misc]  # type: ignore
    heuristic_score = None  # type: ignore[assignment,misc]
    heuristic_outreach = None  # type: ignore[assignment,misc]
    score_batch = None  # type: ignore[assignment,misc]
    TIER_EMOJI = {}  # type: ignore[assignment]
    TIER_COLORS = {}  # type: ignore[assignment]
    mask_key = None  # type: ignore[assignment,misc]
    detect_provider = None  # type: ignore[assignment,misc]  # type: ignore

# ---------------------------------------------------------------------------
# Default values for variables used by the runner function below.
# These get populated in the sidebar before each scrape, but we set
# safe defaults here so the runner never crashes with NameError if
# the user clicks "Get Leads" before the sidebar has rendered.
# ---------------------------------------------------------------------------
import os as _os_defaults
_os_defaults.environ.setdefault("MAPLEAD_OPENAI_API_KEY", "")
_os_defaults.environ.setdefault("OPENROUTER_API_KEY", "")
enable_ai: bool = False
openrouter_key: str = ""
ai_model_id: str = ""
ai_operations: list = []

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="MapLead — Google Maps Lead Generator",
    page_icon="🗺️",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "About": (
            "**MapLead** turns Google Maps searches into clean business leads. "
            "Built with Playwright + Streamlit. Use responsibly and respect Google's ToS."
        ),
        "Get Help": "https://github.com/sabsar42/Google-Map-Scrapper-Streamlit-Web",
    },
)

# ---------------------------------------------------------------------------
# Custom CSS — keeps it minimal & on-brand
# ---------------------------------------------------------------------------

CUSTOM_CSS = """
<style>
    /* Hide streamlit chrome */
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    header [data-testid="stToolbar"] { display: none; }

    /* Hero card */
    .hero {
        background: linear-gradient(135deg, #2563EB 0%, #7C3AED 100%);
        padding: 2rem 2.5rem;
        border-radius: 1rem;
        color: white;
        margin-bottom: 1.5rem;
        box-shadow: 0 12px 32px rgba(37, 99, 235, 0.25);
    }
    .hero h1 { margin: 0; font-size: 2.4rem; font-weight: 800; letter-spacing: -0.02em; }
    .hero p  { margin: 0.6rem 0 0 0; opacity: 0.92; font-size: 1.05rem; }

    /* Stat tiles */
    .stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.75rem; margin: 1rem 0 1.25rem 0; }
    .stat-tile {
        background: var(--background-color, white);
        border: 1px solid #E2E8F0;
        border-radius: 0.75rem;
        padding: 1rem 1.25rem;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }
    .stat-tile .value { font-size: 1.75rem; font-weight: 700; color: #2563EB; line-height: 1.1; }
    .stat-tile .label { font-size: 0.78rem; color: #64748B; text-transform: uppercase; letter-spacing: 0.05em; margin-top: 0.25rem; }

    /* Tip */
    .tip {
        background: #F1F5F9;
        border-left: 3px solid #2563EB;
        padding: 0.6rem 1rem;
        border-radius: 0.4rem;
        font-size: 0.9rem;
        color: #475569;
        margin: 0.75rem 0;
    }

    /* Footer */
    .footer {
        text-align: center;
        padding: 2rem 0 1rem 0;
        color: #94A3B8;
        font-size: 0.85rem;
    }
</style>
"""

st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Page navigation (sidebar radio)
# ---------------------------------------------------------------------------
PAGE_SCRAPE = "🔍 Scrape"
PAGE_CRM = "📇 CRM"
PAGE_DB = "🗄️ Database"
PAGE_STATS = "📊 Stats"
PAGE_SETTINGS = "⚙️ Settings"
page = st.sidebar.radio(
    "Navigation",
    [PAGE_SCRAPE, PAGE_CRM, PAGE_DB, PAGE_STATS, PAGE_SETTINGS],
    index=0,
    label_visibility="collapsed",
    key="nav_page",
)
st.sidebar.divider()


# ---------------------------------------------------------------------------
# Database + Security singletons
# ---------------------------------------------------------------------------
@st.cache_resource
def get_db():
    """Single shared LeadDB instance for this Streamlit session."""
    from database import LeadDB
    return LeadDB("maplead.db")


@st.cache_resource
def get_security():
    """Companion security object for the same DB file."""
    from security import DatabaseSecurity
    return DatabaseSecurity("maplead.db", audit_actor="streamlit")


# ---------------------------------------------------------------------------
# Hero
# ---------------------------------------------------------------------------

st.markdown(
    """
<div class="hero">
    <h1>🗺️ MapLead</h1>
    <p>Turn Google Maps searches into Excel-ready business leads — name, address, phone, website, rating, reviews.</p>
</div>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    if page == PAGE_SCRAPE:
        st.markdown("### ⚙️ Settings")
    
        # ---- Backend selector --------------------------------------------------
        backend_options = {
            "osm": "🆓 OpenStreetMap — 100% free, no signup, works in India",
            "justdial": "🇮🇳 JustDial — India-specific, ~$2/1k (~$5 free credit/mo)",
            "indiamart": "🇮🇳 IndiaMART — India-specific B2B, ~$2/1k (~$5 free/mo)",
            "foursquare": "⚠️ Foursquare — free tier BROKEN by Foursquare (410)",
            "yelp": "🆓 Yelp Fusion — 150k/mo free (limited India)",
            "playwright": "🆓 Playwright — free, brittle (stops at ~7 leads)",
            "botasaurus": "🛡️ Botasaurus — FREE, anti-detect, no key (best free option)",
            "outscraper": "💎 Outscraper — paid, robust (~$1/1k, works in India)",
            "serpapi": "💎 SerpApi — paid, robust (~$50/5k)",
        }
        env_backend = os.environ.get("SCRAPER_BACKEND", "osm").lower()
        default_idx = list(backend_options.keys()).index(env_backend) if env_backend in backend_options else 0
    
        backend_name = st.selectbox(
            "Scraper backend",
            options=list(backend_options.keys()),
            format_func=lambda k: backend_options[k],
            index=default_idx,
            help="For India: OSM (free, partial) or Foursquare (free, ratings). For best data: Outscraper (~$1/1k).",
        )
    
        # API key inputs (only show when needed)
        outscraper_key = ""
        serpapi_key = ""
        yelp_key = ""
        foursquare_key = ""
        apify_key = ""
        if backend_name == "outscraper":
            outscraper_key = st.text_input(
                "Outscraper API key",
                value=os.environ.get("OUTSCRAPER_API_KEY", ""),
                type="password",
                help="Get one free at https://app.outscraper.com/profile",
            )
            if not outscraper_key:
                st.warning("Outscraper API key required")
        elif backend_name == "serpapi":
            serpapi_key = st.text_input(
                "SerpApi API key",
                value=os.environ.get("SERPAPI_API_KEY", ""),
                type="password",
                help="Get one at https://serpapi.com/manage-api-key",
            )
            if not serpapi_key:
                st.warning("SerpApi API key required")
        elif backend_name == "yelp":
            yelp_key = st.text_input(
                "Yelp Fusion API key",
                value=os.environ.get("YELP_API_KEY", ""),
                type="password",
                help="Free 5,000 calls/day at https://www.yelp.com/developers/v3/manage_app",
            )
            if not yelp_key:
                st.warning("Yelp API key required")
            st.caption("Free tier: 5,000 calls/day = ~150,000 leads/month")
        elif backend_name == "foursquare":
            foursquare_key = st.text_input(
                "Foursquare API key",
                value=os.environ.get("FOURSQUARE_API_KEY", ""),
                type="password",
                help="Free 100,000 calls/month at https://foursquare.com/developers/",
            )
            if not foursquare_key:
                st.warning("Foursquare API key required")
            st.caption("Free tier: 100,000 calls/month, works in India")
        elif backend_name in ("justdial", "indiamart"):
            apify_key = st.text_input(
                "Apify API token",
                value=os.environ.get("APIFY_API_KEY", ""),
                type="password",
                help="Free $5/month credit at https://console.apify.com/",
            )
            if not apify_key:
                st.warning("Apify API token required")
            st.caption("Free $5/month credit ≈ 2,500 leads · $2 per 1,000 after")
    
        st.divider()
    
        # ---- Backend-specific options (only show when relevant) ---------------
        headless = True
        if backend_name == "playwright":
            headless = st.toggle(
                "Headless mode",
                value=True,
                help=(
                    "On: invisible browser (faster, more detectable). "
                    "Off: real visible window (slower, stealthier). Off requires a display."
                ),
            )
        elif backend_name == "osm":
            st.info(
                "💡 OSM queries need a **location**: e.g. \"coffee shops in Brooklyn\"",
                icon="ℹ️",
            )
        elif backend_name == "yelp":
            st.info(
                "💡 Yelp queries need a **location**: e.g. \"plumbers in Brooklyn\"",
                icon="ℹ️",
            )
        elif backend_name == "foursquare":
            st.info(
                "💡 Foursquare queries need a **location**: e.g. \"cafes in Mumbai\"",
                icon="ℹ️",
            )
        elif backend_name == "justdial":
            st.info(
                "🇮🇳 **JustDial queries need a city**: e.g. \"restaurants in Mumbai\"",
                icon="ℹ️",
            )
        elif backend_name == "indiamart":
            st.info(
                "🇮🇳 **IndiaMART queries are B2B products**: e.g. \"LED lights\" or \"cotton fabric\"",
                icon="ℹ️",
            )
        elif backend_name == "botasaurus":
            st.info(
                "🛡️ **Botasaurus** — anti-detect Google Maps scraper. Free, no API key. "
                "First run takes ~30s to launch Chrome. Best free option for 100+ leads.",
                icon="ℹ️",
            )
    
        locale = st.selectbox(
            "Search region (locale)",
            options=[
                ("en", "English (US)"),
                ("en-GB", "English (UK)"),
                ("en-IN", "English (India)"),
                ("hi", "हिन्दी (Hindi)"),
                ("de", "Deutsch"),
                ("fr", "Français"),
                ("es", "Español"),
                ("it", "Italiano"),
                ("pt-BR", "Português (Brasil)"),
                ("ja", "日本語"),
                ("ko", "한국어"),
                ("zh-CN", "中文 (简体)"),
                ("ar", "العربية"),
            ],
            format_func=lambda x: x[1],
            index=0,
            help="Affects the Google Maps domain, language and rating format",
        )[0]
    
        st.divider()
        st.markdown("### 🎯 Lead Packs")
        st.caption("One-click campaign presets. Runs multiple queries and merges the results.")
    
        # Use Botasaurus by default for packs — it's free and works for all of these.
        free_backend = backend_name if backend_name in ("botasaurus", "playwright") else "botasaurus"
    
        LEAD_PACKS: dict[str, list[dict]] = {
            "Signage Business \u2014 Hyderabad": [
                {"label": "🍽 Restaurants", "query": "restaurants in Hyderabad", "target": 50},
                {"label": "🛍 Shopping malls", "query": "shopping malls in Hyderabad", "target": 30},
                {"label": "🏨 Hotels", "query": "hotels in Hyderabad", "target": 50},
                {"label": "🏥 Hospitals & clinics", "query": "hospitals in Hyderabad", "target": 40},
                {"label": "📢 Advertising agencies", "query": "advertising agencies in Hyderabad", "target": 30},
                {"label": "🎉 Event organisers", "query": "event organisers in Hyderabad", "target": 25},
            ],
        }
    
        pack_name = st.selectbox(
            "Pick a campaign",
            options=list(LEAD_PACKS.keys()),
            index=0,
            key="lead_pack_select",
            help="Runs each query sequentially with the chosen backend and merges results.",
        )
        pack = LEAD_PACKS[pack_name]
    
        per_query = st.slider(
            "Leads per query",
            min_value=10,
            max_value=100,
            value=30,
            step=10,
            key="lead_pack_per_query",
            help="How many leads to scrape per query. Larger = slower.",
        )
    
        total_estimated = len(pack) * per_query
        est_minutes = total_estimated * 0.15  # ~9s per lead for Botasaurus
        st.caption(
            f"📊 {len(pack)} queries × {per_query} = ~{total_estimated} leads, "
            f"~{est_minutes:.0f} min with {free_backend}"
        )
    
        if st.button(
            f"🚀 Run \"{pack_name}\" campaign",
            type="primary",
            use_container_width=True,
            key="run_lead_pack",
        ):
            # Only set the activation flag here. Read the other widget values
            # directly from session_state at runtime (Streamlit reserves the
            # widget keys — you can't reassign to them after instantiation).
            st.session_state.lead_pack_active = True
    
        st.divider()
        st.markdown("### 📚 Tips")
        st.markdown(
            """
            - Be **specific** in your search term
              `"Coffee shops in Brooklyn"` beats `"coffee"`
            - Turn **off** headless if you hit CAPTCHAs
            - Larger batches take longer — start with 30
            - Don't run more than once per minute per IP
            - For production scale, switch to **Outscraper**
            """
        )
        st.divider()
        st.caption("v1.1 · MIT License")
    
    
    # ---------------------------------------------------------------------------
    # Search form
    # ---------------------------------------------------------------------------
    
if page == PAGE_SCRAPE:
    col1, col2 = st.columns([3, 1])
    with col1:
        search_term = st.text_input(
            "🔍 Search term",
            placeholder="e.g. Coffee Shops in Brooklyn, NY",
            label_visibility="visible",
        )
    with col2:
        total = st.number_input(
            "Max results",
            min_value=1,
            max_value=500,
            value=50,
            step=5,
        )

    with st.expander("🎯 Filters (applied after scraping)", expanded=False):
        f1, f2, f3 = st.columns(3)
        with f1:
            min_rating = st.slider("Min ★ rating", 0.0, 5.0, 0.0, 0.1)
        with f2:
            min_reviews = st.number_input("Min # reviews", min_value=0, value=0, step=10)
        with f3:
            require_website = st.checkbox("Must have website")
            require_phone = st.checkbox("Must have phone")

    # ---- Message customization panel (sets MAPLEAD_MSG_CFG in session_state) ----
    with st.expander("✏️ Customize my messages", expanded=False):
        from ai_messages import UserConfig
        from settings import (
            DEFAULT_MSG_CFG, get_presets, apply_preset, save_msg_cfg, load_msg_cfg, reset_msg_cfg,
        )

        # Auto-load from DB (overrides session_state so reloads restore)
        db = get_db()
        db_defaults = load_msg_cfg(db)

        # Quick presets: one-click load an example
        st.markdown("##### 🚀 Quick start")
        preset_names = [name for name, _ in get_presets()]
        preset_cols = st.columns(min(3, len(preset_names)))
        for i, preset_name in enumerate(preset_names[:3]):
            with preset_cols[i]:
                if st.button(preset_name, key=f"preset_top_{i}", use_container_width=True):
                    new_cfg = apply_preset(db, preset_name)
                    st.session_state["MAPLEAD_MSG_CFG"] = new_cfg
                    st.success(f"✅ Loaded preset: {preset_name}")
                    st.rerun()
        if st.button("⋯ show more presets", key="preset_more"):
            for preset_name in preset_names[3:]:
                if st.button(preset_name, key=f"preset_{preset_name}", use_container_width=True):
                    new_cfg = apply_preset(db, preset_name)
                    st.session_state["MAPLEAD_MSG_CFG"] = new_cfg
                    st.success(f"✅ Loaded preset: {preset_name}")
                    st.rerun()

        # Merge: session_state overrides DB, DB overrides defaults
        _mc_existing = dict(db_defaults)
        _mc_existing.update(st.session_state.get("MAPLEAD_MSG_CFG", {}) or {})

        st.caption("Auto-filled with sensible defaults. Edit anything — saves to DB permanently.")

        c1, c2 = st.columns(2)
        with c1:
            _sender_name = st.text_input(
                "Your name",
                value=_mc_existing.get("sender_name", DEFAULT_MSG_CFG["sender_name"]),
                placeholder="e.g. Vikram",
                help="Appears in message signatures",
            )
            _sender_company = st.text_input(
                "Your company / brand",
                value=_mc_existing.get("sender_company", DEFAULT_MSG_CFG["sender_company"]),
                placeholder="e.g. QuickReply AI",
                help="Optional. Mentioned in your intro and offers.",
            )
            _sender_role = st.text_input(
                "Your role",
                value=_mc_existing.get("sender_role", DEFAULT_MSG_CFG["sender_role"]),
                placeholder="e.g. growth consultant",
            )
            _primary_channel = st.selectbox(
                "Primary outreach channel",
                options=["email", "whatsapp", "call"],
                index=["email", "whatsapp", "call"].index(
                    _mc_existing.get("primary_channel", "email")
                ),
                help="Which variant to optimize primarily",
            )
            _tone = st.selectbox(
                "Tone",
                options=["friendly", "formal", "direct", "storytelling", "curious"],
                index=["friendly", "formal", "direct", "storytelling", "curious"].index(
                    _mc_existing.get("tone", "friendly")
                ),
                help="Affects angle selection and opener phrasing",
            )
        with c2:
            _industry_context = st.text_area(
                "What you do (1-2 sentences)",
                value=_mc_existing.get("industry_context", DEFAULT_MSG_CFG["industry_context"]),
                placeholder="e.g. We help local businesses automate WhatsApp replies.",
                height=80,
                help="Used in offer line",
            )
            _product_offer = st.text_area(
                "Specific thing you're offering",
                value=_mc_existing.get("product_offer", DEFAULT_MSG_CFG["product_offer"]),
                placeholder="e.g. 14-day free trial, no card needed",
                height=60,
            )
            _custom_offer = st.text_area(
                "Custom offer (verbatim, overrides default)",
                value=_mc_existing.get("custom_offer", DEFAULT_MSG_CFG["custom_offer"]),
                placeholder="Sentences describing your offer verbatim",
                height=60,
            )
            _custom_cta = st.text_input(
                "Custom closing question",
                value=_mc_existing.get("custom_cta", DEFAULT_MSG_CFG["custom_cta"]),
                placeholder="e.g. Got Tuesday at 3pm?",
            )

        # Save button + reset
        col_save, col_reset = st.columns(2)
        with col_save:
            save_clicked = st.button(
                "💾 Save to DB", type="primary", use_container_width=True,
                help="Persist all customizations across browser sessions",
            )
        with col_reset:
            reset_clicked = st.button(
                "🔄 Reset to defaults", use_container_width=True,
            )

        # Capture all values into session state immediately (autosaves on rerun)
        st.session_state["MAPLEAD_MSG_CFG"] = {
            "sender_name": _sender_name,
            "sender_company": _sender_company,
            "sender_role": _sender_role,
            "primary_channel": _primary_channel,
            "tone": _tone,
            "industry_context": _industry_context,
            "product_offer": _product_offer,
            "custom_offer": _custom_offer,
            "custom_cta": _custom_cta,
        }

        if save_clicked:
            save_msg_cfg(db, st.session_state["MAPLEAD_MSG_CFG"])
            st.success("✅ Saved permanently to maplead.db")
        if reset_clicked:
            cfg = reset_msg_cfg(db)
            st.session_state["MAPLEAD_MSG_CFG"] = cfg
            st.success("🔄 Reset to defaults")
            st.rerun()

    # ---- AI enrichment panel (sets MAPLEAD_AI_CFG in session_state) ----
    with st.expander("🤖 AI enrichment (optional)", expanded=False):
        from provider_detect import detect_provider, mask_key
        _ai_existing_key = st.session_state.get("MAPLEAD_OPENAI_API_KEY", "")
        _ai_enable = st.checkbox(
            "Enable AI scoring/outreach after scraping",
            value=bool(_ai_existing_key),
            help="Uses OpenRouter (or any compatible provider) to score leads and/or generate outreach.",
        )
        _ai_key_input = st.text_input(
            "AI API key",
            value=_ai_existing_key,
            type="password",
            placeholder="sk-or-v1-... (auto-detects provider)",
            help="Auto-detects OpenRouter, Anthropic, OpenAI, Groq, Together, Fireworks. Not stored on disk.",
        )
        if _ai_key_input:
            _prov = detect_provider(_ai_key_input)
            if _prov:
                st.caption(f"🔎 Detected: **{_prov.name}** → `{_prov.base_url}`")
            else:
                st.caption("⚠️ Unknown key format — will default to OpenRouter")

        _ai_model = st.text_input(
            "Model (optional, leave blank for provider default)",
            value="",
            placeholder="qwen/qwen3.7-flash",
        )
        _ai_ops = st.multiselect(
            "AI operations",
            options=["score", "outreach", "category"],
            default=["score"],
            format_func=lambda x: {"score": "🎯 Score", "outreach": "✉️ Outreach", "category": "🏷️ Categorize"}[x],
        )
        # Save to session_state
        st.session_state["MAPLEAD_AI_CFG"] = {
            "enabled": _ai_enable and bool(_ai_key_input),
            "api_key": _ai_key_input,
            "model": _ai_model or "",
            "operations": _ai_ops,
        }
        if _ai_enable and _ai_key_input:
            st.success(f"✅ AI enabled — key `{mask_key(_ai_key_input)}`")
        elif _ai_enable:
            st.warning("Enable needs an API key")

    run = st.button("🚀 Get Leads", type="primary", use_container_width=True)


    # ---------------------------------------------------------------------------
    # State
    # ---------------------------------------------------------------------------

    if "results" not in st.session_state:
        st.session_state.results: BusinessList | None = None
    if "run_meta" not in st.session_state:
        st.session_state.run_meta: dict = {}
    if "error" not in st.session_state:
        st.session_state.error: str | None = None


    # ---------------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------------


    def apply_filters(
        businesses: list[Business],
        min_rating: float,
        min_reviews: int,
        require_website: bool,
        require_phone: bool,
    ) -> list[Business]:
        out = businesses
        if min_rating > 0:
            out = [b for b in out if (b.reviews_average or 0) >= min_rating]
        if min_reviews > 0:
            out = [b for b in out if (b.reviews_count or 0) >= min_reviews]
        if require_website:
            out = [b for b in out if b.website]
        if require_phone:
            out = [b for b in out if b.phone_number]
        return out


    def render_stat_tiles(stats: dict) -> None:
        """Render the 4-up stat tiles as raw HTML (avoid 4 separate st.metric calls)."""
        avg_rating = (
            f"⭐ {stats['avg_rating']:.2f}" if stats["avg_rating"] is not None else "—"
        )
        avg_reviews = (
            f"{stats['avg_reviews']:.0f}" if stats["avg_reviews"] is not None else "—"
        )
        website_str = f"{stats['with_website']} ({stats['website_pct']:.0f}%)"
        phone_str = f"{stats['with_phone']} ({stats['phone_pct']:.0f}%)"

        st.markdown(
            f"""
    <div class="stat-grid">
        <div class="stat-tile"><div class="value">{stats['total']}</div><div class="label">Total leads</div></div>
        <div class="stat-tile"><div class="value">{avg_rating}</div><div class="label">Avg rating</div></div>
        <div class="stat-tile"><div class="value">{avg_reviews}</div><div class="label">Avg reviews</div></div>
        <div class="stat-tile"><div class="value">{website_str}</div><div class="label">With website</div></div>
    </div>
    """,
            unsafe_allow_html=True,
        )
        st.caption(f"With phone: {phone_str}")


    # ---------------------------------------------------------------------------
    # Run scrape
    # ---------------------------------------------------------------------------

    if run:
        if not search_term.strip():
            st.error("Please enter a search term.")
        else:
            st.session_state.results = None
            st.session_state.error = None

            progress_bar = st.progress(0.0, text="Starting browser...")
            status = st.empty()

            async def runner() -> tuple[BusinessList | None, float, str | None]:
                async def update(current: int, total_n: int) -> None:
                    pct = (current / total_n) if total_n else 0
                    progress_bar.progress(
                        min(pct, 1.0),
                        text=f"Scraping… {current}/{total_n}",
                    )

                start = time.time()
                try:
                    if get_backend is None:
                        raise RuntimeError("api_backends.py not available — reinstall the app")

                    # Build the selected backend with API keys from the sidebar
                    if backend_name == "outscraper" and outscraper_key:
                        os.environ["OUTSCRAPER_API_KEY"] = outscraper_key
                    elif backend_name == "serpapi" and serpapi_key:
                        os.environ["SERPAPI_API_KEY"] = serpapi_key
                    elif backend_name == "yelp" and yelp_key:
                        os.environ["YELP_API_KEY"] = yelp_key
                    elif backend_name == "foursquare" and foursquare_key:
                        os.environ["FOURSQUARE_API_KEY"] = foursquare_key
                    elif backend_name in ("justdial", "indiamart") and apify_key:
                        os.environ["APIFY_API_KEY"] = apify_key

                    # Pull AI config from session_state so we don't depend
                    # on sidebar-scope variables that may be undefined.
                    _ai_cfg = st.session_state.get("MAPLEAD_AI_CFG", {}) or {}
                    _ai_key = (
                        _ai_cfg.get("api_key")
                        or st.session_state.get("MAPLEAD_OPENAI_API_KEY", "")
                        or os.environ.get("OPENROUTER_API_KEY", "")
                    )
                    _ai_enabled = _ai_cfg.get("enabled", False)
                    _ai_model = (
                        _ai_cfg.get("model")
                        or st.session_state.get("MAPLEAD_OPENAI_MODEL", "")
                        or "qwen/qwen3.7-flash"
                    )
                    _ai_ops = _ai_cfg.get("operations") or ["score"]

                    if _ai_enabled and _ai_key:
                        os.environ["OPENROUTER_API_KEY"] = _ai_key
                        os.environ["MAPLEAD_OPENAI_API_KEY"] = _ai_key

                    backend = get_backend(backend_name)
                    result = await backend.scrape(
                        search_term=search_term.strip(),
                        total=int(total),
                        headless=headless,
                        locale=locale,
                        progress_callback=update,
                    )

                    # AI enrichment step (runs after scrape).
                    # ai_core.AICore handles BOTH AI and heuristic paths
                    # - no silent failures, no missing fields.
                    if result and result.business_list and AICore is not None:
                        ai_instance = AICore(
                            api_key=_ai_key or None,
                            model=_ai_model or None,
                        )
                        test_status = ai_instance.test()
                        ai_works = test_status["ok"]
                        ai_err = test_status.get("error")

                        # Run enrichment (AI if working, else heuristic-only)
                        if _ai_enabled and ai_works and _ai_ops:
                            try:
                                from ai_messages import enrich_leads_with_messages, UserConfig
                                _msg_cfg = UserConfig(
                                    **st.session_state.get("MAPLEAD_MSG_CFG", {}) or {}
                                )
                                total_biz = len(result.business_list)
                                # Use the new per-lead message engine for full uniqueness
                                enrich_leads_with_messages(
                                    result.business_list,
                                    ai=ai_instance,
                                    channel=_msg_cfg.primary_channel,
                                    config=_msg_cfg,
                                )
                                for i in range(total_biz):
                                    await update(total_biz + i + 1, total_biz * 2)
                            except Exception as exc:
                                ai_err = f"{type(exc).__name__}: {exc}"
                                logger.warning("AI message enrich failed: %s", exc)
                        else:
                            # Even without AI, generate unique template messages
                            try:
                                from ai_messages import enrich_leads_with_messages, UserConfig
                                _msg_cfg2 = UserConfig(
                                    **st.session_state.get("MAPLEAD_MSG_CFG", {}) or {}
                                )
                                enrich_leads_with_messages(
                                    result.business_list, ai=None, config=_msg_cfg2,
                                )
                            except Exception as exc:
                                logger.warning("Template message fill failed: %s", exc)

                        # ALWAYS fill any gaps with heuristic (no field ever stays blank)
                        for biz in result.business_list:
                            hs = heuristic_score(biz)
                            if biz.ai_score is None:
                                biz.ai_score = hs.score
                                biz.ai_tier = hs.tier
                                biz.ai_reason = hs.reason + ("  (heuristic)" if not ai_works else "")
                            if not biz.ai_outreach:
                                biz.ai_outreach = hs.outreach
                            if not biz.ai_category:
                                biz.ai_category = hs.category
                            # Fill empty multi-channel messages with template fallback
                            if not biz.ai_body_email:
                                biz.ai_body_email = hs.outreach
                            if not biz.ai_subject:
                                biz.ai_subject = f"Quick note for {biz.name or 'you'}"
                            if not biz.ai_whatsapp:
                                biz.ai_whatsapp = hs.outreach.split("\n")[0] if hs.outreach else ""

                        # Save status for UI
                        st.session_state["LAST_AI_STATUS"] = {
                            "configured": _ai_enabled and bool(_ai_key),
                            "attempted": _ai_enabled and bool(_ai_key) and bool(_ai_ops),
                            "succeeded": ai_works,
                            "error": ai_err,
                            "n_businesses": len(result.business_list),
                            "model": test_status.get("model", ""),
                            "provider": test_status.get("provider", ""),
                            "key": test_status.get("key", ""),
                            "fallback_used": not ai_works and (_ai_enabled and bool(_ai_key)),
                        }

                    return result, time.time() - start, None
                except Exception as exc:  # noqa: BLE001
                    return None, time.time() - start, str(exc)

            with status.status("Working…"):
                biz_list, elapsed, error = asyncio.run(runner())

            progress_bar.empty()
            status.empty()

            if error:
                st.session_state.error = error
                st.error(f"❌ Scraping failed: {error}")
                # Backend-specific suggestions
                if backend_name == "justdial":
                    st.info(
                        "💡 JustDial tips:\n"
                        "- Query must include a city: `restaurants in Mumbai`\n"
                        "- Use JustDial's exact city names (e.g. *Bengaluru*, not *Bangalore*)\n"
                        "- If a category returns 0 results, try a broader category\n"
                        "- Check your Apify credit balance at console.apify.com/billing",
                        icon="🇮🇳",
                    )
                elif backend_name == "indiamart":
                    st.info(
                        "💡 IndiaMART tips:\n"
                        "- Use B2B product terms: `LED lights`, `cotton fabric`, `stainless steel pipes`\n"
                        "- Add a location for local suppliers: `stainless steel pipes in Mumbai`\n"
                        "- Avoid consumer terms like *restaurants* or *salons*\n"
                        "- Check your Apify credit balance at console.apify.com/billing",
                        icon="🇮🇳",
                    )
                elif backend_name == "outscraper":
                    st.info(
                        "💡 Try: reduce **max results**, or wait a minute — "
                        "Google may have rate-limited your IP.",
                        icon="💎",
                    )
                elif backend_name == "serpapi":
                    st.info(
                        "💡 SerpApi tip: reduce **max results** or check your quota at serpapi.com/dashboard",
                        icon="💎",
                    )
                elif backend_name in ("yelp", "foursquare"):
                    st.info(
                        "💡 Make sure the query includes a **location**: "
                        'e.g. `plumbers in Brooklyn` or `cafes in Mumbai`',
                        icon="⚠️",
                    )
                else:
                    st.info(
                        "💡 Try: toggle **off** headless mode, reduce **max results**, "
                        "or wait a minute — Google may have rate-limited your IP."
                    )
            else:
                st.session_state.results = biz_list
                st.session_state.last_search_term = search_term.strip()
                st.session_state.last_backend = backend_name
                # Auto-save to database (silent — fail-safe: don't break UI on DB errors)
                try:
                    db = get_db()
                    summary = db.upsert_many(
                        biz_list.business_list,
                        source_query=search_term.strip(),
                        backend=backend_name,
                    )
                    st.session_state.db_save_summary = summary
                except Exception as exc:
                    st.session_state.db_save_summary = {"error": str(exc)}
                st.session_state.run_meta = {
                    "elapsed": elapsed,
                    "count": len(biz_list.business_list) if biz_list else 0,
                    "source": search_term.strip(),
                }
                st.success(
                    f"✅ Found {len(biz_list.business_list) if biz_list else 0} businesses in {elapsed:.1f}s"
                )
                # Surface DB save summary
                save_sum = st.session_state.get("db_save_summary") or {}
                if save_sum and "error" not in save_sum:
                    st.caption(
                        f"💾 Saved to DB: {save_sum.get('inserted', 0)} new, "
                        f"{save_sum.get('updated', 0)} updated, "
                        f"{save_sum.get('unchanged', 0)} unchanged"
                    )
                elif save_sum.get("error"):
                    st.caption(f"⚠️ DB save failed: {save_sum['error']}")
                st.rerun()


    # ---------------------------------------------------------------------------
    # Lead Pack runner — runs multiple queries sequentially and merges results
    # ---------------------------------------------------------------------------

    if st.session_state.get("lead_pack_active"):
        # Read widget values directly from session_state — Streamlit stores them
        # under their widget keys. We must NOT reassign to those keys.
        pack_name: str = st.session_state.get("lead_pack_select", "Campaign")
        pack: list[dict] = LEAD_PACKS.get(pack_name, [])
        per_query: int = st.session_state.get("lead_pack_per_query", 30)
        pack_backend: str = backend_name if backend_name in ("botasaurus", "playwright") else "botasaurus"

        # Reset the flag so the button click doesn't re-trigger on rerun
        st.session_state.lead_pack_active = False

        if not pack or get_backend is None:
            st.error("Lead Pack misconfigured or backends unavailable.")
        else:
            st.markdown(f"### 🚀 Running campaign: **{pack_name}**")
            st.caption(f"Using `{pack_backend}` backend — this may take several minutes.")

            overall_status = st.status(
                f"Starting campaign… 0/{len(pack)} queries done",
                expanded=True,
            )
            progress_bar = st.progress(0.0)
            all_businesses = []
            failed_queries = []
            t_start = time.time()

            async def run_campaign() -> tuple[list, list]:
                backend = get_backend(pack_backend)
                collected: list = []
                failed: list = []
                for i, item in enumerate(pack):
                    q = item["query"]
                    t = item.get("target") or per_query
                    overall_status.update(
                        label=f"[{i + 1}/{len(pack)}] {item['label']} — `{q}`",
                    )
                    try:
                        sub = await backend.scrape(
                            search_term=q,
                            total=t,
                            headless=headless,
                            locale=locale,
                            progress_callback=None,
                        )
                        added = 0
                        for biz in sub.business_list:
                            biz.__dict__["source_query"] = q  # tag for later filtering
                            if biz not in collected:
                                collected.append(biz)
                                added += 1
                        overall_status.write(f"  → {added} new leads")
                    except Exception as exc:
                        overall_status.write(f"  → ❌ failed: {exc}")
                        failed.append({"query": q, "error": str(exc)})
                    progress_bar.progress((i + 1) / len(pack))
                return collected, failed

            biz_list, failed = asyncio.run(run_campaign())
            elapsed = time.time() - t_start
            overall_status.update(
                label=f"✅ Campaign complete — {len(biz_list)} unique leads in {elapsed / 60:.1f} min",
                state="complete",
            )

            # Build a BusinessList so the rest of the UI works unchanged
            from scraper import BusinessList
            combined = BusinessList()
            for b in biz_list:
                combined.add(b)
            st.session_state.results = combined
            # Auto-save all campaign results to the database
            try:
                db = get_db()
                summary = db.upsert_many(
                    biz_list,
                    source_query=pack_name,
                    backend=pack_backend,
                )
                st.session_state.db_save_summary = summary
            except Exception as exc:
                st.session_state.db_save_summary = {"error": str(exc)}
            st.session_state.run_meta = {
                "elapsed": elapsed,
                "count": len(biz_list),
                "pack": pack_name,
                "failed": failed,
            }
            if failed:
                with st.expander(f"⚠️ {len(failed)} query/queries failed"):
                    for f in failed:
                        st.write(f"- **{f['query']}**: {f['error']}")
            st.success(f"✅ {len(biz_list)} unique leads from {len(pack)} queries")
            st.rerun()


    # ---------------------------------------------------------------------------
    # Render results
    # ---------------------------------------------------------------------------

    if st.session_state.results and st.session_state.results.business_list:
        biz_list: BusinessList = st.session_state.results
        all_businesses = biz_list.business_list

        # Apply filters
        filtered = apply_filters(
            all_businesses, min_rating, min_reviews, require_website, require_phone
        )

        stats = compute_stats(all_businesses)
        render_stat_tiles(stats)

        # ---- AI status banner -------------------------------------------------
        _ai_status = st.session_state.get("LAST_AI_STATUS")
        if _ai_status:
            if _ai_status.get("succeeded"):
                st.success(
                    f"🤖 **AI active** — {_ai_status.get('provider','')} "
                    f"({_ai_status.get('model','?')}) "
                    f"generated unique messages for {_ai_status.get('n_businesses', 0)} leads"
                )
            elif _ai_status.get("attempted") and _ai_status.get("error"):
                _err = _ai_status.get('error', '')[:180]
                st.warning(
                    f"⚠️ **AI call failed** — `{_err}`\n\n"
                    f"**Falling back to template messages** (these ARE being generated — "
                    f"scroll down to **✉️ Unique Outreach Messages** section below to see them).\n\n"
                    f"**To enable real AI:** get a working key at https://openrouter.ai/keys "
                    f"(free, takes 30 seconds, no card needed)."
                )
            elif _ai_status.get("configured") and not _ai_status.get("attempted"):
                st.info(
                    "🧮 AI enabled but no operations selected. "
                    "Showing heuristic scores + template messages below."
                )
            else:
                st.info(
                    "📝 **Showing template messages** (no AI key set). "
                    "Scroll down to see the unique per-lead messages below. "
                    "Add an OpenRouter key in 🤖 AI enrichment for AI-rewritten messages."
                )

        # Charts
        if filtered:
            df = pd.DataFrame([b.to_dict() for b in filtered])
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("##### ⭐ Rating distribution")
                if "reviews_average" in df.columns and df["reviews_average"].notna().any():
                    rating_counts = (
                        df["reviews_average"].dropna().round().astype(int).value_counts().sort_index()
                    )
                    st.bar_chart(rating_counts)
                else:
                    st.caption("No rating data")
            with c2:
                st.markdown("##### 💬 Top 20 by review count")
                if "reviews_count" in df.columns and df["reviews_count"].notna().any():
                    top = (
                        df.dropna(subset=["reviews_count"])
                        .sort_values("reviews_count", ascending=False)
                        .head(20)[["name", "reviews_count"]]
                        .set_index("name")
                    )
                    st.bar_chart(top)
                else:
                    st.caption("No review-count data")

        st.divider()
        st.markdown(f"##### 📋 Leads — {len(filtered)} of {len(all_businesses)}")
        if filtered:
            display_df = pd.DataFrame([b.to_dict() for b in filtered])
            st.dataframe(
                display_df,
                use_container_width=True,
                height=420,
                column_config={
                    "name": st.column_config.TextColumn("Name", width="medium"),
                    "category": st.column_config.TextColumn("Category", width="small"),
                    "address": st.column_config.TextColumn("Address", width="large"),
                    "phone_number": st.column_config.TextColumn("Phone", width="small"),
                    "website": st.column_config.LinkColumn("Website", width="medium"),
                    "reviews_average": st.column_config.NumberColumn(
                        "★", format="%.1f", width="small"
                    ),
                    "reviews_count": st.column_config.NumberColumn(
                        "Reviews", format="%d", width="small"
                    ),
                    "google_maps_url": st.column_config.LinkColumn(
                        "Google Maps", width="small"
                    ),
                    "ai_score": st.column_config.NumberColumn(
                        "🎯 AI", format="%d", width="small",
                        help="AI lead score (0-10)",
                    ),
                    "ai_tier": st.column_config.TextColumn("Tier", width="small"),
                    "ai_reason": st.column_config.TextColumn("AI Note", width="large"),
                    "ai_outreach": st.column_config.TextColumn("AI Outreach", width="large"),
                    "ai_category": st.column_config.TextColumn("AI Tag", width="small"),
                    "latitude": None,
                    "longitude": None,
                    "is_closed": None,
                },
            )

            # ---- Per-lead message preview -----------------------------------
            st.markdown(f"##### ✉️ Unique Outreach Messages — {len(filtered)} leads")
            st.caption(
                "Each lead gets a unique message based on their actual data. "
                "Click any row to expand the full multi-channel set."
            )
            for i, biz in enumerate(filtered[:50], start=1):  # cap at 50 for perf
                tier_emoji = {"hot": "🔥", "warm": "🟡", "cold": "🔵", "skip": "⚫"}.get(biz.ai_tier or "skip", "⚫")
                source_tag = biz.ai_messages_source or "template"
                source_label = "🤖 AI" if source_tag == "ai" else "📝 Template"
                with st.expander(
                    f"{tier_emoji} {biz.name or '(no name)'}  •  {biz.ai_angle_id or '—'}  •  {source_label}",
                    expanded=False,
                ):
                    mcol1, mcol2 = st.columns([3, 2])
                    with mcol1:
                        st.markdown("**📧 Subject line:**")
                        st.code(biz.ai_subject or "(none)", language=None)
                        if biz.ai_subject_b and biz.ai_subject_b != biz.ai_subject:
                            st.caption(f"Alt A/B: {biz.ai_subject_b}")
                        if biz.ai_subject_c and biz.ai_subject_c != biz.ai_subject:
                            st.caption(f"Alt C: {biz.ai_subject_c}")

                        st.markdown("**📧 Email body:**")
                        st.text_area(
                            "body",
                            value=biz.ai_body_email or "(empty)",
                            height=220,
                            key=f"body_{biz.name}_{i}",
                            label_visibility="collapsed",
                        )

                        st.markdown("**🔄 Follow-up sequence:**")
                        for day, field_name in [(3, "ai_followup_day3"), (7, "ai_followup_day7"), (14, "ai_followup_day14")]:
                            msg = getattr(biz, field_name, None)
                            if msg:
                                st.markdown(f"*Day {day}:*")
                                st.text_area(
                                    f"d{day}",
                                    value=msg,
                                    height=110,
                                    key=f"{field_name}_{biz.name}_{i}",
                                    label_visibility="collapsed",
                                )

                    with mcol2:
                        st.markdown("**💬 WhatsApp:**")
                        st.text_area(
                            "wa",
                            value=biz.ai_whatsapp or "(empty)",
                            height=80,
                            key=f"wa_{biz.name}_{i}",
                            label_visibility="collapsed",
                        )

                        st.markdown("**📱 SMS:**")
                        st.text_area(
                            "sms",
                            value=biz.ai_sms or "(empty)",
                            height=60,
                            key=f"sms_{biz.name}_{i}",
                            label_visibility="collapsed",
                        )

                        st.markdown("**☎️ Call script:**")
                        st.text_area(
                            "call",
                            value=biz.ai_call_script or "(empty)",
                            height=260,
                            key=f"call_{biz.name}_{i}",
                            label_visibility="collapsed",
                        )

                        st.markdown("**🏷️ Tag:**")
                        st.code(biz.ai_category or "(none)", language=None)

            # Downloads
            st.markdown("##### 📥 Download")

            # Build a smart filename that includes the query, backend, and lead count
            is_pack = bool(st.session_state.get("run_meta", {}).get("pack"))
            fname_query = st.session_state.get("lead_pack_select") or st.session_state.get("last_search_term") or ""
            fname_backend = st.session_state.get("last_backend") or ""
            fname_pack = st.session_state.get("run_meta", {}).get("pack", "") if is_pack else ""
            fname_count = len(filtered)

            d1, d2, d3 = st.columns(3)
            with d1:
                st.download_button(
                    "📊 Excel (.xlsx)",
                    data=export_excel(filtered),
                    file_name=make_filename(fname_query, fname_backend, fname_pack, "xlsx", fname_count),
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
            with d2:
                st.download_button(
                    "📄 CSV",
                    data=export_csv(filtered),
                    file_name=make_filename(fname_query, fname_backend, fname_pack, "csv", fname_count),
                    mime="text/csv",
                    use_container_width=True,
                )
            with d3:
                st.download_button(
                    "🔧 JSON",
                    data=export_json(filtered),
                    file_name=make_filename(fname_query, fname_backend, fname_pack, "json", fname_count),
                    mime="application/json",
                    use_container_width=True,
                )

            # New: phone-only and vCard for cold outreach
            st.markdown("##### 📞 For cold outreach")
            phones = [b for b in filtered if b.phone_number]
            if not phones:
                st.caption("No leads with phone numbers in the current filter. Loosen filters or pick a different backend.")
            else:
                p1, p2 = st.columns(2)
                with p1:
                    st.download_button(
                        f"📞 Phone-only CSV ({len(phones)} leads)",
                        data=export_phones_csv(phones),
                        file_name=make_filename(fname_query, fname_backend, fname_pack, "phones.csv", len(phones)),
                        mime="text/csv",
                        use_container_width=True,
                        help="Just Name + Phone + click-to-call link. Drop into your calling sheet.",
                    )
                with p2:
                    st.download_button(
                        f"👤 vCard (.vcf) — {len(phones)} contacts",
                        data=export_vcard(phones),
                        file_name=make_filename(fname_query, fname_backend, fname_pack, "vcf", len(phones)),
                        mime="text/vcard",
                        use_container_width=True,
                        help="Import directly into phone contacts / WhatsApp / Truecaller.",
                    )

            # For lead packs: Excel with one sheet per source query
            if is_pack:
                st.markdown("##### 📚 Lead-pack export (split by campaign)")
                st.download_button(
                    "📊 Excel — 1 sheet per query",
                    data=export_excel_by_source(filtered),
                    file_name=make_filename("", fname_backend, fname_pack, "xlsx", fname_count),
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                    help="One sheet per search query that produced leads in this pack, plus an 'All leads' summary sheet.",
                )
        else:
            st.info("No leads match your filters. Loosen the filters and try again.")

    elif st.session_state.results is not None and not st.session_state.results.business_list:
        # Backend-specific empty-result hints
        if backend_name == "justdial":
            st.warning(
                "Scrape ran successfully but returned **0 results**.\n\n"
                "**Common reasons for JustDial:**\n"
                "- That category doesn't exist in that city on JustDial\n"
                "- City name doesn't match JustDial's exact format "
                "(try *Bengaluru* instead of *Bangalore*, *Gurugram* instead of *Gurgaon*)\n"
                "- JustDial blocks datacenter IPs sometimes — the actor returns empty instead of erroring\n\n"
                "**Try:**\n"
                "- A different city: `dentists in Mumbai`\n"
                "- A different category: `restaurants` instead of *multi-cuisine restaurants*\n"
                "- Verify manually at [justdial.com](https://www.justdial.com) that listings exist",
                icon="🇮🇳",
            )
        elif backend_name == "indiamart":
            st.warning(
                "Scrape ran successfully but returned **0 results**.\n\n"
                "**Common reasons for IndiaMART:**\n"
                "- No suppliers match that exact product term\n"
                "- The product term is too niche — try a broader category\n"
                "- IndiaMART is region-scoped; try with a state name\n\n"
                "**Try:**\n"
                "- `LED lights` instead of *RGB LED strip lights 5m waterproof*\n"
                "- `cotton fabric` instead of *combed cotton gsm 200*\n"
                "- `stainless steel pipes in Maharashtra` (state, not city)",
                icon="🇮🇳",
            )
        elif backend_name == "osm":
            st.warning(
                "Scrape ran successfully but returned **0 results**.\n\n"
                "OSM (OpenStreetMap) is community-edited and has **thin coverage in India**. "
                "Try **Outscraper**, **JustDial**, or **IndiaMART** for India-specific data.",
                icon="🆓",
            )
        else:
            st.warning(
                "Scrape ran successfully but returned 0 results. "
                "Try a more specific search term or turn off headless mode."
            )


    # ---------------------------------------------------------------------------
    # Footer
    # ---------------------------------------------------------------------------

    st.markdown(
    """
    <div class="footer">
    MapLead · Built with Playwright + Streamlit<br>
    Use responsibly and respect <a href="https://www.google.com/intl/en/help/terms_maps/" target="_blank">Google's Terms of Service</a>
    </div>
    """,
    unsafe_allow_html=True,
    )


    # ---------------------------------------------------------------------------

    
# Database page — view, search, edit, bulk-update stored leads
# ---------------------------------------------------------------------------
elif page == PAGE_CRM:
    # =========================================================================
    # CRM — pipeline + tasks + activity
    # =========================================================================
    from database import STATUSES
    import crm as crm_mod

    st.markdown("## 📇 CRM — Leads, Pipeline, Activity")
    st.caption(
        "Every scrape auto-saves here. Move leads through the pipeline, "
        "log calls/emails, and track what's working."
    )

    db = get_db()
    sources = db.list_sources()

    if not sources:
        st.info(
            "📭 **No leads yet.** Scrape something from 🔍 Scrape, "
            "and they'll show up here."
        )
        st.stop()

    # ---- Top KPI strip -----------------------------------------------------
    pipeline = crm_mod.get_pipeline_summary(db)
    total = sum(p["count"] for p in pipeline)
    hot_count = sum(1 for biz in crm_mod.get_hot_leads(db, min_score=8))

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("📦 Total leads", f"{total:,}")
    k2.metric("🔥 Hot (score ≥8)", hot_count)
    k3.metric("📞 To contact (New)", pipeline[0]["count"])
    k4.metric("✅ Won", pipeline[4]["count"])

    st.divider()

    # ---- Pipeline kanban --------------------------------------------------
    st.markdown("### 🔀 Pipeline")
    cols = st.columns(len(STATUSES))
    status_emojis = {
        "New": "🆕", "Contacted": "📞", "Interested": "⭐",
        "Quoted": "💰", "Won": "🏆", "Lost": "❌"
    }
    for col, row in zip(cols, pipeline):
        with col:
            emoji = status_emojis.get(row["status"], "·")
            st.markdown(
                f"<div style='background:{row['color']}20; border-left:4px solid {row['color']}; "
                f"padding:0.6rem; border-radius:6px; text-align:center;'>"
                f"<div style='font-size:1.6rem; font-weight:700;'>{row['count']}</div>"
                f"<div style='font-size:0.8rem;'>{emoji} {row['status']}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

    st.divider()

    # ---- Today's tasks (New leads ready for outreach) ---------------------
    st.markdown("### 📞 Ready for Outreach")
    tasks = crm_mod.get_today_tasks(db)
    if not tasks:
        st.caption("Nothing to do — all hot leads are already contacted. 👏")
    else:
        st.caption(f"{len(tasks)} new leads with phone/website — click to log activity")
        for task in tasks[:15]:
            tc1, tc2, tc3, tc4, tc5 = st.columns([3, 2, 2, 1, 1])
            with tc1:
                st.markdown(f"**{task['name']}**")
                st.caption(f"{task['category'] or '—'} · {task['source']}")
            with tc2:
                st.code(task['phone'] or "(no phone)", language=None)
            with tc3:
                if task['phone']:
                    digits = ''.join(c for c in task['phone'] if c.isdigit() or c == '+')
                    wa_link = f"https://wa.me/{digits.replace('+','')}"
                    st.markdown(f"[💬 WhatsApp]({wa_link})  •  [📞 Call](tel:{digits})")
            with tc4:
                if st.button("✅ Log contact", key=f"logged_{task['id']}", use_container_width=True):
                    crm_mod.quick_log_contact(db, task['id'], task['source'], "call")
                    st.rerun()
            with tc5:
                if st.button("🚫 Skip", key=f"skip_{task['id']}", use_container_width=True):
                    db.set_status(task['id'], "Lost", task['source'], note="Skipped from outreach list")
                    st.rerun()

    st.divider()

    # ---- Hot leads (AI-scored, untouched) ----------------------------------
    st.markdown("### 🔥 Hot Leads (AI Score ≥ 8, Not Contacted)")
    hot_leads = crm_mod.get_hot_leads(db, min_score=8)
    if not hot_leads:
        st.caption("No hot leads yet — scrape more, or wait for leads to accumulate.")
    else:
        for lead in hot_leads[:10]:
            with st.expander(f"{getattr(lead, 'ai_score', None) or '?'}/10 — {lead.name or '(no name)'}  •  {lead.category or '—'}"):
                st.markdown(
                    f"**Phone:** `{lead.phone or '—'}`\n\n"
                    f"**Address:** {lead.address or '—'}\n\n"
                    f"**Website:** {lead.website or '—'}\n\n"
                    f"**Rating:** {lead.rating} ({lead.reviews_count} reviews)\n\n"
                    f"**Source:** {lead.source}"
                )
                if getattr(lead, "ai_body_email", None):
                    st.markdown("**📧 Outreach (auto-generated):**")
                    st.text_area(
                        "body",
                        value=lead.ai_body_email,
                        height=200,
                        key=f"hot_body_{lead.id}",
                        label_visibility="collapsed",
                    )
                if getattr(lead, "ai_whatsapp", None):
                    st.markdown("**💬 WhatsApp:**")
                    st.code(lead.ai_whatsapp, language=None)
                ca, cb, cc, cd = st.columns(4)
                with ca:
                    if st.button("📞 Log call", key=f"hc_{lead.id}_call", use_container_width=True):
                        crm_mod.quick_log_contact(db, lead.id, lead.source, "call")
                        st.toast("Call logged")
                        st.rerun()
                with cb:
                    if st.button("📧 Log email", key=f"hc_{lead.id}_email", use_container_width=True):
                        crm_mod.quick_log_contact(db, lead.id, lead.source, "email")
                        db.set_status(lead.id, "Contacted", lead.source)
                        st.toast("Email sent + marked Contacted")
                        st.rerun()
                with cc:
                    if st.button("📥 Move: Contacted", key=f"hc_{lead.id}_mc", use_container_width=True):
                        db.set_status(lead.id, "Contacted", lead.source)
                        st.toast("→ Contacted")
                        st.rerun()
                with cd:
                    if st.button("🏆 Won!", key=f"hc_{lead.id}_won", use_container_width=True):
                        db.set_status(lead.id, "Won", lead.source)
                        st.toast("🎉 Marked Won")
                        st.rerun()

    st.divider()

    # ---- Recent activity --------------------------------------------------
    st.markdown("### 📜 Recent Activity")
    activity = crm_mod.get_recent_activity(db, limit=10)
    if not activity:
        st.caption("No activity yet — log your first call or email above.")
    else:
        kind_emoji = {"call": "📞", "email": "📧", "whatsapp": "💬",
                       "meeting": "🤝", "note": "📝"}
        for act in activity:
            e = kind_emoji.get(act['kind'], "·")
            st.markdown(
                f"{e} **{act['lead_name']}** — {act['kind']}"
                f"  `{act['source']}`  _{act['at']}_"
                + (f"\n\n   > {act['summary']}" if act['summary'] else "")
            )
    st.caption(
        "Each scrape creates its own table — leads from different campaigns never mix. "
        "Pick a source below to view, search, and edit."
    )

    db = get_db()
    sources = db.list_sources()

    # ---- Source picker ----
    if not sources:
        st.info("No leads yet. Run a scrape from 🔍 Scrape first.")
    else:
        src_names = ["\u2014 ALL sources (combined) \u2014"] + [f"{s.name} ({s.lead_count} leads)" for s in sources]
        # Persist source selection across reruns
        if "db_selected_source" not in st.session_state:
            st.session_state.db_selected_source = src_names[1] if len(src_names) > 1 else src_names[0]
        # If the previously selected source was deleted, fall back to ALL
        if st.session_state.db_selected_source not in src_names:
            st.session_state.db_selected_source = src_names[0]
        sel = st.selectbox(
            "Source",
            options=src_names,
            key="db_selected_source",
            help="Each scrape = one table. Pick one to view, or ALL to search across.",
        )
        is_all = sel.startswith("\u2014")
        # Extract real source name (strip the '(N leads)' suffix)
        active_source = None if is_all else sel.rsplit(" (", 1)[0]

        # ---- Stats for current view ----
        if is_all:
            s = db.stats()
            total_leads = s["total"]
            with_phone = s["with_phone"]
        else:
            leads_in_src = db.query(source=active_source, limit=100000)
            total_leads = len(leads_in_src)
            with_phone = sum(1 for l in leads_in_src if l.phone_digits)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total leads", total_leads)
        c2.metric("With phone", with_phone, f"{with_phone * 100 // max(total_leads, 1)}%")
        c3.metric("Sources", len(sources))
        c4.metric("Active source", "ALL" if is_all else active_source[:25])
        st.divider()

        # ---- Source management (rename / drop) ----
        if not is_all:
            with st.expander("⚙️ Manage this source", expanded=False):
                mc1, mc2 = st.columns(2)
                with mc1:
                    new_name = st.text_input(
                        "Rename source",
                        value=active_source,
                        key=f"rename_{active_source}",
                    )
                    if st.button("Rename", key=f"btn_rename_{active_source}") and new_name.strip() and new_name != active_source:
                        if db.rename_source(active_source, new_name.strip()):
                            st.success(f"Renamed to '{new_name}'")
                            st.session_state.db_selected_source = f"{new_name} ({next((x.lead_count for x in db.list_sources() if x.name == new_name), 0)} leads)"
                            st.rerun()
                        else:
                            st.error("Rename failed (duplicate name?)")
                with mc2:
                    confirm = st.checkbox(f"Yes, delete all {total_leads} leads", key=f"confirm_drop_{active_source}")
                    if st.button("🗑 Drop this source", type="secondary", disabled=not confirm, key=f"btn_drop_{active_source}"):
                        n = db.drop_source(active_source)
                        st.warning(f"Deleted {n} leads and dropped table for '{active_source}'")
                        st.session_state.db_selected_source = "\u2014 ALL sources (combined) \u2014"
                        st.rerun()

        # ---- Filters ----
        st.markdown("### 🔍 Filter")
        f1, f2, f3, f4 = st.columns(4)
        with f1:
            f_status = st.multiselect("Status", STATUSES, default=["New"])
        with f2:
            f_search = st.text_input("Search name/phone/address", placeholder="")
        with f3:
            f_min_rating = st.number_input("Min ★ rating", 0.0, 5.0, 0.0, 0.1)
        with f4:
            f_order = st.selectbox(
                "Sort by",
                ["Last seen (newest)", "Last seen (oldest)", "Rating (high→low)",
                 "Rating (low→high)", "Name (A→Z)", "Times seen"],
                index=0,
                key="db_order",
            )
        order_map = {
            "Last seen (newest)": "last_seen DESC",
            "Last seen (oldest)": "last_seen ASC",
            "Rating (high→low)": "rating DESC",
            "Rating (low→high)": "rating ASC",
            "Name (A→Z)": "name ASC",
            "Times seen": "times_seen DESC",
        }
        f_has_phone = st.selectbox("Has phone?", ["Any", "Yes", "No"], index=0, key="db_has_phone")
        has_phone = {"Yes": True, "No": False, "Any": None}[f_has_phone]

        common = dict(
            status=f_status or None,
            search=f_search or None,
            has_phone=has_phone,
            min_rating=f_min_rating if f_min_rating > 0 else None,
            order_by=order_map[f_order],
            limit=2000,
        )
        if is_all:
            leads = db.query_all(**common)
        else:
            leads = db.query(source=active_source, **common)
        st.caption(f"Showing **{len(leads)}** leads" + ("" if is_all else f" from '{active_source}'"))

        if not leads:
            st.info("No leads match these filters.")
            leads = []
        else:
            st.markdown("### 📋 Results")
            import pandas as pd
            from features import whatsapp_url, format_phone_in
            rows = []
            for l in leads:
                phone_fmt = format_phone_in(l.phone)
                wa = whatsapp_url(l.phone)
                rating_str = ""
                if l.rating:
                    rating_str = f"{l.rating:.1f}★"
                    if l.reviews_count:
                        rating_str += f" ({l.reviews_count:,})"
                rows.append({
                    "id": l.id,
                    "Name": l.name or "",
                    "Category": l.category or "",
                    "Status": l.status,
                    "Phone": phone_fmt or "—",
                    "WA": wa or "",
                    "Rating": rating_str,
                    "Address": (l.address or "")[:60],
                    "Source": l.source or "",
                    "Last seen": l.last_seen,
                })
            df = pd.DataFrame(rows)
            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "id": None,
                    "WA": st.column_config.LinkColumn("WA", display_text="👉 chat", help="Click to open WhatsApp"),
                },
            )

            st.markdown("### ⚡ Bulk actions")
            b1, b2 = st.columns(2)
            with b1:
                bulk_status = st.selectbox("Set status to", ["(choose)"] + STATUSES, key="bulk_status_sel")
                if bulk_status != "(choose)":
                    if st.button(
                        f"Apply '{bulk_status}' to all {len(leads)} shown",
                        use_container_width=True,
                        key="btn_apply_bulk",
                    ):
                        if is_all:
                            n = 0
                            seen_keys = set()
                            for l in leads:
                                k = (l.source, l.id)
                                if k in seen_keys:
                                    continue
                                seen_keys.add(k)
                                n += db.bulk_set_status([l.id], l.source, bulk_status)
                            st.success(f"Updated {n} leads")
                        else:
                            n = db.bulk_set_status([l.id for l in leads], active_source, bulk_status)
                            st.success(f"Updated {n} leads")
                        st.rerun()
            with b2:
                if st.button(
                    f"🗑 Delete {len(leads)} shown leads",
                    type="secondary",
                    use_container_width=True,
                    key="btn_delete_shown",
                ):
                    if is_all:
                        n = 0
                        for l in leads:
                            n += db.delete([l.id], l.source)
                    else:
                        n = db.delete([l.id for l in leads], active_source)
                    st.warning(f"Deleted {n} leads")
                    st.rerun()

            st.markdown("### 🔍 Lead details")
            lead_id = st.number_input(
                "Lead ID (from table above)",
                min_value=0,
                max_value=10_000_000,
                value=0,
                step=1,
                key="db_lead_id_input",
            )
            # Determine which source to fetch from
            detail_source = active_source
            if lead_id and is_all:
                # Find which source the lead belongs to by looking at displayed rows
                match = next((l for l in leads if l.id == int(lead_id)), None)
                if match:
                    detail_source = match.source
            if lead_id:
                lead = db.get(int(lead_id), detail_source or "(none)")
                if lead:
                    with st.expander(f"{lead.name} — {lead.phone or 'no phone'}", expanded=True):
                        d1, d2 = st.columns(2)
                        with d1:
                            new_status = st.selectbox(
                                "Status",
                                STATUSES,
                                index=STATUSES.index(lead.status) if lead.status in STATUSES else 0,
                                key=f"st_{lead.source}_{lead.id}",
                            )
                            new_note = st.text_area("Notes", value=lead.notes or "", key=f"nt_{lead.source}_{lead.id}")
                            if st.button("Save", key=f"sv_{lead.source}_{lead.id}"):
                                db.set_status(lead.id, new_status, lead.source, new_note)
                                sec.audit("set_status", source=lead.source,
                                          details=f"lead_id={lead.id} -> {new_status}")
                                st.success("Saved")
                                st.rerun()
                        with d2:
                            st.write(f"**Source:** {lead.source}")
                            st.write(f"**Address:** {lead.address or '—'}")
                            st.write(f"**Phone:** {format_phone_in(lead.phone) or '—'}")
                            if lead.website:
                                st.write(f"**Website:** {lead.website}")
                            st.write(f"**Rating:** {lead.rating} ({lead.reviews_count or 0} reviews)")
                            st.write(f"**Backend:** {lead.backend or '—'}")
                            st.write(f"**First seen:** {lead.first_seen}")
                            st.write(f"**Last seen:** {lead.last_seen} (seen {lead.times_seen}x)")
                            if lead.google_maps_url:
                                st.markdown(f"[Open in Google Maps]({lead.google_maps_url})")

                        # ---- AI helpers (per-lead)
                        from scraper import Business as _Biz
                        ai_biz = _Biz(
                            name=lead.name, address=lead.address,
                            phone_number=lead.phone, category=lead.category,
                            website=lead.website,
                            reviews_average=lead.rating,
                            reviews_count=lead.reviews_count,
                            google_maps_url=lead.google_maps_url,
                        )
                        st.markdown("**🤖 AI helpers:**")
                        # Show persisted score if any
                        if lead.ai_score is not None:
                            st.success(f"💾 Persisted AI score: **{lead.ai_score}/10** — {lead.ai_score_reason}")
                        ac1, ac2, ac3, ac4 = st.columns(4)
                        city = lead.source.split(" in ")[-1] if " in " in lead.source else "your city"
                        with ac1:
                            if st.button("📊 Score", key=f"ai_score_{lead.source}_{lead.id}",
                                         use_container_width=True):
                                with st.spinner("Scoring…"):
                                    s = ai_mod.score_lead(ai_biz)
                                # Persist
                                db.set_ai_score(lead.id, lead.source, s["score"], s["reason"])
                                st.info(f"**{s['score']}/10** — {s['reason']}\n\n*({s['source']})*")
                                sec.audit("ai_score", source=lead.source,
                                          details=f"lead_id={lead.id} score={s['score']}")
                                st.rerun()
                        with ac2:
                            if st.button("💬 WhatsApp (3 variants)", key=f"ai_var_{lead.source}_{lead.id}",
                                         use_container_width=True):
                                with st.spinner("Drafting 3 variants…"):
                                    variants = ai_mod.generate_variants(ai_biz, "whatsapp", city)
                                for i, v in enumerate(variants, 1):
                                    st.markdown(f"**Variant {i}** ({v.get('angle','?')}):\n\n> {v.get('message','')}")
                        with ac3:
                            if st.button("📧 Email", key=f"ai_email_{lead.source}_{lead.id}",
                                         use_container_width=True):
                                with st.spinner("Drafting email…"):
                                    email = ai_mod.generate_email(ai_biz, city)
                                st.code(email)
                        with ac4:
                            if st.button("🎯 Qualify", key=f"ai_qual_{lead.source}_{lead.id}",
                                         use_container_width=True):
                                with st.spinner("Qualifying…"):
                                    qual = ai_mod.qualify_lead(ai_biz, city)
                                if "score" in qual:
                                    db.set_ai_score(lead.id, lead.source,
                                                    qual.get("score", 0),
                                                    qual.get("best_pitch", ""),
                                                    qualified=qual.get("qualified"))
                                st.json(qual)

                        # Cold call script + research as a row below
                        rc1, rc2 = st.columns(2)
                        with rc1:
                            if st.button("📞 Cold call script", key=f"ai_cc_{lead.source}_{lead.id}",
                                         use_container_width=True):
                                with st.spinner("Drafting…"):
                                    script = ai_mod.generate_cold_call_script(ai_biz, city)
                                st.code(script)
                        with rc2:
                            if st.button("🔍 Research", key=f"ai_re_{lead.source}_{lead.id}",
                                         use_container_width=True):
                                with st.spinner("Researching…"):
                                    research = ai_mod.research_lead(ai_biz)
                                if research:
                                    st.markdown(research)
                                else:
                                    st.caption("No API key configured — research unavailable.")

                        st.markdown("**Contact log:**")
                        contacts = db.contacts_for(lead.id, lead.source)
                        if contacts:
                            for c in contacts:
                                st.write(f"- **{c['occurred_at']}** ({c['kind']}): {c['summary']}")
                        else:
                            st.caption("No contacts logged yet.")
                        bc1, bc2, bc3, bc4 = st.columns(4)
                        kinds = ["call", "whatsapp", "email", "meeting"]
                        for col, kind in zip((bc1, bc2, bc3, bc4), kinds):
                            with col:
                                if st.button(f"+ {kind}", key=f"lg_{lead.source}_{lead.id}_{kind}"):
                                    db.add_contact(lead.id, lead.source, kind, f"Logged from {kind} button")
                                    st.rerun()
                else:
                    st.warning(f"No lead with id={lead_id} in source '{detail_source}'")

        st.divider()
        st.markdown("### 📤 Export")
        col1, col2 = st.columns(2)
        with col1:
            if is_all:
                csv = db.export_all_csv()
                fn = f"maplead_all_sources_{datetime.now().strftime('%Y-%m-%d')}.csv"
            else:
                csv = db.export_source_csv(active_source)
                fn = make_filename(active_source, "", "", "csv", len(leads))
            st.download_button(
                "📊 Export current view as CSV",
                data=csv,
                file_name=fn,
                mime="text/csv",
                use_container_width=True,
                key="btn_export_csv",
            )
        with col2:
            stats_bytes = json.dumps(db.stats(), indent=2, default=str).encode()
            st.download_button(
                "🔧 Export DB stats as JSON",
                data=stats_bytes,
                file_name=f"maplead_stats_{datetime.now().strftime('%Y-%m-%d')}.json",
                mime="application/json",
                use_container_width=True,
                key="btn_export_stats",
            )


# ---------------------------------------------------------------------------
# Database page (raw table view + bulk ops)
# ---------------------------------------------------------------------------
elif page == PAGE_DB:
    from database import STATUSES

    st.markdown("## 🗄️ Lead Database")
    st.caption(
        "Each scrape creates its own table. Leads from different campaigns "
        "never mix. **Use 📇 CRM for daily work — that page has the same data, "
        "plus pipeline, tasks, and quick-log buttons.**"
    )

    db = get_db()
    sources = db.list_sources()

    if not sources:
        st.info("📭 No leads yet. Run a scrape from 🔍 Scrape.")
    else:
        # Quick stats per source
        st.markdown("### 📋 Sources")
        import pandas as pd
        rows = []
        for s in sources:
            rows.append({
                "Source": s.name,
                "Leads": s.lead_count,
                "Backend": s.backend or "—",
                "Created": s.created_at or "—",
                "Last scrape": s.last_scraped_at or "—",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        # Per-source details
        st.markdown("### 📂 View Source Details")
        src_names = [s.name for s in sources]
        sel = st.selectbox("Pick a source", src_names, key="db_detail_sel")
        if sel:
            leads = db.query(source=sel, limit=500)
            st.caption(f"{len(leads)} leads in '{sel}'")
            if leads:
                import pandas as pd
                df = pd.DataFrame([{
                    "Name": l.name,
                    "Status": l.status,
                    "Phone": l.phone,
                    "Rating": l.rating,
                    "Reviews": l.reviews_count,
                    "Category": l.category,
                } for l in leads])
                st.dataframe(df, use_container_width=True, hide_index=True)

                # Bulk actions
                st.markdown("### ⚡ Bulk Actions")
                ba1, ba2, ba3 = st.columns(3)
                with ba1:
                    new_st = st.selectbox("Move all New →", STATUSES, key="bulk_st")
                    if st.button("Apply to all 'New' in this source", key="bulk_apply"):
                        ids = [l.id for l in leads if l.status == "New"]
                        if ids:
                            db.bulk_set_status(ids, sel, new_st)
                            st.toast(f"Updated {len(ids)} leads → {new_st}")
                            st.rerun()
                with ba2:
                    if st.button("Export this source (CSV)", key="bulk_export"):
                        from crm import export_leads_csv
                        st.download_button(
                            "💾 Download CSV",
                            data=export_leads_csv(leads),
                            file_name=f"maplead_{sel.replace(' ', '_')}.csv",
                            mime="text/csv",
                        )
                with ba3:
                    confirm = st.checkbox("Confirm: drop this source")
                    if st.button("🗑 Drop source", type="secondary", disabled=not confirm):
                        n = db.drop_source(sel)
                        st.toast(f"Dropped {sel} ({n} leads)")
                        st.rerun()

# ---------------------------------------------------------------------------
# Stats page
# ---------------------------------------------------------------------------
elif page == PAGE_STATS:
    from database import STATUSES

    st.markdown("## 📊 Lead Database Stats")
    db = get_db()
    s = db.stats()
    sources = db.list_sources()

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total leads", s["total"])
    m2.metric("With phone", s["with_phone"])
    m3.metric("Sources", len(sources))
    won = s["by_status"].get("Won", 0)
    conversion = (won * 100 // s["total"]) if s["total"] else 0
    m4.metric("Conversion rate", f"{conversion}%", f"{won} won")

    st.divider()
    st.markdown("### Pipeline status (all sources combined)")
    cols = st.columns(len(STATUSES))
    for col, s_name in zip(cols, STATUSES):
        n = s["by_status"].get(s_name, 0)
        col.metric(s_name, n)

    st.divider()
    st.markdown("### Sources")
    if sources:
        import pandas as pd
        df = pd.DataFrame([
            {
                "Source": x.name,
                "Leads": x.lead_count,
                "Backend": x.backend or "—",
                "Last used": x.last_used_at,
                "Created": x.created_at,
            }
            for x in sources
        ])
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.bar_chart(df.set_index("Source")["Leads"])
    else:
        st.caption("No sources yet. Run a scrape to create the first one.")

    st.divider()
    st.markdown("### Recent activity (across all sources)")
    recent = db.query_all(limit=20)
    if recent:
        import pandas as pd
        df = pd.DataFrame([
            {
                "Name": l.name or "",
                "Status": l.status,
                "Phone": l.phone or "",
                "Source": (l.source or "")[:40],
                "Last seen": l.last_seen,
            }
            for l in recent
        ])
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.caption("No leads in the database yet.")


# ---------------------------------------------------------------------------
# Settings page — security, backups, AI config
# ---------------------------------------------------------------------------
elif page == PAGE_SETTINGS:
    from security import DatabaseSecurity
    import ai as ai_mod

    st.markdown("## ⚙️ Settings")
    st.caption("Security, backups, audit log, and AI configuration.")

    sec = get_security()
    db = get_db()

    # ---- Read-only mode
    st.markdown("### 🔒 Database protection")
    persisted_ro = sec.is_read_only_persisted()
    session_ro = sec.read_only
    ro1, ro2 = st.columns([2, 3])
    with ro1:
        st.write("**Current state:**")
        if session_ro or persisted_ro:
            st.error("🔒 READ-ONLY — writes are blocked")
        else:
            st.success("✅ Writable")
    with ro2:
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Enable read-only (this session)",
                         disabled=session_ro, use_container_width=True):
                sec.set_read_only(True)
                st.rerun()
            if st.button("Disable read-only (this session)",
                         disabled=not session_ro, use_container_width=True):
                sec.set_read_only(False)
                st.rerun()
        with c2:
            if persisted_ro:
                if st.button("Clear persistent read-only", use_container_width=True):
                    sec.set_read_only(False, persist=True)
                    st.rerun()
            else:
                if st.button("Persist read-only across restarts", use_container_width=True):
                    sec.set_read_only(True, persist=True)
                    st.rerun()

    st.divider()

    # ---- Auto-backup
    st.markdown("### 💾 Backups")
    st.caption(
        f"Auto-snapshot is taken before any destructive operation. "
        f"Up to {DatabaseSecurity.__init__.__code__.co_consts and '20'} backups kept."
    )
    b1, b2 = st.columns([1, 3])
    with b1:
        if st.button("📸 Create backup now", use_container_width=True):
            dest = sec.backup(label="manual")
            st.success(f"Saved to {dest.name}")
    with b2:
        backups = sec.list_backups()
        if backups:
            import pandas as pd
            df = pd.DataFrame(backups)
            st.dataframe(df, use_container_width=True, hide_index=True)
            # Restore dropdown
            with st.expander("↩️ Restore from backup", expanded=False):
                options = {b["name"]: b["path"] for b in backups}
                pick = st.selectbox("Pick a backup", list(options.keys()))
                confirm = st.checkbox("Yes, replace current database")
                if st.button("Restore", disabled=not confirm, type="secondary"):
                    if sec.restore_backup(options[pick]):
                        st.success(f"Restored from {pick}. Reload the app.")
                    else:
                        st.error("Restore failed.")
        else:
            st.caption("No backups yet.")

    st.divider()

    # ---- Audit log
    st.markdown("### 📜 Audit log")
    st.caption("Every write to the DB is recorded here. Last 100 entries.")
    log = sec.get_audit_log(limit=100)
    if log:
        import pandas as pd
        df = pd.DataFrame(log)
        df = df[["occurred_at", "actor", "action", "source", "details"]]
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.caption("No audit entries yet.")

    st.divider()

    # ---- AI config (OpenRouter-first)
    st.markdown("### 🤖 AI features")
    st.caption(
        "Powered by OpenRouter — one key unlocks Claude, GPT-4o, Gemini, "
        "Llama, DeepSeek and 100+ other models. Also works with any OpenAI-"
        "compatible endpoint (DeepSeek, Groq, Together, Ollama, LM Studio)."
    )

    configured = ai_mod.is_configured()
    if configured:
        endpoint = ai_mod.get_base_url()
        if ai_mod.is_openrouter():
            st.success("✅ AI configured — OpenRouter active")
        else:
            st.success(f"✅ AI configured — custom endpoint: {endpoint}")
        st.caption(f"Model: `{ai_mod.get_model()}`")
    else:
        st.warning("⚠️ No API key configured — AI features will use built-in templates")

    # ---- Quick presets
    st.markdown("**Quick presets:**")
    pc1, pc2, pc3 = st.columns(3)
    with pc1:
        if st.button("🎯 OpenRouter (Claude 3.5 Sonnet)", use_container_width=True,
                     help="Best quality for cold outreach drafting"):
            import os as _os
            st.session_state["MAPLEAD_OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"
            st.session_state["MAPLEAD_OPENAI_MODEL"] = "anthropic/claude-3.5-sonnet"
            _os.environ["MAPLEAD_OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"
            _os.environ["MAPLEAD_OPENAI_MODEL"] = "anthropic/claude-3.5-sonnet"
            st.success("Preset applied")
            st.rerun()
    with pc2:
        if st.button("⚡ OpenRouter (GPT-4o mini)", use_container_width=True,
                     help="Best value for bulk scoring 1000s of leads"):
            import os as _os
            st.session_state["MAPLEAD_OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"
            st.session_state["MAPLEAD_OPENAI_MODEL"] = "openai/gpt-4o-mini"
            _os.environ["MAPLEAD_OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"
            _os.environ["MAPLEAD_OPENAI_MODEL"] = "openai/gpt-4o-mini"
            st.success("Preset applied")
            st.rerun()
    with pc3:
        if st.button("🆓 OpenRouter (Llama 3.2 free)", use_container_width=True,
                     help="Free tier via OpenRouter — slower but no cost"):
            import os as _os
            st.session_state["MAPLEAD_OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"
            st.session_state["MAPLEAD_OPENAI_MODEL"] = "meta-llama/llama-3.2-3b-instruct:free"
            _os.environ["MAPLEAD_OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"
            _os.environ["MAPLEAD_OPENAI_MODEL"] = "meta-llama/llama-3.2-3b-instruct:free"
            st.success("Preset applied")
            st.rerun()

    with st.form("ai_config"):
        st.markdown("**Manual configuration**")
        new_key = st.text_input("API key (paste your OpenRouter sk-or-v1-…)",
                                type="password",
                                help="Stored only for this session, never written to disk.")
        # Model picker with presets
        model_options = [m["id"] for m in ai_mod.POPULAR_MODELS] + ["(custom)"]
        current = ai_mod.get_model()
        if current not in model_options:
            model_options.insert(-1, current)
        chosen = st.selectbox("Model", model_options,
                              index=model_options.index(current) if current in model_options else 0,
                              help="Popular OpenRouter models. Pick '(custom)' to type your own.")
        if chosen == "(custom)":
            col_a, col_b = st.columns([3, 1])
            with col_a:
                new_model = st.text_input("Custom model ID", value=current,
                                          help="e.g. anthropic/claude-3.5-sonnet, openai/gpt-4o-mini")
            with col_b:
                st.markdown("🔗 [Browse 100+ OpenRouter models →](https://openrouter.ai/models)",
                            unsafe_allow_html=True)
        else:
            new_model = chosen
            # Show pricing hint
            cur = next((m for m in ai_mod.POPULAR_MODELS if m["id"] == chosen), None)
            if cur:
                st.caption(
                    f"💰 {cur['label']} — ${cur['input']}/M input, ${cur['output']}/M output. "
                    f"[See all models](https://openrouter.ai/models)"
                )
        new_url = st.text_input("Base URL", value=ai_mod.get_base_url(),
                                help="Default: OpenRouter if key starts with sk-or-")
        submitted = st.form_submit_button("Save for this session")
        if submitted:
            import os as _os
            # Save to BOTH st.session_state (survives reruns in session)
            # AND os.environ (survives subprocess restarts).
            if new_key:
                st.session_state["MAPLEAD_OPENAI_API_KEY"] = new_key
                _os.environ["MAPLEAD_OPENAI_API_KEY"] = new_key
            if new_url:
                st.session_state["MAPLEAD_OPENAI_BASE_URL"] = new_url
                _os.environ["MAPLEAD_OPENAI_BASE_URL"] = new_url
            if new_model:
                st.session_state["MAPLEAD_OPENAI_MODEL"] = new_model
                _os.environ["MAPLEAD_OPENAI_MODEL"] = new_model
            st.success("Saved for this session. Re-open Campaign Strategist.")
            st.rerun()

    # ---- Bulk AI scoring
    st.divider()
    st.markdown("### 🧮 Bulk AI scoring")
    st.caption("Score every lead in a source at once. Results are saved to the DB.")
    sources_for_scoring = db.list_sources() if db else []
    if sources_for_scoring:
        import pandas as pd
        src_pick = st.selectbox(
            "Pick a source to bulk-score",
            options=[s.name for s in sources_for_scoring],
            key="bulk_score_src",
        )
        n_leads = next((s.lead_count for s in sources_for_scoring if s.name == src_pick), 0)
        cost = ai_mod.estimate_cost(n_leads * 250, n_leads * 80)
        st.caption(
            f"~{n_leads} leads · est. cost: "
            + (f"${cost['total_usd']:.4f}" if cost else "unknown model")
        )
        if st.button(f"🚀 Score all {n_leads} leads in '{src_pick}'", type="primary"):
            progress = st.progress(0.0, text="Scoring leads…")
            leads = db.query(source=src_pick, limit=10_000)
            updates = []
            for i, lead in enumerate(leads):
                from scraper import Business as _Biz
                biz = _Biz(name=lead.name, address=lead.address, phone_number=lead.phone,
                           category=lead.category, website=lead.website,
                           reviews_average=lead.rating, reviews_count=lead.reviews_count,
                           google_maps_url=lead.google_maps_url)
                s = ai_mod.score_lead(biz)
                updates.append({"id": lead.id, "score": s["score"], "reason": s["reason"]})
                if i % 10 == 0:
                    progress.progress((i + 1) / max(len(leads), 1),
                                      text=f"Scored {i + 1}/{len(leads)} ({s['source']})")
            n = db.bulk_set_ai_scores(updates, src_pick)
            sec.audit("bulk_score", source=src_pick, details=f"{n} leads scored")
            progress.progress(1.0, text="Done!")
            st.success(f"✅ Scored {n} leads in '{src_pick}'")
            st.rerun()
    else:
        st.caption("No sources yet. Scrape something first.")

    # ---- Campaign strategist
    st.divider()
    st.markdown("### 🎯 Campaign strategist")

    # Status indicator (top of section so the user always sees it)
    if ai_mod.is_configured():
        masked = ai_mod.get_api_key()[:8] + "..." + ai_mod.get_api_key()[-4:]
        st.success(
            f"✅ AI ready — key `{masked}` on `{ai_mod.get_model()}`"
        )
    else:
        st.warning("⚠️ AI not configured — using built-in curated suggestions.")
        with st.expander("🔑 Set your OpenRouter key here (no need to scroll up)", expanded=True):
            st.markdown(
                "[Get a free OpenRouter key →](https://openrouter.ai/keys)  "
                "(free tier available, no card required)"
            )
            with st.form("inline_key_form"):
                inline_key = st.text_input(
                    "OpenRouter key",
                    type="password",
                    placeholder="sk-or-v1-...",
                    help="Starts with sk-or-v1-. Will only be stored for this session.",
                )
                inline_model = st.selectbox(
                    "Model",
                    options=[m["id"] for m in ai_mod.POPULAR_MODELS],
                    index=1,  # GPT-4o mini is sensible default
                    format_func=lambda x: next(
                        (f"{m['label']} (${m['input']}/${m['output']} per 1M)"
                         for m in ai_mod.POPULAR_MODELS if m["id"] == x),
                        x,
                    ),
                )
                col_save, col_test = st.columns(2)
                with col_save:
                    inline_submitted = st.form_submit_button(
                        "💾 Save key & enable AI", type="primary", use_container_width=True,
                    )
                with col_test:
                    inline_test = st.form_submit_button(
                        "🔌 Test connection", use_container_width=True,
                    )

                # Test connection without saving (so user can verify a key first)
                if inline_test and inline_key:
                    from provider_detect import detect_provider, mask_key
                    prov = detect_provider(inline_key)
                    if prov is None:
                        st.warning(
                            f"⚠️ Key `{mask_key(inline_key)}` has an unrecognized prefix. "
                            f"Most providers use sk-or-*, sk-ant-*, gsk_*, etc. "
                            f"Will still try OpenRouter endpoint."
                        )
                    else:
                        st.info(
                            f"🔎 Detected provider: **{prov.name}** — "
                            f"endpoint `{prov.base_url}`, default model `{prov.default_model}`"
                        )
                    # Try a real ping
                    try:
                        import httpx
                        test_url = (prov.base_url if prov else "https://openrouter.ai/api/v1") + "/chat/completions"
                        r = httpx.post(
                            test_url,
                            headers={"Authorization": f"Bearer {inline_key}",
                                     "Content-Type": "application/json",
                                     "HTTP-Referer": "https://github.com/sabsar42/maplead",
                                     "X-Title": "MapLead AI test"},
                            json={"model": (inline_model if 'inline_model' in dir() else "qwen/qwen3.7-flash"),
                                  "messages": [{"role": "user", "content": "ping"}],
                                  "max_tokens": 5},
                            timeout=15,
                        )
                        if r.status_code == 200:
                            st.success(f"✅ Key works! Model responded.")
                        elif r.status_code == 401:
                            st.error(f"❌ 401 Unauthorized — key is invalid for this provider")
                        elif r.status_code == 402:
                            st.error(f"❌ 402 Payment required — account out of credits")
                        else:
                            st.error(f"❌ HTTP {r.status_code}: {r.text[:200]}")
                    except Exception as exc:
                        st.error(f"❌ Connection failed: {exc}")
                if inline_submitted and inline_key:
                    import os as _os
                    st.session_state["MAPLEAD_OPENAI_API_KEY"] = inline_key
                    st.session_state["MAPLEAD_OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"
                    st.session_state["MAPLEAD_OPENAI_MODEL"] = inline_model
                    _os.environ["MAPLEAD_OPENAI_API_KEY"] = inline_key
                    _os.environ["MAPLEAD_OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"
                    _os.environ["MAPLEAD_OPENAI_MODEL"] = inline_model
                    st.success("✅ Key saved — click ✨ Suggest best queries again")
                    st.rerun()

    cs1, cs2 = st.columns(2)
    with cs1:
        cs_city = st.text_input("City", value="Hyderabad", key="strat_city")
    with cs2:
        cs_industry = st.text_input("Industry / category", value="signage",
                                    key="strat_industry",
                                    help="e.g. 'restaurants', 'signage', 'jewellery shops'")
    col_btn1, col_btn2 = st.columns([3, 1])
    with col_btn1:
        run_strat = st.button("✨ Suggest best queries", use_container_width=True,
                              type="primary")
    with col_btn2:
        if not ai_mod.is_configured():
            st.link_button("🔑 Get key",
                           "https://openrouter.ai/keys",
                           use_container_width=True,
                           help="Free OpenRouter account, no card needed")
    if run_strat:
        with st.spinner("Thinking…"):
            result = ai_mod.suggest_queries(cs_city, cs_industry)
        # Show AI vs fallback badge
        ai_status = "🤖 AI" if ai_mod.is_configured() else "📋 Built-in"
        st.caption(f"{ai_status} suggestions for **{cs_industry}** in **{cs_city}**:")
        # Suppress the verbose "No AI configured" warning - just show a tiny hint
        if not ai_mod.is_configured():
            st.caption(
                "💡 Tip: Add an OpenRouter key in ⚙ Settings for AI-tailored suggestions. "
                "These built-in patterns work fine without it."
            )
        for q in result.get("queries", []):
            with st.expander(f"🔎 {q['query']}", expanded=False):
                st.write(f"**Why:** {q.get('why', '—')}")
                st.write(f"**Expected volume:** {q.get('expected_volume', '—')}")
        # Direct "use this query" buttons — copy to clipboard
        st.markdown("**Copy any query to use in 🔍 Scrape:**")
        for q in result.get("queries", []):
            st.code(q['query'], language="text")

    # ---- Test panel
    st.divider()
    with st.expander("🧪 Test AI features", expanded=False):
        from scraper import Business
        sample = Business(
            name="Tan Coffee", phone_number="081210 81814",
            category="Coffee shop", reviews_average=4.6, reviews_count=2089,
            address="Hitech City, Hyderabad", website="tancoffee.in",
        )
        city = "Hyderabad"
        if st.button("Run AI on sample lead", use_container_width=True):
            with st.spinner("Calling AI..."):
                score = ai_mod.score_lead(sample)
                wa = ai_mod.generate_whatsapp_message(sample, city)
                script = ai_mod.generate_cold_call_script(sample, city)
                email = ai_mod.generate_email(sample, city)
                variants = ai_mod.generate_variants(sample, "whatsapp", city)
                qual = ai_mod.qualify_lead(sample, city)
                research = ai_mod.research_lead(sample) if configured else ""
            st.markdown(f"**Score** (`{score['source']}`): {score['score']}/10 — {score['reason']}")
            st.markdown(f"**WhatsApp:**\n\n> {wa}")
            st.markdown(f"**Cold call:**\n\n> {script}")
            st.markdown(f"**Email:**\n\n```\n{email}\n```")
            st.markdown(f"**3 variants:**")
            for i, v in enumerate(variants, 1):
                st.markdown(f"  {i}. *({v.get('angle', '')})* — {v.get('message', '')}")
            if qual:
                st.markdown(f"**Qualification:**\n\n```json\n{qual}\n```")
            if research:
                st.markdown(f"**Research:**\n\n{research}")
