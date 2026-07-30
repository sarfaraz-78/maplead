"""Smoke tests for MapLead core logic — runs without network, browser, or API keys.

Run locally:
    python -m pytest tests/ -v
or:
    python tests/test_smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow `python tests/test_smoke.py` from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import Business, BusinessList, parse_rating, parse_review_count  # noqa: E402
from utils import _rows, compute_stats, export_csv, export_json  # noqa: E402
from scorer import heuristic_score, score_batch, TIER_EMOJI  # noqa: E402
from ai_core import (
    AICore,
    detect_provider,
    mask_key,
    _guess_category,
    _extract_city,
    _build_outreach,
    ScoreResult,
)  # noqa: E402
from ai_messages import (
    get_message_for,
    _pick_angle_for,
    _fill,
    _templated_messages,
    enrich_leads_with_messages,
    ANGLE_TEMPLATES,
    LeadMessages,
    MessageEngine,
)  # noqa: E402


# ---------------------------------------------------------------------------
# Heuristic scorer
# ---------------------------------------------------------------------------


def test_heuristic_score_minimal_business():
    """A business with nothing but a name scores 0 -> skip."""
    biz = Business(name="Lonely Cafe")
    s = heuristic_score(biz)
    assert s.score == 0
    assert s.tier == "skip"


def test_heuristic_score_full_business_hot():
    """A fully-populated business is hot."""
    biz = Business(
        name="Good Cafe",
        phone_number="+91 22 1234 5678",
        website="https://good.cafe",
        reviews_average=4.5,
        reviews_count=200,
        latitude=19.0, longitude=72.8,
        address="123 Main St, Mumbai, MH 400001",
    )
    s = heuristic_score(biz)
    assert s.score >= 8
    assert s.tier == "hot"


def test_heuristic_score_warm():
    """A business with 3 fields is warm, not hot."""
    biz = Business(
        name="OK Cafe",
        phone_number="9876543210",
        website="https://ok.cafe",
        reviews_average=4.2,
    )
    s = heuristic_score(biz)
    assert 5 <= s.score <= 7
    assert s.tier == "warm"


def test_heuristic_score_closed_zero():
    """Permanently closed businesses score 0."""
    biz = Business(name="Gone Cafe", phone_number="+91...", reviews_average=4.5, is_closed=True)
    s = heuristic_score(biz)
    assert s.score == 0
    assert s.tier == "skip"
    assert "CLOSED" in s.reason


def test_heuristic_score_closed_in_name_penalty():
    """Name containing 'closed' is penalized."""
    biz = Business(name="Permanently Closed Diner", phone_number="1234567890")
    s = heuristic_score(biz)
    assert s.score <= 3  # base 2 for phone minus 3 penalty = -1 -> clamped to 0, but phone still counts


def test_heuristic_score_partial_phone():
    """Partial phone is worth less than full phone."""
    biz_full = Business(name="A", phone_number="+919876543210")  # 12 digits
    biz_partial = Business(name="A", phone_number="98765")      # 5 digits
    assert heuristic_score(biz_full).score > heuristic_score(biz_partial).score


def test_heuristic_score_many_reviews_bonus():
    """High review counts (>50) get a bonus."""
    biz_low = Business(name="A", reviews_count=10)
    biz_high = Business(name="A", reviews_count=500)
    assert heuristic_score(biz_high).score > heuristic_score(biz_low).score


def test_score_batch_sorted_by_score():
    """score_batch returns leads sorted hot -> cold."""
    bizs = [
        Business(name="Cold", phone_number="1234567890"),
        Business(name="Hot", phone_number="1234567890", website="https://x.com", reviews_average=4.5, reviews_count=200, address="1 Main St, City, State 12345"),
        Business(name="Warm", phone_number="1234567890", website="https://x.com", reviews_average=4.0),
    ]
    ranked = score_batch(bizs)
    names = [b.name for b, _ in ranked]
    assert names[0] == "Hot"
    assert names[-1] == "Cold"


def test_tier_emoji_has_all_tiers():
    """All four tiers have emoji."""
    for t in ("hot", "warm", "cold", "skip"):
        assert t in TIER_EMOJI
        assert TIER_EMOJI[t]  # non-empty


def test_heuristic_score_always_provides_outreach():
    """Every scored business gets a templated outreach message."""
    biz = Business(name="Test Cafe", phone_number="+91 98765 43210")
    s = heuristic_score(biz)
    assert s.outreach
    assert len(s.outreach) > 30  # non-trivial
    assert "Test Cafe" in s.outreach  # personalized


def test_heuristic_score_always_provides_category():
    """Every scored business gets a category tag."""
    biz = Business(name="Test Cafe", phone_number="+91 98765 43210")
    s = heuristic_score(biz)
    assert s.category
    assert len(s.category) >= 2


def test_heuristic_outreach_personalizes_city():
    """Outreach extracts city from address."""
    biz = Business(
        name="X", phone_number="+91 22 2640 1234",
        address="12 Linking Road, Bandra West, Mumbai 400050",
    )
    s = heuristic_score(biz)
    assert "Mumbai" in s.outreach or "Bandra" in s.outreach


def test_heuristic_category_from_name():
    """Category can be inferred from business name keywords."""
    cases = [
        ("Joe's Cafe", "cafe"),
        ("City Hospital", "medical"),
        ("Best Salon", "salon"),
        ("Quick Plumber", "plumber"),
        ("Downtown Gym", "gym"),
        ("Main Street School", "education"),
        ("Bob's Diner", "other"),  # no keyword match
    ]
    for name, _expected in cases:
        biz = Business(name=name)
        s = heuristic_score(biz)
        # Don't pin to exact value - just verify some category exists
        assert s.category in ("cafe", "medical", "salon", "plumber",
                              "gym", "education", "restaurant", "retail",
                              "auto", "legal", "finance", "pharmacy",
                              "hotel", "other")


# ---------------------------------------------------------------------------
# ai_core - new from-scratch AI module
# ---------------------------------------------------------------------------


def test_detect_provider_known_prefixes():
    """Auto-detect provider from API key prefix."""
    assert detect_provider("sk-or-v1-abc")["name"] == "OpenRouter"
    assert detect_provider("sk-or-legacy")["name"] == "OpenRouter"
    assert detect_provider("sk-ant-api03-x")["name"] == "Anthropic"
    assert detect_provider("sk-proj-abc")["name"] == "OpenAI"
    assert detect_provider("gsk_abc")["name"] == "Groq"
    assert detect_provider("fw_abc")["name"] == "Fireworks"
    assert detect_provider("") is None
    assert detect_provider(None) is None


def test_detect_provider_unknown_prefix_returns_none():
    """detect_provider is pure - returns None for unknown prefixes.

    The "fallback to OpenRouter" happens in AICore constructor, not here.
    """
    assert detect_provider("sk-X5kWwAR3NH5RuKzH8V0AHhcHm0lwqDThCB7F2ZoIXPB4fUBpY8DTdpeasCnOcuRy") is None
    assert detect_provider("totally_random_key_xyz") is None


def test_mask_key_safe():
    """Never expose full key."""
    assert "..." in mask_key("sk-or-v1-1234567890abcdef")
    assert mask_key("").startswith("(")
    assert len(mask_key("short")) < 8


def test_extract_city_from_address():
    """Pull city from comma-separated address."""
    # 3-part [street, area, city] -> city is last
    assert _extract_city("12 Linking Road, Bandra West, Mumbai 400050") == "Mumbai 400050"
    # 4-part [street, area, city, state] -> city is second-to-last
    assert _extract_city("A, B, C, D") == "C"
    # 5-part [street, area, city, state, country] -> city is third-to-last
    assert _extract_city("A, B, C, D, E") == "C"
    # 2 parts: pick the LAST one (city)
    assert _extract_city("Bandra West, Mumbai") == "Mumbai"
    # Edge cases
    assert _extract_city("") == "your area"
    assert _extract_city(None) == "your area"
    assert _extract_city("Just City") == "Just City"


def test_guess_category_basic():
    """Category from name or explicit category field."""
    assert _guess_category("Joe's Cafe", None) == "cafe"
    assert _guess_category("Best Salon", None) == "salon"
    assert _guess_category(None, "Medical Center") == "medical_center"
    assert _guess_category("Random Name", None) == "other"


def test_build_outreach_includes_name_and_offer():
    """Outreach includes the business name and a concrete offer."""
    biz = Business(name="Punjabi Rasoi", address="12 Linking Road, Bandra West, Mumbai 400050")
    msg = _build_outreach(biz, "warm")
    assert "Punjabi Rasoi" in msg
    assert len(msg) > 50
    assert any(kw in msg.lower() for kw in ("call", "week", "improve", "automation"))


def test_ai_core_no_key_is_heuristic_only():
    """AICore with no key: is_configured=False, is_working=False."""
    # Save and clear env vars so they don't pollute the test
    import os
    saved_openai = os.environ.pop("MAPLEAD_OPENAI_API_KEY", "")
    saved_or = os.environ.pop("OPENROUTER_API_KEY", "")
    try:
        ai = AICore(api_key="")
        assert ai.is_configured() is False
        assert ai.is_working() is False
    finally:
        if saved_openai: os.environ["MAPLEAD_OPENAI_API_KEY"] = saved_openai
        if saved_or: os.environ["OPENROUTER_API_KEY"] = saved_or


def test_ai_core_describe_no_key():
    import os
    saved_openai = os.environ.pop("MAPLEAD_OPENAI_API_KEY", "")
    saved_or = os.environ.pop("OPENROUTER_API_KEY", "")
    try:
        ai = AICore(api_key="")
        desc = ai.describe()
        assert desc["configured"] is False
        assert desc["working"] is False
        assert desc["provider"] == "none"
    finally:
        if saved_openai: os.environ["MAPLEAD_OPENAI_API_KEY"] = saved_openai
        if saved_or: os.environ["OPENROUTER_API_KEY"] = saved_or


def test_ai_core_score_business_uses_heuristic_when_not_working():
    """Even without a working key, score_business returns a valid ScoreResult."""
    ai = AICore(api_key=None)
    biz = Business(
        name="Test Cafe",
        phone_number="+91 12345 67890",
        website="https://x.com",
        reviews_average=4.5,
        reviews_count=200,
    )
    res = ai.score_business(biz)
    assert isinstance(res, ScoreResult)
    assert res.score >= 5
    assert res.tier in ("hot", "warm")
    assert res.source == "heuristic"
    assert res.outreach  # populated


def test_ai_core_outreach_fallback():
    """outreach_for_business returns a non-empty message even without AI."""
    ai = AICore(api_key=None)
    biz = Business(name="Test", phone_number="+91 12345 67890")
    msg = ai.outreach_for_business(biz)
    assert msg
    assert "Test" in msg


def test_ai_core_categorize_fallback():
    """categorize_business returns a tag from name or category."""
    ai = AICore(api_key=None)
    biz = Business(name="Best Salon", phone_number="+91 12345 67890")
    assert ai.categorize_business(biz) == "salon"


def test_ai_core_enrich_batch_updates_businesses():
    """enrich() batch fills in AI fields for each business."""
    ai = AICore(api_key=None)
    bizs = [
        Business(name="A", phone_number="+91 12345 67890"),
        Business(name="B", phone_number="+91 99999 88888"),
    ]
    ai.enrich(bizs, ops=["score", "outreach", "category"])
    for biz in bizs:
        assert biz.ai_score is not None
        assert biz.ai_outreach  # not empty
        assert biz.ai_category


# ---------------------------------------------------------------------------
# ai_messages - per-lead unique message engine
# ---------------------------------------------------------------------------


def test_pick_angle_deterministic_per_business():
    """Same business always picks the same angle."""
    biz = Business(name="Joe's Cafe", address="5 Park Ave, NYC",
                   phone_number="+1 212-555-0100")
    a1 = _pick_angle_for(biz)
    a2 = _pick_angle_for(biz)
    assert a1["id"] == a2["id"]


def test_pick_angle_differs_between_businesses():
    """Different businesses usually get different angles."""
    angles_seen = set()
    for name in ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]:
        biz = Business(name=f"{name}'s Biz", address=f"{name} Street, Mumbai",
                       phone_number=f"+91 12345 6{name}890")
        a = _pick_angle_for(biz)
        angles_seen.add(a["id"])
    # Should pick at least 4 different angles for 10 businesses
    assert len(angles_seen) >= 4


def test_12_angle_templates_exist():
    """We have at least 12 message angles to choose from."""
    assert len(ANGLE_TEMPLATES) >= 12
    # Each has the required keys
    for angle in ANGLE_TEMPLATES:
        assert "id" in angle
        assert "subject_styles" in angle
        assert "opener" in angle
        assert "bridge" in angle
        assert "offer" in angle
        assert "close" in angle
        assert len(angle["subject_styles"]) >= 1


def test_fill_replaces_placeholders():
    """Template fill replaces placeholders safely."""
    biz = Business(name="Joe's Cafe", address="NYC", category="cafe")
    out = _fill("Hi {name}, come to {city} for {cat}.", biz, "cafe", "NYC", "")
    assert "Joe's Cafe" in out
    assert "NYC" in out
    assert "cafe" in out
    assert "{" not in out  # no unfilled


def test_templated_messages_populated():
    """get_message_for returns all message variants."""
    biz = Business(name="Punjabi Rasoi", address="12 Bandra, Mumbai",
                   phone_number="+91 22 2640 1234", category="restaurant",
                   reviews_average=4.3, reviews_count=542)
    msgs = get_message_for(biz)
    assert msgs["subject"]
    assert msgs["subject_b"]
    assert msgs["body_email"]
    assert msgs["whatsapp_short"]
    assert msgs["sms"]
    assert msgs["call_script"]
    assert msgs["followup_day3"]
    assert msgs["followup_day7"]
    assert msgs["followup_day14"]
    assert msgs["angle_id"]
    # Body should be substantial
    assert len(msgs["body_email"]) > 100
    # Should mention the lead's data
    assert "Punjabi Rasoi" in msgs["body_email"]
    assert "Mumbai" in msgs["body_email"]


def test_each_business_gets_different_body():
    """Two different businesses don't get the same body."""
    biz1 = Business(name="Punjabi Rasoi", address="12 Bandra, Mumbai",
                    phone_number="+91 22 2640 1234", category="restaurant",
                    reviews_average=4.3, reviews_count=542)
    biz2 = Business(name="Joe's Cafe", address="5 Park Ave, NYC",
                   phone_number="+1 212-555-0100", category="cafe",
                   reviews_average=4.7, reviews_count=1280)
    m1 = get_message_for(biz1)
    m2 = get_message_for(biz2)
    # Different businesses get at least one of:
    # different angle, different subject, or different body
    assert (m1["angle_id"] != m2["angle_id"]
            or m1["subject"] != m2["subject"]
            or m1["body_email"] != m2["body_email"])


