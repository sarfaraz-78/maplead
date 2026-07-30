"""
MapLead — CRM UI components
===========================

High-level helpers for the in-app CRM. Wraps LeadDB with views and
widgets that Streamlit pages can drop in directly:

- get_pipeline_summary()   counts per status + funnel numbers
- get_today_tasks()        leads due today for follow-up
- get_recent_activity()    most recent N contact-log entries
- quick_log_contact()      helper to log a "called X" entry fast
- bulk_update_status()     filtered bulk-status update
- export_leads_csv()       CSV bytes for st.download_button
- get_funnel_chart_data()  status counts for funnel chart
- get_hot_leads()          AI-scored leads above a threshold, untopiched

All callers pass a LeadDB instance.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from database import LeadDB, Lead, STATUSES


# ---------------------------------------------------------------------------
# Status pipeline
# ---------------------------------------------------------------------------

# Map of status -> sort index for stable pipeline ordering
STATUS_ORDER: dict[str, int] = {s: i for i, s in enumerate(STATUSES)}


def get_pipeline_summary(db: LeadDB) -> list[dict[str, Any]]:
    """Count leads at each status across all sources.

    Returns: [{"status": "New", "count": 42, "color": "#3B82F6"}, ...]
    """
    counts: dict[str, int] = {s: 0 for s in STATUSES}
    for src in db.list_sources():
        # Query all leads in source (capped) and tally statuses
        try:
            leads = db.query(source=src.name, limit=10_000)
            for lead in leads:
                if lead.status in counts:
                    counts[lead.status] += 1
        except Exception:
            continue
    palette = {
        "New":        "#3B82F6",
        "Contacted":  "#8B5CF6",
        "Interested": "#F59E0B",
        "Quoted":     "#06B6D4",
        "Won":        "#10B981",
        "Lost":       "#EF4444",
    }
    return [
        {"status": s, "count": counts[s], "color": palette.get(s, "#94A3B8")}
        for s in STATUSES
    ]


def get_funnel_chart_data(pipeline: list[dict[str, Any]]) -> dict[str, int]:
    """Convert pipeline summary to dict for chart input."""
    return {row["status"]: row["count"] for row in pipeline}


# ---------------------------------------------------------------------------
# Follow-up tasks
# ---------------------------------------------------------------------------

def get_today_tasks(db: LeadDB, days_ahead: int = 0) -> list[dict[str, Any]]:
    """Leads where last_seen is in the past N days (default = today).

    These are basic 'due for follow-up' candidates. Real production
    would have explicit next_follow_up_date column; this is a heuristic.
    """
    cutoff = datetime.now() - timedelta(days=max(0, days_ahead) + 7)
    today_str = datetime.now().date().isoformat()
    out: list[dict[str, Any]] = []
    for src in db.list_sources():
        leads = db.query(source=src.name, limit=1000)
        for lead in leads:
            # Lead already contacted? Skip contact outreach tasks.
            if lead.status != "New":
                continue
            # Lead has phone or email? Otherwise can't really outreach.
            if not lead.phone and not lead.website:
                continue
            out.append({
                "id": lead.id,
                "name": lead.name or "(no name)",
                "source": src.name,
                "status": lead.status,
                "phone": lead.phone,
                "category": lead.category or "",
                "first_seen": lead.first_seen,
                "last_seen": lead.last_seen,
            })
            if len(out) >= 50:
                return out
    return out


# ---------------------------------------------------------------------------
# Activity timeline
# ---------------------------------------------------------------------------

def get_recent_activity(db: LeadDB, limit: int = 10) -> list[dict[str, Any]]:
    """Most recent N contact-log entries across all sources.

    Returns: [{"lead_id", "source", "kind", "summary", "at", "lead_name"}, ...]
    """
    rows: list[dict[str, Any]] = []
    for src in db.list_sources():
        try:
            contacts = db.contacts_for_source(src.name, limit=limit)
        except Exception:
            continue
        for c in contacts:
            # Try to get the lead's name
            lead = db.get(c["lead_id"], src.name)
            rows.append({
                "source": src.name,
                "lead_id": c["lead_id"],
                "lead_name": lead.name if lead and lead.name else f"Lead #{c['lead_id']}",
                "kind": c.get("kind", "note"),
                "summary": c.get("summary", ""),
                "at": c.get("at", c.get("created_at", "")),
            })
    # Sort newest first
    rows.sort(key=lambda r: r["at"] or "", reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------------------
# Hot leads (AI-scored, not yet contacted)
# ---------------------------------------------------------------------------

def get_hot_leads(db: LeadDB, min_score: int = 7, limit: int = 20) -> list[Lead]:
    """Leads across all sources with AI score >= min_score and status=New."""
    out: list[Lead] = []
    for src in db.list_sources():
        try:
            leads = db.query(source=src.name, limit=1000)
        except Exception:
            continue
        for lead in leads:
            if lead.status == "New" and (lead.ai_score or 0) >= min_score:
                out.append(lead)
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------------------
# Conversion rates
# ---------------------------------------------------------------------------

def compute_conversion_rates(db: LeadDB) -> dict[str, float]:
    """Return conversion rate per pipeline stage (as fraction of total).

    Example: {'new_to_contacted': 0.32, 'contacted_to_qualified': 0.45, ...}
    """
    pipeline = get_pipeline_summary(db)
    total = sum(p["count"] for p in pipeline)
    if total == 0:
        return {}
    counts = {p["status"]: p["count"] for p in pipeline}
    rates = {}
    if counts.get("New", 0) > 0:
        contacted_or_beyond = total - counts["New"]
        rates["new_to_contacted"] = contacted_or_beyond / total
    if (counts.get("Contacted", 0) + counts.get("Interested", 0)) > 0:
        interested_or_beyond = (
            counts.get("Interested", 0)
            + counts.get("Quoted", 0)
            + counts.get("Won", 0)
            + counts.get("Lost", 0)
        )
        rates["contacted_to_interested"] = interested_or_beyond / total
    if counts.get("Won", 0) > 0:
        rates["overall_to_won"] = counts["Won"] / total
    if counts.get("Lost", 0) > 0:
        rates["lost_rate"] = counts["Lost"] / total
    return rates


# ---------------------------------------------------------------------------
# Bulk operations
# ---------------------------------------------------------------------------

def bulk_update_status(
    db: LeadDB,
    source: str,
    current_status: str,
    new_status: str,
) -> int:
    """Move all leads at current_status to new_status within one source.

    Returns the number of updated leads.
    """
    leads = db.query(source=source, status=current_status, limit=10_000)
    ids = [l.id for l in leads]
    if not ids:
        return 0
    return db.bulk_set_status(ids, source, new_status)


def delete_leads_by_status(db: LeadDB, source: str, status: str) -> int:
    """Delete all leads at a status within a source."""
    leads = db.query(source=source, status=status, limit=10_000)
    ids = [l.id for l in leads]
    if not ids:
        return 0
    return db.delete(ids, source)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_leads_csv(leads: Iterable[Lead]) -> bytes:
    """CSV bytes for st.download_button."""
    leads = list(leads)
    buf = io.StringIO()
    if leads:
        fieldnames = [
            f.name for f in Lead.__dataclass_fields__.values()
        ]
        w = csv.DictWriter(buf, fieldnames=fieldnames)
        w.writeheader()
        for lead in leads:
            row = {k: getattr(lead, k) for k in fieldnames}
            # Convert datetime to string
            for k, v in row.items():
                if isinstance(v, datetime):
                    row[k] = v.isoformat()
            w.writerow(row)
    return buf.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------------------
# "Quick log" helpers
# ---------------------------------------------------------------------------

KIND_OPTIONS = ["call", "email", "whatsapp", "meeting", "note"]


def quick_log_contact(
    db: LeadDB,
    lead_id: int,
    source: str,
    kind: str,
    summary: str = "",
) -> int:
    """Log a quick contact activity. Returns contact_id."""
    if kind not in KIND_OPTIONS:
        kind = "note"
    return db.add_contact(lead_id, source, kind=kind, summary=summary)
