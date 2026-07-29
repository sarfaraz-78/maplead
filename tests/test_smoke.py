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