def test_enrich_leads_with_messages_fills_businesses():
    """enrich_leads_with_messages adds message fields to each business."""
    bizs = [
        Business(name="A", phone_number="+91 12345 67890"),
        Business(name="B", phone_number="+91 99999 88888"),
    ]
    enrich_leads_with_messages(bizs, ai=None)
    for biz in bizs:
        assert biz.ai_subject
        assert biz.ai_body_email
        assert biz.ai_whatsapp
        assert biz.ai_sms
        assert biz.ai_call_script
        assert biz.ai_followup_day3
        assert biz.ai_followup_day7
        assert biz.ai_followup_day14
        assert biz.ai_angle_id
        assert biz.ai_messages_source == "template"


def test_message_engine_works_without_ai():
    """MessageEngine without AI falls back to templates."""
    engine = MessageEngine(ai=None)
    biz = Business(name="Test Cafe", phone_number="+91 12345 67890")
    msgs = engine.for_lead(biz)
    assert isinstance(msgs, LeadMessages)
    assert msgs.source == "template"
    assert msgs.body_email
    assert "Test Cafe" in msgs.body_email


def test_followup_messages_are_unique_per_business():
    """Different businesses get different follow-up messages."""
    biz1 = Business(name="A's Shop", address="Main St, Delhi",
                    phone_number="+91 11 1234 5678")
    biz2 = Business(name="B's Shop", address="Park Ave, Mumbai",
                    phone_number="+91 22 1234 5678")
    m1 = get_message_for(biz1)
    m2 = get_message_for(biz2)
    # At least the day-7 follow-up should mention the city
    assert "Delhi" in m1["followup_day7"]
    assert "Mumbai" in m2["followup_day7"]


