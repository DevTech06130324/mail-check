"""Invalid saved settings expose recovery instructions without leaking values."""
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mailcheck import config
from mailcheck.web.app import create_app


class WebConfigRecoveryTests(unittest.TestCase):
    def test_legacy_config_returns_safe_recovery_message(self):
        old_config = {
            "llm": {"base_url": "http://private-user:private-secret@old-router/v1",
                    "model": "old-model"},
        }
        with patch.object(config, "_read_toml", return_value=old_config), \
             patch.object(config, "config_path", return_value=Path("example-config.toml")), \
             patch("mailcheck.web.app._scheduler_started.is_set", return_value=True):
            with TestClient(create_app()) as client:
                for path in ("/api/settings", "/api/status"):
                    with self.subTest(path=path):
                        response = client.get(path)
                        self.assertEqual(response.status_code, 503)
                        self.assertIn("example-config.toml", response.text)
                        self.assertIn("mail-check init --base-url", response.text)
                        self.assertNotIn("private-secret", response.text)
                        self.assertNotIn("old-router", response.text)
                        self.assertNotIn("old-model", response.text)
                # A compiled SPA serves its shell at /settings and reads the
                # safe recovery response from the JSON endpoint above.
                self.assertIn(client.get("/settings").status_code, (200, 503))


if __name__ == "__main__":
    unittest.main()
