"""
MapLead — Streamlit UI
======================

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import asyncio
import os
import time

import pandas as pd
import streamlit as st

from scraper import Business, BusinessList
from utils import compute_stats, export_csv, export_excel, export_json

try:
    from api_backends import get_backend, ScraperBackend
except ImportError:
    get_backend = None  # type: ignore[assignment]
    ScraperBackend = None  # type: ignore[assignment, misc]

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
        st.session_state.run_lead_pack = True
        st.session_state.lead_pack_name = pack_name
        st.session_state.lead_pack_queries = pack
        st.session_state.lead_pack_per_query = per_query
        st.session_state.lead_pack_backend = free_backend

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

                backend = get_backend(backend_name)
                result = await backend.scrape(
                    search_term=search_term.strip(),
                    total=int(total),
                    headless=headless,
                    locale=locale,
                    progress_callback=update,
                )
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
            st.session_state.run_meta = {
                "elapsed": elapsed,
                "count": len(biz_list.business_list) if biz_list else 0,
            }
            st.success(
                f"✅ Found {len(biz_list.business_list) if biz_list else 0} businesses in {elapsed:.1f}s"
            )
            st.rerun()


# ---------------------------------------------------------------------------
# Lead Pack runner — runs multiple queries sequentially and merges results
# ---------------------------------------------------------------------------

if st.session_state.get("run_lead_pack"):
    pack_name: str = st.session_state.get("lead_pack_name", "Campaign")
    pack: list[dict] = st.session_state.get("lead_pack_queries", [])
    per_query: int = st.session_state.get("lead_pack_per_query", 30)
    pack_backend: str = st.session_state.get("lead_pack_backend", "botasaurus")

    # Reset the flag so the button click doesn't re-trigger on rerun
    st.session_state.run_lead_pack = False

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
                "latitude": None,
                "longitude": None,
            },
        )

        # Downloads
        st.markdown("##### 📥 Download")
        d1, d2, d3 = st.columns(3)
        with d1:
            st.download_button(
                "📊 Excel (.xlsx)",
                data=export_excel(filtered),
                file_name=f"maplead_{int(time.time())}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        with d2:
            st.download_button(
                "📄 CSV",
                data=export_csv(filtered),
                file_name=f"maplead_{int(time.time())}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with d3:
            st.download_button(
                "🔧 JSON",
                data=export_json(filtered),
                file_name=f"maplead_{int(time.time())}.json",
                mime="application/json",
                use_container_width=True,
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