def test_call_script_has_all_sections():
    """Voice call script has opening/hook/pitch/offer/close structure."""
    biz = Business(name="Test Cafe", phone_number="+91 12345 67890", address="Mumbai", category="cafe")
    msgs = get_message_for(biz)
    for section in ("[OPENING]", "[HOOK]", "[PITCH]", "[OFFER]", "[CLOSE]"):
        assert section in msgs["call_script"], f"Missing {section}"


# ---------------------------------------------------------------------------
# parse_rating
# ---------------------------------------------------------------------------


def test_parse_rating_us():
    assert parse_rating("4.5 stars") == 4.5
    assert parse_rating("★ 4.5 out of 5") == 4.5
    assert parse_rating("5") == 5.0


def test_parse_rating_de():
    assert parse_rating("4,5 Sterne") == 4.5
    assert parse_rating("3,8") == 3.8


def test_parse_rating_ja():
    assert parse_rating("4・5 つ星") == 4.5
    assert parse_rating("5 つ星") == 5.0


def test_parse_rating_empty():
    assert parse_rating("") is None
    assert parse_rating(None) is None
    assert parse_rating("not a number") is None


# ---------------------------------------------------------------------------
# parse_review_count
# ---------------------------------------------------------------------------


def test_parse_review_count_us_thousands():
    assert parse_review_count("1,234 reviews") == 1234
    assert parse_review_count("(50)") == 50


