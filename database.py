"""
MapLead — Persistent lead database, one table per source
=======================================================

Each scrape (or campaign) gets its own SQLite table named after the
source query, so leads from different campaigns never mix.

Schema
------
sources
    registry of all known source tables \u2014 one row per scrape.
    Lets us list, rename, and drop sources without scanning the schema.

leads_<slug>          one table per source, identical schema:
    id, name, address, phone, phone_digits, category, website,
    rating, reviews_count, latitude, longitude, google_maps_url,
    backend, status, notes, first_seen, last_seen, times_seen,
    UNIQUE(phone_digits) within the source

contacts_<slug>       one contact-log table per source, FK to leads

If the user scrapes without a `source_query` (e.g. an ad-hoc search),
leads go to `leads__default`.

Usage
-----
    db = LeadDB()
    db.upsert_many(businesses, source="restaurants in Hyderabad", backend="botasaurus")
    db.list_sources()                         # all known sources + counts
    leads = db.query(source="restaurants in Hyderabad", status="New")
    leads = db.query_all(status="New")        # search across all sources
    db.set_status(lead_id=42, status="Contacted", source="restaurants in Hyderabad")
    db.drop_source("hotels in Hyderabad")     # nuke one source completely
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

DEFAULT_SOURCE = "(no source)"


def phone_digits(phone: Optional[str]) -> str:
    if not phone:
        return ""
    return re.sub(r"\D", "", phone)


def slugify_source(source: str) -> str:
    """Convert 'Restaurants in Hyderabad!' -> 'restaurants_in_hyderabad'."""
    s = re.sub(r"[^a-z0-9]+", "_", (source or "").lower()).strip("_")
    return s[:60] or "default"


def _table_for_source(source: str) -> str:
    """Compute the leads table name for a given source string."""
    return f"leads_{slugify_source(source)}"


def _contacts_table_for_source(source: str) -> str:
    return f"contacts_{slugify_source(source)}"


@dataclass
class SourceInfo:
    id: int
    name: str
    table_name: str
    backend: str
    created_at: str
    last_used_at: str
    lead_count: int


@dataclass
class Lead:
    """Row from a leads_<slug> table."""
    id: int
    source: str
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
    backend: Optional[str]
    status: str
    notes: Optional[str]
    ai_score: Optional[int] = None
    ai_score_reason: Optional[str] = None
    ai_research: Optional[str] = None
    ai_qualified: Optional[str] = None
    # Multi-channel outreach messages (added so CRM hot-leads can preview them)
    ai_subject: Optional[str] = None
    ai_subject_b: Optional[str] = None
    ai_subject_c: Optional[str] = None
    ai_body_email: Optional[str] = None
    ai_whatsapp: Optional[str] = None
    ai_sms: Optional[str] = None
    ai_call_script: Optional[str] = None
    ai_followup_day3: Optional[str] = None
    ai_followup_day7: Optional[str] = None
    ai_followup_day14: Optional[str] = None
    ai_angle_id: Optional[str] = None
    ai_messages_source: Optional[str] = None
    first_seen: str = ""
    last_seen: str = ""
    times_seen: int = 1


def _row_to_lead(row: sqlite3.Row, source: str) -> Lead:
    """Build a Lead, tolerating tables that may be missing newer columns."""
    keys = set(row.keys())
    fields = {
        "id": row["id"],
        "source": source,
        "name": row["name"] if "name" in keys else None,
        "address": row["address"] if "address" in keys else None,
        "phone": row["phone"] if "phone" in keys else None,
        "phone_digits": row["phone_digits"] if "phone_digits" in keys else None,
        "category": row["category"] if "category" in keys else None,
        "website": row["website"] if "website" in keys else None,
        "rating": row["rating"] if "rating" in keys else None,
        "reviews_count": row["reviews_count"] if "reviews_count" in keys else None,
        "latitude": row["latitude"] if "latitude" in keys else None,
        "longitude": row["longitude"] if "longitude" in keys else None,
        "google_maps_url": row["google_maps_url"] if "google_maps_url" in keys else None,
        "backend": row["backend"] if "backend" in keys else None,
        "status": row["status"] if "status" in keys else "New",
        "notes": row["notes"] if "notes" in keys else None,
        "ai_score": row["ai_score"] if "ai_score" in keys else None,
        "ai_score_reason": row["ai_score_reason"] if "ai_score_reason" in keys else None,
        "ai_research": row["ai_research"] if "ai_research" in keys else None,
        "ai_qualified": row["ai_qualified"] if "ai_qualified" in keys else None,
        # Per-lead multi-channel messages (added later; safe if missing)
        "ai_subject": row["ai_subject"] if "ai_subject" in keys else None,
        "ai_subject_b": row["ai_subject_b"] if "ai_subject_b" in keys else None,
        "ai_subject_c": row["ai_subject_c"] if "ai_subject_c" in keys else None,
        "ai_body_email": row["ai_body_email"] if "ai_body_email" in keys else None,
        "ai_whatsapp": row["ai_whatsapp"] if "ai_whatsapp" in keys else None,
        "ai_sms": row["ai_sms"] if "ai_sms" in keys else None,
        "ai_call_script": row["ai_call_script"] if "ai_call_script" in keys else None,
        "ai_followup_day3": row["ai_followup_day3"] if "ai_followup_day3" in keys else None,
        "ai_followup_day7": row["ai_followup_day7"] if "ai_followup_day7" in keys else None,
        "ai_followup_day14": row["ai_followup_day14"] if "ai_followup_day14" in keys else None,
        "ai_angle_id": row["ai_angle_id"] if "ai_angle_id" in keys else None,
        "ai_messages_source": row["ai_messages_source"] if "ai_messages_source" in keys else None,
        "first_seen": row["first_seen"] if "first_seen" in keys else "",
        "last_seen": row["last_seen"] if "last_seen" in keys else "",
        "times_seen": row["times_seen"] if "times_seen" in keys else 1,
    }
    return Lead(**fields)


# Standard column list used for both CREATE TABLE and INSERT/UPDATE.
LEAD_COLUMNS = (
    "name", "address", "phone", "phone_digits", "category",
    "website", "rating", "reviews_count", "latitude", "longitude",
    "google_maps_url", "backend",
    "status", "notes",
    "first_seen", "last_seen", "times_seen",
)


def _create_leads_table_sql(table: str) -> str:
    """SQL to create the standard leads table for a source."""
    return f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
            backend         TEXT,
            status          TEXT NOT NULL DEFAULT 'New',
            notes           TEXT,
            ai_score        INTEGER,
            ai_score_reason TEXT,
            ai_research     TEXT,
            ai_qualified    TEXT,
            ai_qualified_at TEXT,
            ai_subject       TEXT,
            ai_subject_b     TEXT,
            ai_subject_c     TEXT,
            ai_body_email    TEXT,
            ai_whatsapp      TEXT,
            ai_sms           TEXT,
            ai_call_script   TEXT,
            ai_followup_day3 TEXT,
            ai_followup_day7 TEXT,
            ai_followup_day14 TEXT,
            ai_angle_id      TEXT,
            ai_messages_source TEXT,
            deleted_at      TEXT,
            first_seen      TEXT NOT NULL,
            last_seen       TEXT NOT NULL,
            times_seen      INTEGER NOT NULL DEFAULT 1,
            UNIQUE(phone_digits)
        )
    """


