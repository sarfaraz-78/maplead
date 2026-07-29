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
        # Query with status as string (not list)
        leads = db.query(status="New")
        assert len(leads) == 2
        # Search by name
        found = db.query(search="Tan")
        assert len(found) == 1
        assert found[0].name == "Tan Coffee"
        # Search by source
        by_src = db.query(source="test")
        assert len(by_src) == 2


def test_database_status_lifecycle():
    from database import LeadDB
    from scraper import Business
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        db = LeadDB(pathlib.Path(tmp) / "test.db")
        b = Business(name="X", phone_number="12345")
        db.upsert_many([b], source_query="t", backend="x")
        lead_id = db.query()[0].id
        db.set_status(lead_id, "Contacted", note="call back Mon")
        lead = db.get(lead_id)
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
        lead_id = db.query()[0].id
        db.add_contact(lead_id, "call", "Spoke 5 min")
        db.add_contact(lead_id, "whatsapp", "Sent details")
        contacts = db.contacts_for(lead_id)
        assert len(contacts) == 2


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
        csv = db.export_to_csv_bytes()
        assert b"X0" in csv and b"X1" in csv and b"X2" in csv


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