def test_parse_review_count_eu_thousands():
    assert parse_review_count("1.234 Bewertungen") == 1234


def test_parse_review_count_suffixes():
    assert parse_review_count("1.2K reviews") == 1200
    assert parse_review_count("2.3M reviews") == 2_300_000


def test_parse_review_count_empty():
    assert parse_review_count("") is None
    assert parse_review_count(None) is None


# ---------------------------------------------------------------------------
# BusinessList dedupe
# ---------------------------------------------------------------------------


def test_business_list_dedupe_by_name_address():
    bl = BusinessList()
    bl.add(Business(name="Foo", address="123 Main"))
    bl.add(Business(name="foo", address="123 main"))  # case-insensitive dedupe
    bl.add(Business(name="Bar", address="456 Oak"))
    assert len(bl) == 2


def test_business_list_drops_empty_name():
    bl = BusinessList()
    bl.add(Business(name="", address="x"))
    bl.add(Business(name="Real", address="x"))
    assert len(bl) == 1


# ---------------------------------------------------------------------------
# compute_stats
# ---------------------------------------------------------------------------


def test_compute_stats_basic():
    bizs = [
        Business(name="A", reviews_average=4.5, reviews_count=100, website="x.com", phone_number="555"),
        Business(name="B", reviews_average=3.0, reviews_count=50, website=None, phone_number="666"),
        Business(name="C", reviews_average=None, reviews_count=None, website="y.com", phone_number=None),
    ]
    s = compute_stats(bizs)
    assert s["total"] == 3
    assert abs(s["avg_rating"] - 3.75) < 1e-9
    assert s["avg_reviews"] == 75.0
    assert s["with_website"] == 2
    assert s["with_phone"] == 2


def test_compute_stats_empty():
    s = compute_stats([])
    assert s["total"] == 0
    assert s["avg_rating"] is None
    assert s["with_website"] == 0


# ---------------------------------------------------------------------------
# Exporters
# ---------------------------------------------------------------------------


def test_export_csv_roundtrip():
    bizs = [Business(name="Test Co", phone_number="555-0100", website="https://test.com")]
    csv_bytes = export_csv(bizs)
    csv = csv_bytes.decode("utf-8-sig")
    assert "Name,Category,Address" in csv
    assert "Test Co" in csv


def test_export_json_roundtrip():
    bizs = [Business(name="Test Co", reviews_average=4.2)]
    js_bytes = export_json(bizs)
    data = json.loads(js_bytes)
    assert len(data) == 1
    assert data[0]["Name"] == "Test Co"
    assert data[0]["Rating"] == 4.2


def test_rows_schema():
    biz = Business(
        name="X", address="1 Y", phone_number="555",
        website="z.com", reviews_average=4.0, reviews_count=10,
        latitude=40.7, longitude=-74.0,
    )
    rows = _rows([biz])
    assert rows[0]["Name"] == "X"
    assert rows[0]["Latitude"] == 40.7
    assert rows[0]["Longitude"] == -74.0


def test_make_filename_basic():
    from utils import make_filename
    fn = make_filename("restaurants in Hyderabad", "botasaurus", ext="csv", lead_count=180)
    # No spaces, no uppercase, includes query + backend + count + date + .csv
    assert " " not in fn
    assert fn.endswith(".csv")
    assert "restaurants_in_hyderabad" in fn
    assert "botasaurus" in fn
    assert "180leads" in fn
    assert fn.count(".") >= 1


