"""JSON page data contracts for the React console."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mailcheck import config, db
from mailcheck.models import Classification, NormalizedMessage
from mailcheck.web import app as web_app
from mailcheck.web.app import create_app


class FrontendApiTests(unittest.TestCase):
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
        self.patches = [
            patch.object(config, "db_path", return_value=self.path),
            patch.object(config, "config_dir", return_value=self.root),
            patch.object(config, "config_path", return_value=self.root / "config.toml"),
        ]
        for active in self.patches:
            active.start()
            self.addCleanup(active.stop)
        self.client = TestClient(create_app())
        self.addCleanup(self.client.close)

    def test_spa_shell_revalidates_html_and_contains_a_boot_recovery_message(self):
        response = self.client.get("/?view=queue&days=30")

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response.headers.get("cache-control", ""))
        self.assertIn("Starting mail-check", response.text)
        self.assertIn("reload this page", response.text)

    def add_message(self, key, *, category="interview_invite", done=False):
        message = NormalizedMessage(
            account_id=self.account_id,
            account_label="personal",
            message_id=f"<{key}@example.test>",
            uid=str(key),
            folder="INBOX",
            from_addr="recruiter@example.test",
            from_name="Recruiter",
            subject=f"Interview at Acme {key}",
            date_utc=datetime(2026, 9, 15, 12),
            body="Hello <script>alert(1)</script> applicant.\n\nChoose a time.",
        )
        pk = db.upsert_message(self.conn, message)
        db.save_classification(
            self.conn,
            pk,
            Classification(
                category=category,
                summary="Choose a time for your interview.",
                company="Acme",
                role="Engineer",
                action_required=True,
            ),
            "model",
            "v1",
        )
        if done:
            db.set_handled(self.conn, pk, True)
        return pk

    def test_bootstrap_exposes_readiness_navigation_and_taxonomy(self):
        self.add_message("one")

        response = self.client.get("/api/bootstrap")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("readiness", payload)
        self.assertEqual(payload["counts"]["actionable"], 1)
        self.assertTrue(payload["taxonomy"])
        self.assertTrue(payload["presets"])

    def test_triage_filters_views_and_keeps_message_bodies_out_of_rows(self):
        active = self.add_message("active")
        self.add_message("finished", done=True)

        queue = self.client.get("/api/triage?view=queue")
        completed = self.client.get("/api/triage?view=completed")

        self.assertEqual(queue.status_code, 200)
        self.assertEqual([row["pk"] for group in queue.json()["groups"] for row in group["items"]], [active])
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["view"], "completed")
        serialized_rows = str(queue.json()["groups"])
        self.assertNotIn("body_text", serialized_rows)
        self.assertNotIn("Choose a time.\\n", serialized_rows)

    def test_message_reader_returns_sanitized_body_and_404_for_missing(self):
        pk = self.add_message("one")

        response = self.client.get(f"/api/messages/{pk}")

        self.assertEqual(response.status_code, 200)
        body_html = response.json()["body_html"]
        self.assertIn("&lt;script&gt;", body_html)
        self.assertNotIn("<script>", body_html)
        self.assertEqual(self.client.get("/api/messages/999999").status_code, 404)

    def test_account_api_never_returns_credentials(self):
        response = self.client.get("/api/accounts")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accounts"][0]["label"], "personal")
        self.assertNotIn("password", str(response.json()).lower())

    def test_settings_api_provides_editable_state_without_secrets(self):
        response = self.client.get("/api/settings")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("llm", payload["settings"])
        self.assertIn("rules", payload)
        self.assertNotIn("client_secret", str(payload).lower())

    def test_status_completion_id_changes_after_a_job(self):
        first = self.client.get("/api/status").json()
        with patch("mailcheck.web.app._job", {"running": False, "message": "Done", "detail": "",
                                                "at": "12:00", "ok": True, "stage_started": None,
                                                "completion_id": 7}):
            second = self.client.get("/api/status").json()

        self.assertIn("completion_id", first)
        self.assertEqual(second["completion_id"], 7)

    def test_built_react_app_owns_existing_routes_and_deep_links(self):
        build = self.root / "frontend_dist"
        build.mkdir()
        (build / "index.html").write_text("<div id='root'>React app</div>", encoding="utf-8")
        with patch.object(web_app, "FRONTEND_DIST", build):
            client = TestClient(create_app())
            self.addCleanup(client.close)
            for path in ("/", "/dashboard", "/accounts", "/settings", "/future/page"):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("React app", response.text)


if __name__ == "__main__":
    unittest.main()
