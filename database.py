"""
MapLead — Persistent lead database (SQLite)
===========================================

A real database behind MapLead so scraped leads survive browser restarts,
status updates are saved, and you can build a history of every campaign.

Schema
------
leads
    id, lead_key (unique), name, address, phone, phone_digits,
    category, website, rating, reviews_count, latitude, longitude,
    google_maps_url, source_query, backend, status, notes, tags,
    first_seen, last_seen, times_seen

contacts
    id, lead_id (FK), kind (call/email/whatsapp/meeting), summary,
    occurred_at

Usage
-----
    db = LeadDB()                         # opens ./maplead.db
    db.upsert_many(businesses, source="restaurants in Hyderabad", backend="botasaurus")
    leads = db.query(status="New", source="cafes in Hyderabad")
    db.set_status(lead_id=42, status="Contacted", note="Will call back Mon")
    db.add_contact(lead_id=42, kind="call", summary="Spoke with manager")
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from scraper import Business

STATUSES: list[str] = ["New", "Contacted", "Interested", "Quoted", "Won", "Lost"]


def phone_digits(phone: Optional[str]) -> str:
    if not phone:
        return ""
    return re.sub(r"\D", "", phone)


def make_lead_key(b: Business) -> str:
    """Stable per-business key. Phone-based when available (most stable),
    otherwise a slug from name + address."""
    d = phone_digits(b.phone_number)
    if d:
        return f"phone:{d}"
    slug = re.sub(r"\s+", " ", f"{(b.name or '').strip().lower()}|{(b.address or '').strip().lower()}")
    return f"id:{slug}"


@dataclass
class Lead:
    """Row from the leads table."""
    id: int
    lead_key: str
    name: Optional[str]
    address: Optional[str]
    phone: Optional[str]
    phone_digits: Optional[str]
    category: Optional[str]
    website: Optional[str]
    rating: Optional[float]
    reviews_count: Optional[int]
    latitude: Optional[float]
    longitude: Optional[float]
    google_maps_url: Optional[str]
    source_query: Optional[str]
    backend: Optional[str]
    status: str
    notes: Optional[str]
    tags: Optional[str]
    first_seen: str
    last_seen: str
    times_seen: int


def _row_to_lead(row: sqlite3.Row) -> Lead:
    return Lead(**{k: row[k] for k in row.keys()})


class LeadDB:
    """SQLite-backed lead store. Thread-safe for Streamlit's single-thread usage."""

    def __init__(self, db_path: str | Path = "maplead.db") -> None:
        self.db_path = Path(db_path)
        self._init_schema()

    # ---------- connection helper ----------
    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")  # better concurrency
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_key        TEXT NOT NULL UNIQUE,
                    name            TEXT,
                    address         TEXT,
                    phone           TEXT,
                    phone_digits    TEXT,
                    category        TEXT,
                    website         TEXT,
                    rating          REAL,
                    reviews_count   INTEGER,
                    latitude        REAL,
                    longitude       REAL,
                    google_maps_url TEXT,
                    source_query    TEXT,
                    backend         TEXT,
                    status          TEXT NOT NULL DEFAULT 'New',
                    notes           TEXT,
                    tags            TEXT,
                    first_seen      TEXT NOT NULL,
                    last_seen       TEXT NOT NULL,
                    times_seen      INTEGER NOT NULL DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS idx_leads_status     ON leads(status);
                CREATE INDEX IF NOT EXISTS idx_leads_source     ON leads(source_query);
                CREATE INDEX IF NOT EXISTS idx_leads_last_seen  ON leads(last_seen);

                CREATE TABLE IF NOT EXISTS contacts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id     INTEGER NOT NULL,
                    kind        TEXT NOT NULL,  -- call, whatsapp, email, meeting, note
                    summary     TEXT,
                    occurred_at TEXT NOT NULL,
                    FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_contacts_lead ON contacts(lead_id);
                """
            )

    # ---------- ingest ----------
    def upsert_many(
        self,
        businesses: Iterable[Business],
        *,
        source_query: str = "",
        backend: str = "",
    ) -> dict[str, int]:
        """Insert or update many leads. Returns counts of inserted/updated/unchanged."""
        now = datetime.now().isoformat(timespec="seconds")
        inserted = updated = unchanged = 0
        with self._conn() as c:
            for b in businesses:
                key = make_lead_key(b)
                existing = c.execute(
                    "SELECT id, source_query, backend, times_seen FROM leads WHERE lead_key = ?",
                    (key,),
                ).fetchone()
                if existing is None:
                    c.execute(
                        """
                        INSERT INTO leads (
                            lead_key, name, address, phone, phone_digits, category,
                            website, rating, reviews_count, latitude, longitude,
                            google_maps_url, source_query, backend,
                            status, notes, tags, first_seen, last_seen, times_seen
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            key, b.name, b.address, b.phone_number,
                            phone_digits(b.phone_number) or None,
                            b.category, b.website, b.reviews_average, b.reviews_count,
                            b.latitude, b.longitude, b.google_maps_url,
                            source_query or None, backend or None,
                            "New", None, None, now, now,
                        ),
                    )
                    inserted += 1
                else:
                    # Update fields, append source_query if new
                    sources = (existing["source_query"] or "")
                    new_sources = sources
                    if source_query and source_query not in sources:
                        new_sources = f"{sources};{source_query}" if sources else source_query
                    backends = (existing["backend"] or "")
                    new_backends = backends
                    if backend and backend not in backends:
                        new_backends = f"{backends};{backend}" if backends else backend
                    c.execute(
                        """
                        UPDATE leads SET
                            name=?, address=?, phone=?, phone_digits=?, category=?,
                            website=?, rating=?, reviews_count=?, latitude=?, longitude=?,
                            google_maps_url=?, source_query=?, backend=?,
                            last_seen=?, times_seen=times_seen+1
                        WHERE lead_key=?
                        """,
                        (
                            b.name, b.address, b.phone_number,
                            phone_digits(b.phone_number) or None,
                            b.category, b.website, b.reviews_average, b.reviews_count,
                            b.latitude, b.longitude, b.google_maps_url,
                            new_sources, new_backends, now, key,
                        ),
                    )
                    # Check if anything actually changed
                    if c.execute("SELECT changes()").fetchone()[0]:
                        updated += 1
                    else:
                        unchanged += 1
        return {"inserted": inserted, "updated": updated, "unchanged": unchanged}

    # ---------- read ----------
    def query(
        self,
        *,
        status: Optional[list[str] | str] = None,
        source: Optional[str] = None,
        search: Optional[str] = None,
        has_phone: Optional[bool] = None,
        min_rating: Optional[float] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 1000,
        order_by: str = "last_seen DESC",
    ) -> list[Lead]:
        """Flexible search. All filters are optional and AND-combined."""
        where: list[str] = []
        params: list = []
        if status:
            if isinstance(status, str):
                status = [status]
            placeholders = ",".join("?" for _ in status)
            where.append(f"status IN ({placeholders})")
            params.extend(status)
        if source:
            where.append("source_query LIKE ?")
            params.append(f"%{source}%")
        if search:
            where.append("(name LIKE ? OR address LIKE ? OR phone LIKE ? OR category LIKE ?)")
            s = f"%{search}%"
            params.extend([s, s, s, s])
        if has_phone is True:
            where.append("phone_digits IS NOT NULL AND phone_digits != ''")
        elif has_phone is False:
            where.append("(phone_digits IS NULL OR phone_digits = '')")
        if min_rating is not None:
            where.append("rating >= ?")
            params.append(min_rating)
        if since:
            where.append("last_seen >= ?")
            params.append(since)
        if until:
            where.append("last_seen <= ?")
            params.append(until)

        sql = "SELECT * FROM leads"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # Whitelist order_by columns to prevent SQL injection
        allowed = {"last_seen DESC", "last_seen ASC", "rating DESC", "rating ASC",
                   "name ASC", "name DESC", "first_seen DESC", "times_seen DESC"}
        if order_by not in allowed:
            order_by = "last_seen DESC"
        sql += f" ORDER BY {order_by} LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [_row_to_lead(r) for r in rows]

    def get(self, lead_id: int) -> Optional[Lead]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return _row_to_lead(row) if row else None

    # ---------- update ----------
    def set_status(self, lead_id: int, status: str, note: Optional[str] = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"Invalid status '{status}'. Choose from {STATUSES}")
        with self._conn() as c:
            if note is not None:
                c.execute("UPDATE leads SET status=?, notes=?, last_seen=? WHERE id=?",
                          (status, note, datetime.now().isoformat(timespec="seconds"), lead_id))
            else:
                c.execute("UPDATE leads SET status=?, last_seen=? WHERE id=?",
                          (status, datetime.now().isoformat(timespec="seconds"), lead_id))

    def bulk_set_status(self, lead_ids: list[int], status: str) -> int:
        if not lead_ids or status not in STATUSES:
            return 0
        with self._conn() as c:
            qmarks = ",".join("?" for _ in lead_ids)
            cur = c.execute(
                f"UPDATE leads SET status=?, last_seen=? WHERE id IN ({qmarks})",
                [status, datetime.now().isoformat(timespec="seconds"), *lead_ids],
            )
            return cur.rowcount

    def add_tags(self, lead_ids: list[int], tags: list[str]) -> None:
        if not lead_ids or not tags:
            return
        with self._conn() as c:
            for lid in lead_ids:
                row = c.execute("SELECT tags FROM leads WHERE id=?", (lid,)).fetchone()
                if not row:
                    continue
                existing = set(t.strip() for t in (row["tags"] or "").split(",") if t.strip())
                existing.update(t.strip() for t in tags if t.strip())
                c.execute(
                    "UPDATE leads SET tags=?, last_seen=? WHERE id=?",
                    (",".join(sorted(existing)), datetime.now().isoformat(timespec="seconds"), lid),
                )

    def delete(self, lead_ids: list[int]) -> int:
        if not lead_ids:
            return 0
        with self._conn() as c:
            qmarks = ",".join("?" for _ in lead_ids)
            cur = c.execute(f"DELETE FROM leads WHERE id IN ({qmarks})", lead_ids)
            return cur.rowcount

    # ---------- contacts (call log) ----------
    def add_contact(self, lead_id: int, kind: str, summary: str = "") -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO contacts (lead_id, kind, summary, occurred_at) VALUES (?, ?, ?, ?)",
                (lead_id, kind, summary, datetime.now().isoformat(timespec="seconds")),
            )
            c.execute("UPDATE leads SET last_seen=? WHERE id=?",
                      (datetime.now().isoformat(timespec="seconds"), lead_id))
            return cur.lastrowid

    def contacts_for(self, lead_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM contacts WHERE lead_id=? ORDER BY occurred_at DESC",
                (lead_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- stats ----------
    def stats(self) -> dict:
        """Return high-level stats: counts by status, total, with-phone, etc."""
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
            with_phone = c.execute(
                "SELECT COUNT(*) AS n FROM leads WHERE phone_digits IS NOT NULL AND phone_digits != ''"
            ).fetchone()["n"]
            status_rows = c.execute(
                "SELECT status, COUNT(*) AS n FROM leads GROUP BY status"
            ).fetchall()
            sources = c.execute(
                """SELECT IFNULL(source_query, 'Direct') AS src, COUNT(*) AS n
                   FROM leads GROUP BY src ORDER BY n DESC LIMIT 20"""
            ).fetchall()
            recent = c.execute(
                "SELECT COUNT(*) AS n FROM leads WHERE last_seen >= datetime('now', '-7 days')"
            ).fetchone()["n"]
        by_status = {s: 0 for s in STATUSES}
        for r in status_rows:
            by_status[r["status"]] = r["n"]
        return {
            "total": total,
            "with_phone": with_phone,
            "recent_7d": recent,
            "by_status": by_status,
            "by_source": [dict(r) for r in sources],
        }

    # ---------- export/import ----------
    def export_to_csv_bytes(self, filter_kwargs: Optional[dict] = None) -> bytes:
        """Export leads matching filters (or all if None) as CSV bytes."""
        leads = self.query(**(filter_kwargs or {}), limit=100_000)
        import io, csv
        buf = io.StringIO()
        if leads:
            w = csv.DictWriter(buf, fieldnames=list(vars(leads[0]).keys()))
            w.writeheader()
            for lead in leads:
                w.writerow(vars(lead))
        return buf.getvalue().encode("utf-8-sig")

    def reset(self) -> None:
        """Drop all tables (for tests only)."""
        with self._conn() as c:
            c.executescript("DROP TABLE IF EXISTS contacts; DROP TABLE IF EXISTS leads;")
        self._init_schema()