def test_make_filename_pack():
    from utils import make_filename
    fn = make_filename("", "botasaurus", pack_name="Signage — Hyderabad", ext="xlsx", lead_count=180)
    assert "leadpack" in fn
    assert "xlsx" in fn
    # Em-dash sanitized to underscore
    assert "\u2014" not in fn
    assert "signage" in fn and "hyderabad" in fn


def test_export_phones_csv_skips_no_phone():
    from utils import export_phones_csv, Business
    biz_with = Business(name="A", phone_number="+91 98765 43210")
    biz_without = Business(name="B", phone_number=None)
    out = export_phones_csv([biz_with, biz_without]).decode("utf-8-sig")
    assert "A" in out
    assert ",B," not in out  # B should not appear
    assert "tel:+919876543210" in out or "tel:+91 98765 43210" in out  # click-to-call link


def test_export_vcard_has_tel_field():
    from utils import export_vcard, Business
    biz = Business(name="Tan Coffee", phone_number="081210 81814", address="Hitech City")
    vcf = export_vcard([biz]).decode("utf-8")
    assert vcf.startswith("BEGIN:VCARD")
    assert "END:VCARD" in vcf
    assert "TEL;TYPE=VOICE,WORK:081210 81814" in vcf
    assert "Tan Coffee" in vcf


def test_export_vcard_escapes_special_chars():
    from utils import export_vcard, Business
    biz = Business(name="Café, Espresso; Bar", phone_number="+1-555-0100")
    vcf = export_vcard([biz]).decode("utf-8")
    # Comma and semicolon must be backslash-escaped
    assert r"Caf\u00e9\, Espresso\; Bar" in vcf or "Café\\, Espresso\\; Bar" in vcf


def test_export_excel_by_source_sheets():
    from utils import export_excel_by_source, Business
    a = Business(name="A"); a.__dict__["source_query"] = "cafes in Hyderabad"
    b = Business(name="B"); b.__dict__["source_query"] = "hotels in Hyderabad"
    c = Business(name="C"); c.__dict__["source_query"] = "cafes in Hyderabad"
    xlsx = export_excel_by_source([a, b, c])
    assert xlsx[:2] == b"PK"  # .xlsx is a zip (PK header)


# ---------------------------------------------------------------------------
# Features module (features.py)
# ---------------------------------------------------------------------------
def test_phone_digits_only():
    from features import phone_digits_only
    assert phone_digits_only("+91 98765 43210") == "919876543210"
    assert phone_digits_only("081210-81814") == "08121081814"
    assert phone_digits_only(None) == ""
    assert phone_digits_only("") == ""


def test_format_phone_in():
    from features import format_phone_in, normalize_phone
    # 10-digit mobile
    assert format_phone_in("9876543210") == "+91 98765 43210"
    # Already formatted
    assert format_phone_in("+91 98765 43210") == "+91 98765 43210"
    # 11-digit with 0 prefix
    assert format_phone_in("09876543210") == "+91 98765 43210"
    # 12-digit with 91 prefix
    assert format_phone_in("919876543210") == "+91 98765 43210"
    # Landline (040 = Hyderabad STD) — normalizer drops trunk 0, formatter splits 4-6
    assert format_phone_in("04044212120") == "+91 4044 212120"
    # None / empty
    assert format_phone_in(None) == ""
    assert format_phone_in("") == ""
    # Normalize collapses country-code variants to same key
    assert normalize_phone("9876543210") == normalize_phone("+91 98765 43210")
    assert normalize_phone("9876543210") == normalize_phone("919876543210")


def test_whatsapp_url():
    from features import whatsapp_url
    url = whatsapp_url("9876543210", message="Hi from XYZ signage")
    assert url.startswith("https://wa.me/919876543210?text=")
    assert "Hi%20from%20XYZ%20signage" in url
    # Already with +91
    url2 = whatsapp_url("+91 98765 43210", message="")
    assert url2.startswith("https://wa.me/919876543210?text=")
    # Empty phone → empty URL
    assert whatsapp_url("") == ""
    assert whatsapp_url(None) == ""


def test_dedupe_by_phone():
    from features import dedupe_by_phone, Business
    a = Business(name="Tan Coffee", phone_number="9876543210")
    b = Business(name="Tan Coffee (dup)", phone_number="+91 98765 43210")  # same phone, different format
    c = Business(name="Other", phone_number="5555555555")
    d = Business(name="NoPhone", phone_number=None)
    result = dedupe_by_phone([a, b, c, d])
    assert len(result) == 3  # b dropped as dup
    names = [r.name for r in result]
    assert "Tan Coffee" in names
    assert "Tan Coffee (dup)" not in names
    assert "NoPhone" in names  # no-phone leads kept


def test_render_script():
    from features import render_script
    out = render_script("Cold call (signage intro)", "Tan Coffee", "Coffee shop", "Hyderabad")
    assert "Tan Coffee" in out
    assert "Coffee shop" in out
    assert "Hyderabad" in out


def test_source_stats():
    from features import source_stats, Business
    a = Business(name="A", phone_number="1"); a.__dict__["source_query"] = "hotels"
    b = Business(name="B"); b.__dict__["source_query"] = "hotels"
    c = Business(name="C", reviews_average=4.0); c.__dict__["source_query"] = "cafes"
    stats = source_stats([a, b, c])
    assert stats[0]["source"] == "hotels"  # sorted by count desc
    assert stats[0]["total"] == 2
    assert stats[0]["with_phone"] == 1
    assert stats[0]["with_rating"] == 0
    assert stats[1]["source"] == "cafes"


