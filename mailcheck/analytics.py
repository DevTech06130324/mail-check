"""Read-only local email analytics, with one scope for totals and pagination."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .db import LATEST_CLASSIFICATION
from .taxonomy import BY_NAME, CATEGORIES, TIER_ACT, TIER_REPLY, TIER_UNKNOWN, label_of, tier_of

_RECEIVED = "COALESCE(m.date_utc, m.fetched_at)"
_INSTANT = f"julianday({_RECEIVED})"
_FROM = "FROM messages m JOIN accounts a ON a.id = m.account_id " + LATEST_CLASSIFICATION
_ATTENTION = tuple(c.name for c in CATEGORIES if c.tier in (TIER_ACT, TIER_REPLY, TIER_UNKNOWN))


class DashboardFilters(BaseModel):
    """Validated global scope; focus fields refine only the message explorer."""

    model_config = ConfigDict(extra="forbid")
    start: date | None = None
    end: date | None = None
    days: int = Field(default=30, ge=1, le=90)
    interval: Literal["day", "week"] = "day"
    tz: str = "UTC"
    account: str | None = Field(default=None, max_length=200)
    categories: list[str] = Field(default_factory=list, max_length=len(CATEGORIES))
    status: Literal["all", "pending", "done"] = "all"
    action: Literal["all", "yes", "no"] = "all"
    q: str = Field(default="", max_length=200)
    sort: Literal["newest", "oldest"] = "newest"
    page: int = Field(default=1, ge=1, le=1_000_000)
    page_size: int = Field(default=50, ge=1, le=100)
    focus_date: date | None = None
    focus_category: str | None = None

    @field_validator("tz")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Choose a valid IANA timezone") from exc
        return value

    @field_validator("categories")
    @classmethod
    def known_categories(cls, values: list[str]) -> list[str]:
        if any(value not in BY_NAME for value in values):
            raise ValueError("Unknown email category")
        return list(dict.fromkeys(values))

    @field_validator("focus_category")
    @classmethod
    def known_focus_category(cls, value: str | None) -> str | None:
        if value is not None and value not in BY_NAME:
            raise ValueError("Unknown email category")
        return value

    @field_validator("q")
    @classmethod
    def trim_search(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def calendar_range(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("Provide both start and end dates")
        if self.start is None:
            self.end = datetime.now(ZoneInfo(self.tz)).date()
            self.start = self.end - timedelta(days=self.days - 1)
        if self.end < self.start:
            raise ValueError("End date must be on or after start date")
        if (self.end - self.start).days >= 90:
            raise ValueError("Choose a range of at most 90 calendar days")
        if self.focus_date and not self.start <= self.focus_date <= self.end:
            raise ValueError("Selected day must be inside the timeline range")
        return self


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bounds(start: date, end: date, zone: ZoneInfo) -> tuple[str, str]:
    # Convert calendar midnights separately: a DST day is not always 24 hours.
    return (
        datetime.combine(start, time.min, zone).astimezone(timezone.utc).isoformat(),
        datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(timezone.utc).isoformat(),
    )


def _scope(filters: DashboardFilters, *, focus: bool = False) -> tuple[str, list]:
    zone = ZoneInfo(filters.tz)
    lower, upper = _bounds(filters.start, filters.end, zone)
    clauses = [f"{_INSTANT} >= julianday(?)", f"{_INSTANT} < julianday(?)"]
    params: list = [lower, upper]
    if filters.account:
        clauses.append("a.label = ?")
        params.append(filters.account)
    if filters.categories:
        clauses.append(f"c.category IN ({','.join('?' for _ in filters.categories)})")
        params.extend(filters.categories)
    if filters.status != "all":
        clauses.append("m.handled_at IS NULL" if filters.status == "pending" else "m.handled_at IS NOT NULL")
    if filters.action != "all":
        clauses.append("c.action_required = ?")
        params.append(int(filters.action == "yes"))
    if filters.q:
        escaped = filters.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        fields = ("m.subject", "m.from_addr", "m.from_name", "c.company", "c.role", "c.summary")
        clauses.append("(" + " OR ".join(f"COALESCE({field}, '') LIKE ? ESCAPE '\\'" for field in fields) + ")")
        params.extend([f"%{escaped}%"] * len(fields))
    if focus:
        if filters.focus_date:
            day_start, day_end = _bounds(filters.focus_date, filters.focus_date, zone)
            clauses.extend([f"{_INSTANT} >= julianday(?)", f"{_INSTANT} < julianday(?)"])
            params.extend([day_start, day_end])
        if filters.focus_category:
            clauses.append("c.category = ?")
            params.append(filters.focus_category)
    return " WHERE " + " AND ".join(clauses), params


def dashboard_data(conn: sqlite3.Connection, filters: DashboardFilters, *, retention_days: int) -> dict:
    """Return an overview and a page of metadata. Never read message bodies."""
    # A savepoint gives every SELECT one snapshot, including with an existing
    # caller transaction, without blocking writers in the WAL database.
    conn.execute("SAVEPOINT dashboard_snapshot")
    try:
        return _dashboard_data(conn, filters, retention_days=retention_days)
    finally:
        conn.execute("RELEASE SAVEPOINT dashboard_snapshot")


def _dashboard_data(conn: sqlite3.Connection, filters: DashboardFilters, *, retention_days: int) -> dict:
    zone = ZoneInfo(filters.tz)

    def local_day(value):
        instant = _datetime(value)
        return instant.astimezone(zone).date().isoformat() if instant else None

    conn.create_function("analytics_local_day", 1, local_day, deterministic=True)
    scope, params = _scope(filters)
    attention_placeholders = ",".join("?" for _ in _ATTENTION)
    rows = conn.execute(
        f"SELECT analytics_local_day({_RECEIVED}) AS day, c.category, COUNT(*) AS n, "
        "SUM(m.handled_at IS NOT NULL) AS completed, "
        f"SUM(m.handled_at IS NULL AND c.category IN ({attention_placeholders})) AS attention, "
        "SUM(c.retryable != 0) AS retryable "
        + _FROM + scope + " GROUP BY day, c.category",
        [*_ATTENTION, *params],
    ).fetchall()
    totals = {category.name: 0 for category in CATEGORIES}
    summary = dict(total=0, pending_attention=0, completed=0, unclassified=0, retryable=0)
    per_day: dict[str, dict[str, int]] = {}
    for row in rows:
        category = row["category"] if row["category"] in BY_NAME else "unclassified"
        totals[category] += row["n"]
        counts = per_day.setdefault(row["day"], {})
        counts[category] = counts.get(category, 0) + row["n"]
        summary["total"] += row["n"]
        summary["pending_attention"] += row["attention"] or 0
        summary["completed"] += row["completed"] or 0
        summary["retryable"] += row["retryable"] or 0
    summary["unclassified"] = totals["unclassified"]

    first_run = conn.execute(
        "SELECT started_at FROM runs WHERE finished_at IS NOT NULL AND accounts_checked > 0 "
        "ORDER BY julianday(started_at), id LIMIT 1"
    ).fetchone()
    started_at = first_run["started_at"] if first_run else None
    collected = _datetime(started_at)
    collection_date = collected.astimezone(zone).date() if collected else None
    earliest = conn.execute(
        f"SELECT {_RECEIVED} AS received FROM messages m WHERE {_INSTANT} IS NOT NULL "
        f"ORDER BY {_INSTANT}, m.id LIMIT 1"
    ).fetchone()
    daily = []
    day = filters.start
    while day <= filters.end:
        counts = {category.name: per_day.get(day.isoformat(), {}).get(category.name, 0)
                  for category in CATEGORIES}
        daily.append({"date": day.isoformat(), "counts": counts, "total": sum(counts.values()),
                      "partial": collection_date is None or day < collection_date})
        day += timedelta(days=1)

    list_scope, list_params = _scope(filters, focus=True)
    total = conn.execute("SELECT COUNT(*) " + _FROM + list_scope, list_params).fetchone()[0]
    total_pages = (total + filters.page_size - 1) // filters.page_size
    page = min(filters.page, max(1, total_pages))
    direction = "DESC" if filters.sort == "newest" else "ASC"
    message_rows = conn.execute(
        f"SELECT m.id AS pk, {_RECEIVED} AS received_at, m.date_utc, m.subject, "
        "m.from_addr, m.from_name, m.handled_at, a.label AS account_label, "
        "c.company, c.role, c.category, c.summary, c.retryable, c.action_required "
        + _FROM + list_scope + f" ORDER BY {_INSTANT} {direction}, m.id {direction} LIMIT ? OFFSET ?",
        [*list_params, filters.page_size, (page - 1) * filters.page_size],
    ).fetchall()
    items = []
    for row in message_rows:
        item = dict(row)
        instant = _datetime(item["received_at"])
        item["received_at"] = instant.isoformat() if instant else None
        item["date"] = instant.astimezone(zone).date().isoformat() if instant else None
        item["date_estimated"] = not bool(item.pop("date_utc"))
        item["category_label"] = label_of(item["category"])
        item["tier"] = tier_of(item["category"])
        item["retryable"] = bool(item["retryable"])
        item["action_required"] = bool(item["action_required"])
        items.append(item)
    normalized_filters = filters.model_dump(mode="json")
    normalized_filters["page"] = page
    return {
        "ok": True, "filters": normalized_filters, "summary": summary,
        "categories": [{"name": category.name, "label": category.label,
                        "tier": category.tier, "count": totals[category.name]} for category in CATEGORIES],
        "daily": daily,
        "history": {"collection_started_at": started_at,
                    "collection_start_date": collection_date.isoformat() if collection_date else None,
                    "earliest_received_date": local_day(earliest["received"]) if earliest else None,
                    "retention_days": retention_days},
        "messages": {"items": items, "total": total, "page": page,
                     "page_size": filters.page_size, "total_pages": total_pages},
    }