def _create_contacts_table_sql(table: str) -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id     INTEGER NOT NULL,
            kind        TEXT NOT NULL,
            summary     TEXT,
            occurred_at TEXT NOT NULL,
            FOREIGN KEY (lead_id) REFERENCES {table.replace('contacts_', 'leads_')}(id) ON DELETE CASCADE
        )
    """


class LeadDB:
    """SQLite-backed lead store. One leads_<slug> table per source query."""

    def __init__(self, db_path: str | Path = "maplead.db") -> None:
        self.db_path = Path(db_path)
        self._init_schema()

    # ---------- connection helper ----------
    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
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
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    name          TEXT NOT NULL,
                    table_name    TEXT NOT NULL UNIQUE,
                    backend       TEXT,
                    created_at    TEXT NOT NULL,
                    last_used_at  TEXT NOT NULL,
                    lead_count    INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Persistent app settings (user profile + message defaults)
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            # Migrate ALL existing leads_<slug> tables so CRM can show
            # multi-channel messages on leads from past scrapes too.
            tables = [
                r["table_name"]
                for r in c.execute(
                    "SELECT table_name FROM sources WHERE table_name LIKE 'leads_%'"
                ).fetchall()
            ]
            for t in tables:
                self._migrate_leads_columns(c, t)
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_sources_name ON sources(name)"
            )

    # ---------- sources registry ----------
    def _ensure_source(self, source: str, backend: str = "") -> str:
        """Register the source if needed. Returns the table name."""
        source = (source or "").strip() or DEFAULT_SOURCE
        table = _table_for_source(source)
        with self._conn() as c:
            existing = c.execute(
                "SELECT id FROM sources WHERE table_name = ?", (table,)
            ).fetchone()
            now = datetime.now().isoformat(timespec="seconds")
            if existing is None:
                c.execute(
                    """INSERT INTO sources (name, table_name, backend, created_at, last_used_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (source, table, backend or None, now, now),
                )
            else:
                c.execute(
                    "UPDATE sources SET last_used_at = ? WHERE id = ?",
                    (now, existing["id"]),
                )
            # Create the leads + contacts tables for this source
            c.execute(_create_leads_table_sql(table))
            c.execute(_create_contacts_table_sql(_contacts_table_for_source(source)))
            # Auto-migrate existing tables: add any missing columns (newer fields)
            self._migrate_leads_columns(c, table)
        return table


    def _migrate_leads_columns(self, c, table: str) -> None:
        """Add any missing columns to an existing leads_<slug> table.

        Safe to call repeatedly. SQLite's ALTER TABLE ADD COLUMN is idempotent
        in our usage because we check for existence first.
        """
        # Columns added in v2 (multi-channel AI messages)
        new_columns = [
            ("ai_subject", "TEXT"),
            ("ai_subject_b", "TEXT"),
            ("ai_subject_c", "TEXT"),
            ("ai_body_email", "TEXT"),
            ("ai_whatsapp", "TEXT"),
            ("ai_sms", "TEXT"),
            ("ai_call_script", "TEXT"),
            ("ai_followup_day3", "TEXT"),
            ("ai_followup_day7", "TEXT"),
            ("ai_followup_day14", "TEXT"),
            ("ai_angle_id", "TEXT"),
            ("ai_messages_source", "TEXT"),
        ]
        existing_cols = {row["name"] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
        for col_name, col_type in new_columns:
            if col_name not in existing_cols:
                try:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass  # column might already exist (race)
        return table

    def list_sources(self) -> list[SourceInfo]:
        """Return all known sources, freshest first."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM sources ORDER BY last_used_at DESC"
            ).fetchall()
        # Recompute live lead_count (cheaper than maintaining it on every write)
        out = []
        for r in rows:
            count = c.execute(
                f"SELECT COUNT(*) AS n FROM {r['table_name']}"
            ).fetchone()["n"] if False else 0  # placeholder, see below
            try:
                with self._conn() as c2:
                    count = c2.execute(
                        f"SELECT COUNT(*) AS n FROM {r['table_name']}"
                    ).fetchone()["n"]
            except sqlite3.OperationalError:
                count = 0  # table missing
            out.append(SourceInfo(
                id=r["id"],
                name=r["name"],
                table_name=r["table_name"],
                backend=r["backend"] or "",
                created_at=r["created_at"],
                last_used_at=r["last_used_at"],
                lead_count=count,
            ))
        return out

    def drop_source(self, source: str) -> int:
        """Delete all leads and the table for one source. Returns count deleted."""
        table = _table_for_source(source)
        contacts = _contacts_table_for_source(source)
        with self._conn() as c:
            try:
                count = c.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            except sqlite3.OperationalError:
                count = 0
            c.execute(f"DROP TABLE IF EXISTS {table}")
            c.execute(f"DROP TABLE IF EXISTS {contacts}")
            c.execute("DELETE FROM sources WHERE table_name = ?", (table,))
        return count

    def rename_source(self, old: str, new: str) -> bool:
        """Rename a source. Migrates all data to the new table. Returns success."""
        old = (old or "").strip()
        new = (new or "").strip()
        if not old or not new or old == new:
            return False
        old_table = _table_for_source(old)
        new_table = _table_for_source(new)
        if old_table == new_table:
            # Only the display name differs \u2014 update sources registry
            with self._conn() as c:
                c.execute("UPDATE sources SET name = ? WHERE table_name = ?",
                          (new, old_table))
            return True
        # Move data
        with self._conn() as c:
            try:
                c.execute(f"SELECT COUNT(*) AS n FROM {old_table}").fetchone()
            except sqlite3.OperationalError:
                return False
            c.execute(f"ALTER TABLE {old_table} RENAME TO {new_table}")
            # Contacts table: rename too if it exists
            old_contacts = _contacts_table_for_source(old)
            new_contacts = _contacts_table_for_source(new)
            try:
                c.execute(f"ALTER TABLE {old_contacts} RENAME TO {new_contacts}")
            except sqlite3.OperationalError:
                pass  # no contacts table yet
            c.execute(
                "UPDATE sources SET name = ?, table_name = ?, last_used_at = ? WHERE table_name = ?",
                (new, new_table, datetime.now().isoformat(timespec="seconds"), old_table),
            )
        return True

    # ---------- ingest ----------
    def upsert_many(
        self,
        businesses: Iterable[Business],
        *,
        source_query: str = "",
        backend: str = "",
    ) -> dict[str, int]:
        """Insert/update many leads into the table for `source_query`."""
        table = self._ensure_source(source_query, backend)
        now = datetime.now().isoformat(timespec="seconds")
        inserted = updated = unchanged = 0
        with self._conn() as c:
            for b in businesses:
                pdig = phone_digits(b.phone_number) or None
                # Try to find existing by phone within this source
                existing = None
                if pdig:
                    existing = c.execute(
                        f"SELECT id, times_seen FROM {table} WHERE phone_digits = ?",
                        (pdig,),
                    ).fetchone()
                if existing is None:
                    # New lead
                    try:
                        c.execute(
                            f"""INSERT INTO {table} (
                                name, address, phone, phone_digits, category,
                                website, rating, reviews_count, latitude, longitude,
                                google_maps_url, backend,
                                status, notes, first_seen, last_seen, times_seen
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'New', NULL, ?, ?, 1)""",
                            (
                                b.name, b.address, b.phone_number, pdig,
                                b.category, b.website, b.reviews_average, b.reviews_count,
                                b.latitude, b.longitude, b.google_maps_url, backend or None,
                                now, now,
                            ),
                        )
                        inserted += 1
                    except sqlite3.IntegrityError:
                        # Race: someone inserted the same phone_digits between
                        # SELECT and INSERT. Treat as unchanged.
                        unchanged += 1
                else:
                    c.execute(
                        f"""UPDATE {table} SET
                            name=?, address=?, phone=?, category=?,
                            website=?, rating=?, reviews_count=?, latitude=?, longitude=?,
                            google_maps_url=?, backend=?, last_seen=?, times_seen=times_seen+1
                        WHERE id=?""",
                        (
                            b.name, b.address, b.phone_number, b.category,
                            b.website, b.reviews_average, b.reviews_count,
                            b.latitude, b.longitude, b.google_maps_url, backend or None,
                            now, existing["id"],
                        ),
                    )
                    if c.execute("SELECT changes()").fetchone()[0]:
                        updated += 1
                    else:
                        unchanged += 1
        return {"inserted": inserted, "updated": updated, "unchanged": unchanged, "source": source_query or DEFAULT_SOURCE}

    # ---------- read ----------
    def query(
        self,
        *,
        source: str,
        status: Optional[list[str] | str] = None,
        search: Optional[str] = None,
        has_phone: Optional[bool] = None,
        min_rating: Optional[float] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 1000,
        order_by: str = "last_seen DESC",
    ) -> list[Lead]:
        """Search within a single source's table."""
        table = _table_for_source(source)
        where: list[str] = []
        params: list = []
        if status:
            if isinstance(status, str):
                status = [status]
            placeholders = ",".join("?" for _ in status)
            where.append(f"status IN ({placeholders})")
            params.extend(status)
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
        allowed = {"last_seen DESC", "last_seen ASC", "rating DESC", "rating ASC",
                   "name ASC", "name DESC", "first_seen DESC", "times_seen DESC"}
        if order_by not in allowed:
            order_by = "last_seen DESC"
        sql = f"SELECT * FROM {table}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order_by} LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            try:
                rows = c.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return []
        return [_row_to_lead(r, source) for r in rows]

    def query_all(
        self,
        *,
        status: Optional[list[str] | str] = None,
        search: Optional[str] = None,
        has_phone: Optional[bool] = None,
        min_rating: Optional[float] = None,
        order_by: str = "last_seen DESC",
        limit: int = 1000,
    ) -> list[Lead]:
        """Search across ALL sources. Each lead is tagged with its source."""
        sources = self.list_sources()
        if not sources:
            return []
        all_leads: list[Lead] = []
        for s in sources:
            leads = self.query(
                source=s.name,
                status=status,
                search=search,
                has_phone=has_phone,
                min_rating=min_rating,
                order_by=order_by,
                limit=limit,
            )
            all_leads.extend(leads)
        # Final safety sort (each source's table sort is already applied, but
        # we cross-source-sort too).
        allowed = {"last_seen DESC", "last_seen ASC", "rating DESC", "rating ASC",
                   "name ASC", "name DESC", "first_seen DESC", "times_seen DESC"}
        if order_by not in allowed:
            order_by = "last_seen DESC"
        col = order_by.split()[0]
        reverse = order_by.endswith("DESC")
        # Some columns like rating/reviews_count may be None — sort with None last
        all_leads.sort(key=lambda l: (getattr(l, col) is None, getattr(l, col, None)), reverse=reverse)
        return all_leads[:limit]

    def get(self, lead_id: int, source: str) -> Optional[Lead]:
        table = _table_for_source(source)
        with self._conn() as c:
            try:
                row = c.execute(f"SELECT * FROM {table} WHERE id = ?", (lead_id,)).fetchone()
            except sqlite3.OperationalError:
                return None
        return _row_to_lead(row, source) if row else None

    # ---------- update ----------
    def set_status(
        self,
        lead_id: int,
        status: str,
        source: str,
        note: Optional[str] = None,
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"Invalid status '{status}'. Choose from {STATUSES}")
        table = _table_for_source(source)
        with self._conn() as c:
            if note is not None:
                c.execute(
                    f"UPDATE {table} SET status=?, notes=?, last_seen=? WHERE id=?",
                    (status, note, datetime.now().isoformat(timespec="seconds"), lead_id),
                )
            else:
                c.execute(
                    f"UPDATE {table} SET status=?, last_seen=? WHERE id=?",
                    (status, datetime.now().isoformat(timespec="seconds"), lead_id),
                )

    def bulk_set_status(self, lead_ids: list[int], source: str, status: str) -> int:
        if not lead_ids or status not in STATUSES:
            return 0
        table = _table_for_source(source)
        with self._conn() as c:
            qmarks = ",".join("?" for _ in lead_ids)
            cur = c.execute(
                f"UPDATE {table} SET status=?, last_seen=? WHERE id IN ({qmarks})",
                [status, datetime.now().isoformat(timespec="seconds"), *lead_ids],
            )
            return cur.rowcount

    def delete(self, lead_ids: list[int], source: str) -> int:
        if not lead_ids:
            return 0
        table = _table_for_source(source)
        with self._conn() as c:
            qmarks = ",".join("?" for _ in lead_ids)
            cur = c.execute(f"DELETE FROM {table} WHERE id IN ({qmarks})", lead_ids)
            return cur.rowcount

    # ---------- AI scoring ----------
    def set_ai_score(
        self,
        lead_id: int,
        source: str,
        score: int,
        reason: str,
        research: Optional[str] = None,
        qualified: Optional[str] = None,
    ) -> None:
        """Persist an AI score (+ optional research + qualification) for one lead."""
        table = _table_for_source(source)
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            c.execute(
                f"""UPDATE {table} SET
                    ai_score=?, ai_score_reason=?,
                    ai_research=COALESCE(?, ai_research),
                    ai_qualified=COALESCE(?, ai_qualified),
                    ai_qualified_at=?, last_seen=?
                WHERE id=?""",
                (score, reason, research, qualified, now, now, lead_id),
            )

    def bulk_set_ai_scores(self, items: list[dict], source: str) -> int:
        """Bulk-update AI scores. Each item: {id, score, reason, research?, qualified?}."""
        if not items:
            return 0
        table = _table_for_source(source)
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            n = 0
            for it in items:
                cur = c.execute(
                    f"""UPDATE {table} SET
                        ai_score=?, ai_score_reason=?,
                        ai_research=COALESCE(?, ai_research),
                        ai_qualified=COALESCE(?, ai_qualified),
                        ai_qualified_at=?, last_seen=?
                    WHERE id=?""",
                    (
                        it["score"], it.get("reason", ""),
                        it.get("research"), it.get("qualified"),
                        now, now, it["id"],
                    ),
                )
                n += cur.rowcount
        return n

    def get_leads_to_score(self, source: str, limit: int = 100) -> list[Lead]:
        """Get leads that haven't been AI-scored yet (or all if you want re-scoring)."""
        return self.query(source=source, limit=limit)

    # ---------- persistent settings (user profile + message defaults) ----------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        """Get a string-typed setting. Returns default if missing."""
        with self._conn() as c:
            row = c.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def get_setting_dict(self, prefix: str = "") -> dict[str, str]:
        """Return all settings starting with prefix as a dict. 'msg_*' keys get prefix stripped."""
        with self._conn() as c:
            rows = c.execute("SELECT key, value FROM app_settings").fetchall()
        out = {}
        for r in rows:
            k = r["key"]
            if k.startswith(prefix):
                stripped = k[len(prefix):] if prefix else k
                out[stripped] = r["value"]
        return out

    def set_setting(self, key: str, value: str) -> None:
        """Upsert a string setting."""
        if value is None:
            return
        with self._conn() as c:
            c.execute(
                """INSERT INTO app_settings (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value, updated_at=excluded.updated_at""",
                (key, str(value), datetime.now().isoformat(timespec="seconds")),
            )

    def set_settings(self, items: dict[str, str], prefix: str = "") -> None:
        """Upsert many settings at once. Prepends prefix to each key."""
        with self._conn() as c:
            now = datetime.now().isoformat(timespec="seconds")
            for k, v in items.items():
                if v is None:
                    continue
                key = prefix + k
                c.execute(
                    """INSERT INTO app_settings (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                           value=excluded.value, updated_at=excluded.updated_at""",
                    (key, str(v), now),
                )

    def delete_setting(self, key: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM app_settings WHERE key=?", (key,))

    # ---------- contacts ----------
    def add_contact(self, lead_id: int, source: str, kind: str, summary: str = "") -> int:
        table_leads = _table_for_source(source)
        table_contacts = _contacts_table_for_source(source)
        with self._conn() as c:
            cur = c.execute(
                f"INSERT INTO {table_contacts} (lead_id, kind, summary, occurred_at) VALUES (?, ?, ?, ?)",
                (lead_id, kind, summary, datetime.now().isoformat(timespec="seconds")),
            )
            c.execute(
                f"UPDATE {table_leads} SET last_seen=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), lead_id),
            )
            return cur.lastrowid

    def contacts_for(self, lead_id: int, source: str) -> list[dict]:
        table = _contacts_table_for_source(source)
        with self._conn() as c:
            try:
                rows = c.execute(
                    f"SELECT * FROM {table} WHERE lead_id=? ORDER BY occurred_at DESC",
                    (lead_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(r) for r in rows]

    # ---------- stats ----------
    def stats(self) -> dict:
        """High-level stats: total across all sources, per-source breakdown, etc."""
        with self._conn() as c:
            sources_rows = c.execute(
                "SELECT name, lead_count FROM sources ORDER BY lead_count DESC"
            ).fetchall()
            # Total across all
            total = 0
            with_phone = 0
            by_status: dict[str, int] = {s: 0 for s in STATUSES}
            for sr in sources_rows:
                tbl = _table_for_source(sr["name"])
                try:
                    rows = c.execute(
                        f"SELECT status, COUNT(*) AS n, "
                        f"SUM(CASE WHEN phone_digits IS NOT NULL AND phone_digits != '' THEN 1 ELSE 0 END) AS ph "
                        f"FROM {tbl} GROUP BY status"
                    ).fetchall()
                    for r in rows:
                        by_status[r["status"]] = by_status.get(r["status"], 0) + r["n"]
                        total += r["n"]
                        with_phone += r["ph"] or 0
                except sqlite3.OperationalError:
                    pass
        return {
            "total": total,
            "with_phone": with_phone,
            "by_status": by_status,
            "by_source": [dict(r) for r in sources_rows],
        }

    def export_source_csv(self, source: str) -> bytes:
        """Export all leads from one source as CSV bytes."""
        leads = self.query(source=source, limit=100_000)
        return self._leads_to_csv(leads)

    def export_all_csv(self) -> bytes:
        """Export ALL leads from ALL sources as one combined CSV."""
        leads = self.query_all(limit=100_000)
        return self._leads_to_csv(leads)

    @staticmethod
    def _leads_to_csv(leads: list[Lead]) -> bytes:
        import io, csv
        buf = io.StringIO()
        if leads:
            # Use dataclass fields
            fieldnames = [f for f in vars(leads[0]).keys()]
            w = csv.DictWriter(buf, fieldnames=fieldnames)
            w.writeheader()
            for lead in leads:
                w.writerow(vars(lead))
        return buf.getvalue().encode("utf-8-sig")

    def reset(self) -> None:
        """Drop everything. For tests."""
        with self._conn() as c:
            sources = c.execute("SELECT table_name FROM sources").fetchall()
            for r in sources:
                c.execute(f"DROP TABLE IF EXISTS {r['table_name']}")
                c.execute(f"DROP TABLE IF EXISTS {r['table_name'].replace('leads_', 'contacts_')}")
            c.execute("DROP TABLE IF EXISTS sources")
        self._init_schema()