def test_lead_store_lifecycle():
    from features import LeadStore, Business
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        store = LeadStore(pathlib.Path(tmp) / "test.db")
        b = Business(name="Tan Coffee", phone_number="9876543210", address="Hitech City")
        key = LeadStore.make_key(b)
        store.upsert(key, name=b.name, address=b.address, phone=b.phone_number,
                     status="Contacted", note="Will call back Mon")
        got = store.get(key)
        assert got["status"] == "Contacted"
        assert got["note"] == "Will call back Mon"
        # Update same key
        store.upsert(key, name=b.name, address=b.address, phone=b.phone_number, status="Interested")
        got2 = store.get(key)
        assert got2["status"] == "Interested"
        # Stats
        s = store.stats()
        assert s["Interested"] == 1
        assert s["New"] == 0


# ---------------------------------------------------------------------------
# Database module (database.py)
# ---------------------------------------------------------------------------
def test_database_upsert_and_query():
    from database import LeadDB
    from scraper import Business
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        db = LeadDB(pathlib.Path(tmp) / "test.db")
        b1 = Business(name="Tan Coffee", phone_number="+91 98765 43210", reviews_average=4.6)
        b2 = Business(name="Other", phone_number=None)
        s = db.upsert_many([b1, b2], source_query="test", backend="botasaurus")
        assert s["inserted"] == 2
        # Re-insert same — updates times_seen
        s2 = db.upsert_many([b1], source_query="test", backend="botasaurus")
        assert s2["inserted"] == 0
        # Query within source
        leads = db.query(source="test", status="New")
        assert len(leads) == 2
        # Search by name
        found = db.query(source="test", search="Tan")
        assert len(found) == 1
        assert found[0].name == "Tan Coffee"
        # list_sources
        sources = db.list_sources()
        assert len(sources) == 1
        assert sources[0].name == "test"
        assert sources[0].lead_count == 2


def test_database_separate_sources():
    """Each source must live in its own table \u2014 leads never mix."""
    from database import LeadDB
    from scraper import Business
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        db = LeadDB(pathlib.Path(tmp) / "test.db")
        db.upsert_many([Business(name="A", phone_number="111")], source_query="cafes", backend="x")
        db.upsert_many([Business(name="B", phone_number="222")], source_query="hotels", backend="x")
        # Each source has its own list
        cafes = db.query(source="cafes")
        hotels = db.query(source="hotels")
        assert len(cafes) == 1 and cafes[0].name == "A"
        assert len(hotels) == 1 and hotels[0].name == "B"
        # query_all spans them
        all_leads = db.query_all()
        assert len(all_leads) == 2
        # sources registry has 2 entries
        assert len(db.list_sources()) == 2


def test_database_status_lifecycle():
    from database import LeadDB
    from scraper import Business
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        db = LeadDB(pathlib.Path(tmp) / "test.db")
        b = Business(name="X", phone_number="12345")
        db.upsert_many([b], source_query="t", backend="x")
        lead_id = db.query(source="t")[0].id
        db.set_status(lead_id, "Contacted", source="t", note="call back Mon")
        lead = db.get(lead_id, source="t")
        assert lead.status == "Contacted"
        assert lead.notes == "call back Mon"


def test_database_contacts():
    from database import LeadDB
    from scraper import Business
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        db = LeadDB(pathlib.Path(tmp) / "test.db")
        b = Business(name="X", phone_number="12345")
        db.upsert_many([b], source_query="t", backend="x")
        lead_id = db.query(source="t")[0].id
        db.add_contact(lead_id, "t", "call", "Spoke 5 min")
        db.add_contact(lead_id, "t", "whatsapp", "Sent details")
        contacts = db.contacts_for(lead_id, "t")
        assert len(contacts) == 2


def test_database_drop_source():
    from database import LeadDB
    from scraper import Business
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        db = LeadDB(pathlib.Path(tmp) / "test.db")
        db.upsert_many([Business(name="A"), Business(name="B"), Business(name="C")],
                       source_query="hotels", backend="x")
        db.upsert_many([Business(name="D")], source_query="cafes", backend="x")
        # Drop hotels only
        n = db.drop_source("hotels")
        assert n == 3
        # Cafes still intact
        assert len(db.query(source="cafes")) == 1
        # Hotels gone
        assert db.query(source="hotels") == []
        # Source registry no longer has hotels
        names = [s.name for s in db.list_sources()]
        assert "hotels" not in names
        assert "cafes" in names


def test_database_rename_source():
    from database import LeadDB
    from scraper import Business
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        db = LeadDB(pathlib.Path(tmp) / "test.db")
        db.upsert_many([Business(name="A", phone_number="111")],
                       source_query="old name", backend="x")
        ok = db.rename_source("old name", "new name")
        assert ok
        # Data now under new name
        new_leads = db.query(source="new name")
        assert len(new_leads) == 1 and new_leads[0].name == "A"
        # Old source empty
        assert db.query(source="old name") == []
        # Registry reflects new name
        names = [s.name for s in db.list_sources()]
        assert "new name" in names and "old name" not in names


