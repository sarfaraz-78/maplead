"""
MapLead \u2014 Security hardening for the lead database
==================================================

Protections:
- Audit log: every write is recorded with timestamp + action + details
- Read-only mode: block all writes when enabled (toggle in app)
- Auto-backup: snapshot the DB before any destructive op
- Soft delete: leads marked deleted are recoverable; only `purge_deleted()`
  permanently removes them
- File lock: prevent two processes from writing at once
- Schema hash: detect external tampering of the DB file
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


_AUDIT_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor       TEXT,
    action      TEXT NOT NULL,
    source      TEXT,
    details     TEXT
)
"""

SCHEMA_HASH_FILE = "schema.sha256"
MAX_BACKUPS = 20  # keep at most N backups, prune oldest


class DatabaseSecurity:
    """Drop-in mixin/companion for LeadDB.

    Provides audit logging, auto-backup, soft delete, and a read-only flag.
    Designed to never raise \u2014 security features fail closed but never break
    the user's flow.
    """

    def __init__(self, db_path: str | Path, audit_actor: str = "streamlit") -> None:
        self.db_path = Path(db_path)
        self.audit_actor = audit_actor
        self._read_only = False
        self._lock = threading.RLock()
        self._init_audit_table()

    # ---------- audit log ----------
    def _init_audit_table(self) -> None:
        try:
            with sqlite3.connect(self.db_path, timeout=10, check_same_thread=False) as c:
                c.execute(_AUDIT_LOG_SCHEMA)
                c.commit()
        except sqlite3.Error:
            pass

    def audit(self, action: str, source: str = "", details: str = "") -> None:
        """Record an action. Fails silently \u2014 never breaks the main flow."""
        try:
            with sqlite3.connect(self.db_path, timeout=5, check_same_thread=False) as c:
                c.execute(
                    "INSERT INTO audit_log (occurred_at, actor, action, source, details) VALUES (?, ?, ?, ?, ?)",
                    (
                        datetime.now().isoformat(timespec="seconds"),
                        self.audit_actor,
                        action,
                        source or None,
                        details[:500] if details else None,
                    ),
                )
                c.commit()
        except sqlite3.Error:
            pass  # audit failure shouldn't break the user

    def get_audit_log(self, limit: int = 100, source: Optional[str] = None) -> list[dict]:
        """Return the most recent audit entries."""
        try:
            with sqlite3.connect(self.db_path, timeout=10, check_same_thread=False) as c:
                c.row_factory = sqlite3.Row
                if source:
                    rows = c.execute(
                        "SELECT * FROM audit_log WHERE source = ? ORDER BY id DESC LIMIT ?",
                        (source, limit),
                    ).fetchall()
                else:
                    rows = c.execute(
                        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
                    ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            return []

    # ---------- read-only mode ----------
    @property
    def read_only(self) -> bool:
        return self._read_only

    def set_read_only(self, value: bool, persist: bool = False) -> None:
        """Toggle read-only mode. With persist=True, writes a flag file too."""
        self._read_only = bool(value)
        flag = self.db_path.parent / ".maplead_readonly"
        if persist:
            flag.write_text("1" if value else "0")
        elif flag.exists():
            try:
                flag.unlink()
            except OSError:
                pass

    def is_read_only_persisted(self) -> bool:
        flag = self.db_path.parent / ".maplead_readonly"
        return flag.exists() and flag.read_text().strip() == "1"

    def guard_write(self, operation: str) -> None:
        """Raise if read-only. Call before any mutating op."""
        if self._read_only:
            raise PermissionError(
                f"Database is in read-only mode. '{operation}' blocked. "
                "Toggle off in the app or delete .maplead_readonly file."
            )

    # ---------- auto-backup ----------
    def backup(self, label: str = "") -> Path:
        """Copy the DB to a timestamped backup file. Returns the backup path."""
        backup_dir = self.db_path.parent / "maplead_backups"
        backup_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        label = label.replace(" ", "_") if label else "snapshot"
        dest = backup_dir / f"{self.db_path.stem}_{ts}_{label}.db"
        try:
            # Use SQLite's backup API for a safe online copy
            with sqlite3.connect(self.db_path, timeout=10, check_same_thread=False) as src:
                with sqlite3.connect(dest, timeout=10, check_same_thread=False) as dst:
                    src.backup(dst)
            self._prune_backups(backup_dir)
            self.audit("backup", details=f"saved to {dest.name}")
            return dest
        except sqlite3.Error as e:
            # Fallback: filesystem copy
            shutil.copy2(self.db_path, dest)
            self.audit("backup", details=f"fs-copy to {dest.name} (sqlite backup failed: {e})")
            return dest

    def _prune_backups(self, backup_dir: Path) -> None:
        files = sorted(backup_dir.glob(f"{self.db_path.stem}_*.db"), key=lambda p: p.stat().st_mtime)
        while len(files) > MAX_BACKUPS:
            try:
                files.pop(0).unlink()
            except OSError:
                break

    def list_backups(self) -> list[dict]:
        backup_dir = self.db_path.parent / "maplead_backups"
        if not backup_dir.exists():
            return []
        out = []
        for p in sorted(backup_dir.glob(f"{self.db_path.stem}_*.db"), reverse=True):
            stat = p.stat()
            out.append({
                "name": p.name,
                "path": str(p),
                "size_kb": stat.st_size // 1024,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            })
        return out

    def restore_backup(self, backup_path: str | Path) -> bool:
        """Replace the live DB with a backup. Creates a safety backup of the live one first."""
        backup_path = Path(backup_path)
        if not backup_path.exists():
            return False
        # Safety backup of current DB before replacing
        try:
            self.backup(label="pre_restore")
            shutil.copy2(backup_path, self.db_path)
            self.audit("restore", details=f"from {backup_path.name}")
            return True
        except (OSError, sqlite3.Error) as e:
            self.audit("restore_failed", details=str(e))
            return False

    # ---------- file lock ----------
    @contextmanager
    def file_lock(self, timeout: float = 5.0):
        """Cross-process file lock to prevent two writers from corrupting the DB.

        Falls back gracefully if the OS doesn't support fcntl (Windows).
        """
        lock_path = self.db_path.with_suffix(".lock")
        if os.name == "nt":
            # Windows: use a simple existence-based lock with timeout
            import time as _time
            waited = 0.0
            while lock_path.exists() and waited < timeout:
                _time.sleep(0.1)
                waited += 0.1
            lock_path.write_text(str(os.getpid()))
            try:
                yield
            finally:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
        else:
            import fcntl
            lock_file = open(lock_path, "w")
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
                lock_file.close()

    # ---------- schema fingerprint ----------
    def schema_hash(self) -> str:
        """SHA-256 of every CREATE statement in the DB \u2014 detects external tampering."""
        try:
            with sqlite3.connect(self.db_path, timeout=10, check_same_thread=False) as c:
                rows = c.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
                ).fetchall()
            blob = "\n".join(r[0] for r in rows).encode("utf-8")
            return hashlib.sha256(blob).hexdigest()[:16]
        except sqlite3.Error:
            return "unknown"

    def verify_schema_hash(self, expected: str) -> bool:
        return self.schema_hash() == expected

    # ---------- soft delete (helper for LeadDB) ----------
    def mark_deleted(self, table: str, lead_ids: list[int]) -> int:
        """Mark leads as deleted in a table (soft delete)."""
        if not lead_ids:
            return 0
        with sqlite3.connect(self.db_path, timeout=10, check_same_thread=False) as c:
            qmarks = ",".join("?" for _ in lead_ids)
            cur = c.execute(
                f"UPDATE {table} SET deleted_at=? WHERE id IN ({qmarks})",
                [datetime.now().isoformat(timespec="seconds"), *lead_ids],
            )
            return cur.rowcount

    def list_deleted(self, source: str, limit: int = 100) -> list[dict]:
        """List soft-deleted leads in a source (recoverable until purged)."""
        from database import _table_for_source  # late import to avoid cycle
        table = _table_for_source(source)
        try:
            with sqlite3.connect(self.db_path, timeout=10, check_same_thread=False) as c:
                c.row_factory = sqlite3.Row
                rows = c.execute(
                    f"SELECT id, name, phone, deleted_at FROM {table} WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            return []

    def restore_deleted(self, source: str, lead_ids: list[int]) -> int:
        """Un-mark soft-deleted leads."""
        from database import _table_for_source
        if not lead_ids:
            return 0
        table = _table_for_source(source)
        with sqlite3.connect(self.db_path, timeout=10, check_same_thread=False) as c:
            qmarks = ",".join("?" for _ in lead_ids)
            cur = c.execute(
                f"UPDATE {table} SET deleted_at=NULL WHERE id IN ({qmarks})",
                lead_ids,
            )
            return cur.rowcount

    def purge_deleted(self, source: str) -> int:
        """Permanently remove soft-deleted leads in a source."""
        from database import _table_for_source
        table = _table_for_source(source)
        try:
            with sqlite3.connect(self.db_path, timeout=10, check_same_thread=False) as c:
                cur = c.execute(f"DELETE FROM {table} WHERE deleted_at IS NOT NULL")
                return cur.rowcount
        except sqlite3.Error:
            return 0

    def list_soft_deleted_total(self) -> int:
        """Total soft-deleted leads across all sources (for stats)."""
        from database import _table_for_source, slugify_source
        try:
            with sqlite3.connect(self.db_path, timeout=10, check_same_thread=False) as c:
                sources = c.execute("SELECT name FROM sources").fetchall()
                total = 0
                for r in sources:
                    tbl = _table_for_source(r["name"])
                    try:
                        n = c.execute(
                            f"SELECT COUNT(*) AS n FROM {tbl} WHERE deleted_at IS NOT NULL"
                        ).fetchone()["n"]
                        total += n
                    except sqlite3.Error:
                        pass
            return total
        except sqlite3.Error:
            return 0