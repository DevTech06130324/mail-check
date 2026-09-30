"""Regression tests for the codebase-review fixes."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from mailcheck import config, db
from mailcheck.models import Classification, NormalizedMessage
from mailcheck.sources.imap import _to_raw
from mailcheck.web.app import _open_link, create_app


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "mail.db"
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.account_id = db.add_account(
            self.conn, label="personal", email="me@example.test", imap_host="imap.example.test"
        )
        for active in (
            patch.object(config, "db_path", return_value=self.path),
            patch.object(config, "config_dir", return_value=self.root),
            patch.object(config, "config_path", return_value=self.root / "config.toml"),
        ):
            active.start()
            self.addCleanup(active.stop)
        self.client = TestClient(create_app())
        self.addCleanup(self.client.close)

    def add(self, key, *, date_utc, category="interview_invite"):
        msg = NormalizedMessage(
            account_id=self.account_id, account_label="personal",
            message_id=f"<{key}@example.test>", uid=str(key), folder="INBOX",
            from_addr="a@example.test", from_name="A", subject=f"Subject {key}",
            date_utc=date_utc, body="Body", provider_url=None,
        )
        pk = db.upsert_message(self.conn, msg)
        db.save_classification(self.conn, pk, Classification(category=category), "m", "1")
        return pk

    # -- Host header (DNS rebinding) -------------------------------------------

    def test_foreign_host_header_is_rejected(self):
        self.assertEqual(self.client.get("/api/bootstrap", headers={"host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/bootstrap", headers={"host": "127.0.0.1:8765"}).status_code, 200)
        self.assertEqual(self.client.get("/api/bootstrap", headers={"host": "[::1]:8765"}).status_code, 200)

    # -- undated mail ----------------------------------------------------------

    def test_unparseable_imap_date_becomes_none(self):
        msg = SimpleNamespace(
            headers={}, uid="7", from_values=None, from_="a@b.test",
            date=datetime(1900, 1, 1), subject="s", text="t", html="",
        )
        self.assertIsNone(_to_raw(msg, "INBOX").date_utc)

    def test_undated_mail_is_visible_and_survives_pruning(self):
        pk = self.add("undated", date_utc=None)
        counts = db.queue_counts(self.conn, since_iso=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
        self.assertEqual(counts["actionable"], 1)
        rows = db.query_triaged(self.conn, since_iso=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
        self.assertEqual([r["pk"] for r in rows], [pk])
        self.assertEqual(db.prune_messages(self.conn, before_iso=(datetime.now(timezone.utc) - timedelta(days=7)).isoformat()), 0)

    def test_migration_clears_stored_1900_dates(self):
        pk = self.add("old", date_utc=datetime(1900, 1, 1))
        self.conn.execute("PRAGMA user_version = 0")
        self.conn.commit()
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT date_utc FROM messages WHERE id = ?", (pk,)).fetchone()[0])

    # -- input bounds and validation -------------------------------------------

    def test_day_counts_are_bounded(self):
        for url, body in (("/api/check", {"since_days": 10**9}), ("/api/check", {"since_days": -1}),
                          ("/api/reclassify", {"days": 10**9})):
            self.assertEqual(self.client.post(url, json=body).status_code, 422, (url, body))
        self.assertEqual(self.client.get("/?days=1000000000").status_code, 422)

    def test_rule_category_must_exist(self):
        bad = self.client.post("/api/rules", json={"category": "typo_cat", "sender": "x@y.test"})
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post("/api/rules", json={"category": "job_alert", "sender": "x@y.test"})
        self.assertEqual(ok.status_code, 200)

    def test_unknown_api_path_is_json_404(self):
        response = self.client.get("/api/nope")
        self.assertEqual(response.status_code, 404)
        self.assertIn("json", response.headers["content-type"])

    def test_malformed_config_gets_recovery_page(self):
        (self.root / "config.toml").write_text("this is [not toml", encoding="utf-8")
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 503)
        self.assertIn("not valid TOML", response.text)

    # -- links -----------------------------------------------------------------

    def test_gmail_link_names_the_account(self):
        url, label = _open_link({
            "provider_url": None, "provider": "imap", "imap_host": "imap.gmail.com",
            "message_id": "<a@b>", "account_email": "me+x@gmail.com",
        })
        self.assertIn("authuser=me%2Bx%40gmail.com", url)
        self.assertEqual(label, "Open in Gmail")


if __name__ == "__main__":
    unittest.main()
