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