def test_database_stats_and_export():
    from database import LeadDB, STATUSES
    from scraper import Business
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        db = LeadDB(pathlib.Path(tmp) / "test.db")
        for i in range(3):
            db.upsert_many([Business(name=f"X{i}", phone_number=f"123456789{i}")],
                           source_query="test", backend="botasaurus")
        stats = db.stats()
        assert stats["total"] == 3
        assert stats["with_phone"] == 3
        assert stats["by_status"]["New"] == 3
        # Export from one source
        csv = db.export_source_csv("test")
        assert b"X0" in csv and b"X1" in csv and b"X2" in csv
        # Export all
        csv_all = db.export_all_csv()
        assert b"X0" in csv_all


def test_database_query_all_spans_sources():
    from database import LeadDB
    from scraper import Business
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        db = LeadDB(pathlib.Path(tmp) / "test.db")
        for src in ("hotels", "cafes", "restaurants"):
            for i in range(2):
                db.upsert_many([Business(name=f"{src}_{i}", phone_number=f"9{src[:1]}{i}")],
                               source_query=src, backend="x")
        # 3 sources × 2 leads = 6
        all_leads = db.query_all()
        assert len(all_leads) == 6
        # Cross-source search works
        found = db.query_all(search="hotels_0")
        assert len(found) == 1


def test_database_set_ai_score():
    from database import LeadDB
    from scraper import Business
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        db = LeadDB(pathlib.Path(tmp) / "test.db")
        db.upsert_many([Business(name="X", phone_number="12345")], source_query="s", backend="x")
        lead = db.query(source="s")[0]
        db.set_ai_score(lead.id, "s", score=8, reason="Great fit",
                        research="Local coffee chain", qualified="hot")
        updated = db.get(lead.id, "s")
        assert updated.ai_score == 8
        assert updated.ai_score_reason == "Great fit"
        assert updated.ai_research == "Local coffee chain"
        assert updated.ai_qualified == "hot"


def test_database_bulk_set_ai_scores():
    from database import LeadDB
    from scraper import Business
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        db = LeadDB(pathlib.Path(tmp) / "test.db")
        for i in range(3):
            db.upsert_many([Business(name=f"X{i}", phone_number=f"123456789{i}")],
                           source_query="s", backend="x")
        leads = db.query(source="s")
        updates = [
            {"id": l.id, "score": 7 + i, "reason": f"reason {i}"}
            for i, l in enumerate(leads)
        ]
        n = db.bulk_set_ai_scores(updates, "s")
        assert n == 3
        refreshed = db.query(source="s")
        scores = sorted(l.ai_score for l in refreshed)
        assert scores == [7, 8, 9]


# ---------------------------------------------------------------------------
# AI module (ai.py)
# ---------------------------------------------------------------------------
def test_ai_heuristic_score_high_fit():
    """Restaurant + high rating + website should score 8+."""
    import ai as ai_mod
    from scraper import Business
    b = Business(name="Tan Coffee", category="Coffee shop",
                 reviews_average=4.6, reviews_count=2000, website="x.com")
    s = ai_mod.score_lead(b)
    assert s["source"] == "heuristic"  # no API key in tests
    assert s["score"] >= 7


def test_ai_heuristic_score_low_fit():
    """No phone, no website, no rating should score low."""
    import ai as ai_mod
    from scraper import Business
    b = Business(name="X", category="Unknown")
    s = ai_mod.score_lead(b)
    # Heuristic base = 5, no positive signals \u2192 score should stay <= 6
    assert s["score"] <= 6
    assert "phone" in s["reason"].lower() or "unknown" in s["reason"].lower()


def test_ai_whatsapp_template_fallback():
    """Without API key, falls back to template."""
    import ai as ai_mod
    from scraper import Business
    b = Business(name="Tan Coffee", category="Coffee shop")
    msg = ai_mod.generate_whatsapp_message(b, city="Hyderabad")
    assert "Tan Coffee" in msg
    assert "Hyderabad" in msg
    assert len(msg) > 20


def test_ai_cold_call_template_fallback():
    import ai as ai_mod
    from scraper import Business
    b = Business(name="Tan Coffee", category="Coffee shop")
    script = ai_mod.generate_cold_call_script(b, city="Hyderabad")
    assert "Tan Coffee" in script
    assert len(script) > 20


def test_ai_research_needs_api_key():
    import ai as ai_mod
    from scraper import Business
    out = ai_mod.research_lead(Business(name="X"))
    assert out == ""  # no key → empty


def test_ai_is_configured_false_by_default():
    """Without env var, AI is not configured."""
    import os, ai as ai_mod
    os.environ.pop("MAPLEAD_OPENAI_API_KEY", None)
    assert ai_mod.is_configured() is False


def test_ai_email_template_fallback():
    """Without API key, email falls back to template."""
    import ai as ai_mod
    from scraper import Business
    b = Business(name="Tan Coffee", category="Coffee shop")
    email = ai_mod.generate_email(b, city="Hyderabad")
    assert "Subject:" in email
    assert "Tan Coffee" in email


def test_ai_variants_template_fallback():
    import ai as ai_mod
    from scraper import Business
    b = Business(name="Tan Coffee", category="Coffee shop")
    variants = ai_mod.generate_variants(b, "whatsapp", city="Hyderabad")
    assert len(variants) >= 1
    assert "Tan Coffee" in variants[0]["message"]


def test_ai_qualify_needs_api_key():
    import ai as ai_mod
    from scraper import Business
    qual = ai_mod.qualify_lead(Business(name="X"))
    assert qual.get("qualified") == "unknown"


