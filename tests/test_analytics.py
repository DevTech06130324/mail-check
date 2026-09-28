"""Analytics contracts: one shared scope, local calendar dates, and exact paging."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mailcheck import config, db
from mailcheck.analytics import DashboardFilters, dashboard_data
from mailcheck.models import Classification, NormalizedMessage
from mailcheck.web.app import create_app


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mail.db"
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.account = db.add_account(self.conn, label="personal", email="me@example.test")
        self.other = db.add_account(self.conn, label="work", email="work@example.test")

    def add(self, key, *, date="2026-09-15T12:00:00", category="rejection",
            account=None, done=False, retryable=False, subject="Application update",
            action=False):
        msg = NormalizedMessage(
            account_id=account or self.account, account_label="personal", message_id=str(key),
            uid=str(key), folder="INBOX", from_addr="recruiter@example.test", from_name="Recruiter",
            subject=subject, date_utc=datetime.fromisoformat(date) if date else None,
            body="BODY MUST NOT BE INCLUDED IN ANALYTICS",
        )
        pk = db.upsert_message(self.conn, msg)
        db.save_classification(self.conn, pk, Classification(
            category=category, summary="Acme application", company="Acme", role="Engineer",
            retryable=retryable, action_required=action), "qwen", "v1")
        if done:
            db.set_handled(self.conn, pk, True)
        return pk

    def data(self, **kwargs):
        return dashboard_data(self.conn, DashboardFilters(
            start="2026-09-14", end="2026-09-16", tz="UTC", **kwargs), retention_days=90)

    def test_global_counts_latest_classification_and_zero_days(self):
        pk = self.add("a", category="interview_invite", action=True)
        self.add("b", done=True)
        self.add("c", category="unclassified", retryable=True, action=True)
        # A newer model classification replaces the category for counting.
        db.save_classification(self.conn, pk, Classification(category="offer", action_required=True), "new-model", "v2")
        result = self.data()
        self.assertEqual(result["summary"], {
            "total": 3, "pending_attention": 2, "completed": 1,
            "unclassified": 1, "retryable": 1,
        })
        self.assertEqual(result["messages"]["total"], 3)
        self.assertEqual(len(result["daily"]), 3)
        self.assertEqual(result["daily"][0]["total"], 0)
        counts = result["daily"][1]["counts"]
        self.assertEqual(counts["offer"], 1)
        self.assertEqual(counts["interview_invite"], 0)
        self.assertEqual(len(counts), 10)
        self.assertNotIn("BODY MUST", str(result))

    def test_drilldown_only_changes_list_and_done_refreshes_counts(self):
        pk = self.add("a", category="offer", action=True)
        self.add("b")
        result = self.data(focus_date="2026-09-15", focus_category="offer")
        self.assertEqual(result["summary"]["total"], 2)
        self.assertEqual(result["messages"]["total"], 1)
        db.set_handled(self.conn, pk, True)
        self.assertEqual(self.data()["summary"]["pending_attention"], 0)
        self.assertEqual(self.data(status="done")["summary"]["total"], 1)

    def test_concurrent_done_uses_one_read_snapshot(self):
        pk = self.add("snapshot")
        writer = db.connect(self.path)
        self.addCleanup(writer.close)
        changed = False

        def between_queries(sql):
            nonlocal changed
            if sql.startswith("SELECT started_at") and not changed:
                changed = True
                db.set_handled(writer, pk, True)

        self.conn.set_trace_callback(between_queries)
        try:
            result = self.data(status="pending")
        finally:
            self.conn.set_trace_callback(None)
        self.assertTrue(changed)
        self.assertEqual(result["summary"]["total"], 1)
        self.assertEqual(result["messages"]["total"], 1)
        self.assertEqual(len(result["messages"]["items"]), 1)
        self.assertEqual(self.data(status="pending")["summary"]["total"], 0)

    def test_combined_filters_and_literal_search_wildcards(self):
        self.add("a", subject="100%_match", category="offer", action=True)
        self.add("b", subject="100XXmatch", category="offer", action=True)
        self.add("c", subject="100%_match", category="offer", account=self.other, action=True)
        result = self.data(account="personal", categories=["offer"], status="pending",
                           action="yes", q="%_")
        self.assertEqual(result["summary"]["total"], 1)
        self.assertEqual(result["messages"]["items"][0]["subject"], "100%_match")

    def test_timezone_dst_and_inclusive_end_date(self):
        self.add("before", date="2026-03-08T05:59:59")
        self.add("start", date="2026-03-08T06:00:00")
        self.add("end", date="2026-03-09T04:59:59+00:00")
        self.add("after", date="2026-03-09T05:00:00+00:00")
        result = dashboard_data(self.conn, DashboardFilters(
            start="2026-03-08", end="2026-03-08", tz="America/Chicago"), retention_days=90)
        self.assertEqual(result["summary"]["total"], 2)
        self.assertTrue(all(item["date"] == "2026-03-08" for item in result["messages"]["items"]))

    def test_fall_dst_repeated_hour_and_inclusive_range(self):
        self.add("before", date="2026-11-01T04:59:59+00:00")
        self.add("start", date="2026-11-01T05:00:00+00:00")
        self.add("first-hour", date="2026-11-01T06:30:00+00:00")
        self.add("second-hour", date="2026-11-01T07:30:00+00:00")
        self.add("end", date="2026-11-02T05:59:59+00:00")
        self.add("after", date="2026-11-02T06:00:00+00:00")
        result = dashboard_data(self.conn, DashboardFilters(
            start="2026-11-01", end="2026-11-01", tz="America/Chicago"), retention_days=90)
        self.assertEqual(result["summary"]["total"], 4)
        self.assertEqual(result["daily"][0]["total"], 4)
        self.assertEqual(result["messages"]["total"], 4)

    def test_missing_date_uses_fetch_time_and_history_is_partial(self):
        pk = self.add("missing", date=None)
        self.conn.execute("UPDATE messages SET fetched_at = ? WHERE id = ?", ("2026-09-15T12:00:00+00:00", pk))
        self.conn.execute("INSERT INTO runs(started_at,finished_at,accounts_checked) VALUES(?,?,1)",
                          ("2026-09-15T10:00:00+00:00", "2026-09-15T10:01:00+00:00"))
        self.conn.commit()
        result = self.data()
        self.assertTrue(result["messages"]["items"][0]["date_estimated"])
        self.assertTrue(result["daily"][0]["partial"])
        self.assertFalse(result["daily"][1]["partial"])
        self.assertEqual(result["history"]["retention_days"], 90)

    def test_pagination_beyond_500_and_stable_sort(self):
        for index in range(601):
            self.add(index)
        first = self.data()
        last = self.data(page=13)
        self.assertEqual(first["messages"]["total"], 601)
        self.assertEqual(first["messages"]["total_pages"], 13)
        self.assertEqual(len(last["messages"]["items"]), 1)
        self.assertEqual(first["messages"]["items"][0]["pk"], 601)
        self.assertEqual(last["messages"]["items"][0]["pk"], 1)
        self.assertEqual(self.data(sort="oldest")["messages"]["items"][0]["pk"], 1)

    def test_invalid_filters(self):
        for fields in (
            {"start": "2026-01-01", "end": "2026-04-01"},
            {"start": "2026-09-16", "end": "2026-09-14"},
            {"start": "2026-09-15"}, {"tz": "Invalid/Zone"},
            {"categories": ["unknown"]}, {"status": "invalid"},
            {"page": 0}, {"page_size": 101}, {"q": "x" * 201},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                DashboardFilters(**fields)

    def test_read_only_routes_and_navigation(self):
        self.add("a")
        with patch.object(config, "db_path", return_value=self.path), \
             patch.object(config, "config_path", return_value=Path(self.tmp.name) / "config.toml"), \
             patch("mailcheck.pipeline.fetch_account", side_effect=AssertionError("Must not fetch")), \
             patch("mailcheck.pipeline.LLMClient", side_effect=AssertionError("Must not infer")), \
             TestClient(create_app()) as client:
            page = client.get("/dashboard")
            self.assertEqual(page.status_code, 200)
            # The compiled React app hydrates navigation in the browser; source
            # installs without a frontend build keep rendering the legacy shell.
            self.assertTrue('id="root"' in page.text or 'href="/dashboard"' in page.text)
            response = client.get("/api/dashboard?start=2026-09-14&end=2026-09-16&tz=UTC")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["summary"]["total"], 1)
            self.assertEqual(client.get("/api/dashboard?days=91").status_code, 400)


if __name__ == "__main__":
    unittest.main()