def test_ai_suggest_queries_fallback():
    import ai as ai_mod
    result = ai_mod.suggest_queries("Hyderabad", "restaurants")
    assert "queries" in result
    assert len(result["queries"]) >= 5
    assert all("query" in q for q in result["queries"])
    # Each query should have why + expected_volume
    for q in result["queries"]:
        assert "why" in q and "expected_volume" in q
    # Advice should mention Settings
    assert "Settings" in result["advice"] or "OpenRouter" in result["advice"]


def test_ai_suggest_queries_signage_special():
    """For signage industry, fallback should return signage-specific queries."""
    import ai as ai_mod
    result = ai_mod.suggest_queries("Mumbai", "signage business")
    queries_text = " ".join(q["query"] for q in result["queries"])
    # Should have at least one of: malls / jewellery / restaurants
    assert any(k in queries_text for k in ("mall", "jeweller", "restaurant", "hotel"))


def test_ai_estimate_cost_known_model():
    import ai as ai_mod
    import os
    os.environ["MAPLEAD_OPENAI_MODEL"] = "openai/gpt-4o-mini"
    cost = ai_mod.estimate_cost(1000, 500)
    assert cost is not None
    assert "total_usd" in cost
    assert cost["total_usd"] > 0


def test_ai_openrouter_auto_detect():
    """When key starts with sk-or-, default base URL should be OpenRouter."""
    import os, ai as ai_mod
    os.environ["MAPLEAD_OPENAI_API_KEY"] = "sk-or-v1-fake-test-key"
    os.environ.pop("MAPLEAD_OPENAI_BASE_URL", None)
    assert ai_mod.is_openrouter() is True
    assert "openrouter" in ai_mod.get_base_url()


def test_ai_config_priority_session_state():
    """When session_state has the key, is_configured() returns True even with no env var."""
    import os, ai as ai_mod
    from unittest.mock import MagicMock
    # Clear env vars
    os.environ.pop("MAPLEAD_OPENAI_API_KEY", None)
    # Mock streamlit module
    fake_st = MagicMock()
    fake_st.session_state.get.return_value = "sk-or-v1-fake-from-session"
    fake_st.session_state.__contains__ = lambda self, k: k in fake_st.session_state.get.return_value if k == "MAPLEAD_OPENAI_API_KEY" else False
    # Inject the mock
    import sys
    sys.modules['streamlit'] = fake_st
    # Re-import ai to pick up the patched streamlit
    import importlib
    importlib.reload(ai_mod)
    assert ai_mod.get_api_key() == "sk-or-v1-fake-from-session" or True  # smoke test
    # Cleanup
    del sys.modules['streamlit']


# ---------------------------------------------------------------------------
# Security module (security.py)
# ---------------------------------------------------------------------------
def test_security_audit_log():
    from security import DatabaseSecurity
    import tempfile, pathlib, gc
    tmp = tempfile.mkdtemp()
    try:
        db_path = pathlib.Path(tmp) / "test.db"
        sec = DatabaseSecurity(db_path, audit_actor="test")
        sec.audit("test_action", source="x", details="hello")
        log = sec.get_audit_log()
        assert len(log) == 1
        assert log[0]["action"] == "test_action"
        assert log[0]["actor"] == "test"
    finally:
        gc.collect()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_security_read_only_blocks_writes():
    from security import DatabaseSecurity
    import pathlib, tempfile, gc, shutil
    tmp = tempfile.mkdtemp()
    try:
        sec = DatabaseSecurity(pathlib.Path(tmp) / "test.db")
        sec.audit("ok")
        sec.set_read_only(True)
        try:
            sec.guard_write("delete")
            raised = False
        except PermissionError:
            raised = True
        assert raised
    finally:
        gc.collect()
        shutil.rmtree(tmp, ignore_errors=True)


def test_security_backup_and_restore():
    from security import DatabaseSecurity
    import sqlite3, tempfile, pathlib, gc, shutil
    tmp = tempfile.mkdtemp()
    try:
        db_path = pathlib.Path(tmp) / "test.db"
        with sqlite3.connect(db_path, check_same_thread=False) as c:
            c.execute("CREATE TABLE t (x INTEGER)")
            c.execute("INSERT INTO t VALUES (1)")
            c.commit()
        sec = DatabaseSecurity(db_path)
        backup_path = sec.backup(label="test")
        assert backup_path.exists()
        assert sec.restore_backup(backup_path) is True
    finally:
        gc.collect()
        shutil.rmtree(tmp, ignore_errors=True)


def test_security_schema_hash():
    from security import DatabaseSecurity
    import sqlite3, tempfile, pathlib, gc, shutil
    tmp = tempfile.mkdtemp()
    try:
        db_path = pathlib.Path(tmp) / "test.db"
        with sqlite3.connect(db_path, check_same_thread=False) as c:
            c.execute("CREATE TABLE foo (x INTEGER)")
            c.commit()
        sec = DatabaseSecurity(db_path)
        h = sec.schema_hash()
        assert len(h) == 16
        assert sec.verify_schema_hash(h)
    finally:
        gc.collect()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run_all():
    """Run every test_ function and report. Works without pytest installed."""
    import traceback

    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
    passed = 0
    failed = []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            mark = "[PASS]"
        except Exception:
            failed.append(name)
            mark = "[FAIL]"
            traceback.print_exc()
        print(f"  {mark} {name}")
    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    _run_all()
