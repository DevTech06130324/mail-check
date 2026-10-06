"""Native Ollama contracts; no model or credentials required."""
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

from mailcheck.config import LLMConfig
from mailcheck.llm.client import LLMClient, LLMError, OllamaBusy


class OllamaTests(unittest.TestCase):
    def client(self, handler, **kwargs):
        client = LLMClient(base_url="http://ollama:11440/", model="qwen", **kwargs)
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_native_contract(self):
        requests = []
        def reply(request):
            requests.append(request)
            return httpx.Response(200, json={"message": {"content": '{"ok":true}',
                "thinking": "ignore me"}, "done": True, "done_reason": "length"})
        c = self.client(reply, num_ctx=16384, think=False, keep_alive="5m")
        result = c.complete("system", "user", max_tokens=123)
        self.assertEqual(str(requests[0].url), "http://ollama:11440/api/chat")
        self.assertNotIn("authorization", requests[0].headers)
        body = json.loads(requests[0].content)
        self.assertEqual(body["options"], {"temperature": 0.0, "num_ctx": 16384, "num_predict": 123})
        self.assertEqual(body["format"], "json")
        self.assertFalse(body["think"])
        self.assertFalse(body["stream"])
        self.assertEqual(body["keep_alive"], "5m")
        self.assertEqual(result, '{"ok":true}')
        self.assertEqual(result.finish_reason, "length")
        c.complete("s", "u", json_mode=False)
        self.assertNotIn("format", json.loads(requests[-1].content))

    def test_config_validation(self):
        self.assertEqual(LLMConfig(base_url=" http://ollama:11440/ ").base_url, "http://ollama:11440")
        self.assertEqual(LLMConfig(timeout_seconds=300).timeout_seconds, 60)
        self.assertEqual(LLMConfig(classification_deadline_seconds=1800).classification_deadline_seconds, 300)
        for settings in ({"base_url":"http://old/v1"}, {"num_ctx":0}, {"keep_alive":"forever"}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                LLMConfig(**settings)

    def test_bad_responses(self):
        for payload in ([], {}, {"error":"model missing"}, {"message":{"content":""}},
                        {"message":{"content":12}}, {"message":{"content":"hi"}, "done":False}):
            with self.subTest(payload=payload):
                c = self.client(lambda r: httpx.Response(200, json=payload))
                with self.assertRaises(LLMError):
                    c.complete("s", "u")
        c = self.client(lambda r: httpx.Response(200, text="not JSON"))
        with self.assertRaises(LLMError):
            c.complete("s", "u")

    @patch("mailcheck.llm.client.time.sleep")
    def test_retry_and_errors(self, sleep):
        for status, count in ((404,1), (400,1), (429,1), (503,1)):
            calls = []
            def reply(r):
                calls.append(r)
                return httpx.Response(status, json={"error":"model unavailable"})
            c = self.client(reply, max_retries=3)
            with self.assertRaises(LLMError):
                c.complete("s", "u")
            self.assertEqual(len(calls), count)
        calls = []
        def timeout(r):
            calls.append(r)
            raise httpx.ReadTimeout("timed out", request=r)
        with self.assertRaises(LLMError):
            self.client(timeout, max_retries=2).complete("s", "u")
        self.assertEqual(len(calls), 1)

    def test_ping_checks_health_json(self):
        for content in ('{"ok":false}', '{}', 'not json', '{"ok":1}'):
            c = self.client(lambda r: httpx.Response(200, json={"message":{"content":content}, "done":True}))
            with self.assertRaises(LLMError):
                c.ping()
        def reply(r):
            self.assertEqual(json.loads(r.content)["options"]["num_predict"], 256)
            return httpx.Response(200, json={"message":{"content":'{"ok":true}'}, "done":True})
        self.assertEqual(self.client(reply).ping(), '{"ok":true}')

    def test_factory_uses_all_config(self):
        cfg = LLMConfig(base_url="http://ollama", num_ctx=32768,
            think=True, keep_alive="10m", timeout_seconds=60, temperature=0.3, use_json_mode=False)
        with LLMClient.from_config(cfg) as c:
            self.assertEqual(c.num_ctx, 32768)
            self.assertTrue(c.think)
            self.assertEqual(c.keep_alive, "10m")
            self.assertEqual(c._client.timeout.read, 60)
            self.assertFalse(c.use_json_mode)

    def test_cli_and_web_without_keyring(self):
        from fastapi.testclient import TestClient
        from typer.testing import CliRunner
        from mailcheck import config as cfgmod
        from mailcheck.cli import app, _require_llm
        from mailcheck.web.app import create_app
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(cfgmod, "config_path", return_value=Path(tmp)/"config.toml"), \
             patch.object(cfgmod, "db_path", return_value=Path(tmp)/"mail.db"), \
             patch("keyring.get_password", side_effect=RuntimeError("keyring unavailable")), \
             patch("keyring.set_password", side_effect=RuntimeError("keyring unavailable")):
            cfg = cfgmod.Config(llm=LLMConfig(base_url="http://ollama"), privacy_ack=True)
            cfgmod.save(cfg)
            _require_llm(cfg)
            result = CliRunner().invoke(app, ["init", "--base-url", "http://ollama/", "--no-test"])
            self.assertEqual(result.exit_code, 0, result.output)
            with TestClient(create_app()) as web:
                self.assertNotIn("Connect a model to start", web.get("/").text)
                page = web.get("/settings")
                self.assertEqual(page.status_code, 200)
                self.assertNotIn('id="s-token"', page.text)
                saved = web.post("/api/settings", json={"num_ctx":32768,"think":True,
                    "keep_alive":"10m","timeout_seconds":60,
                    "classification_deadline_seconds":300}).json()
                self.assertTrue(saved["ok"], saved)
                self.assertEqual(cfgmod.load().llm.num_ctx, 32768)
                self.assertTrue(cfgmod.load().llm.think)
                self.assertEqual(cfgmod.load().llm.keep_alive, "10m")
                self.assertEqual(cfgmod.load().llm.timeout_seconds, 60)
                bad = web.post("/api/settings", json={"base_url":"http://old/v1"}).json()
                self.assertFalse(bad["ok"])
                self.assertEqual(cfgmod.load().llm.base_url, "http://ollama")

    @patch("mailcheck.llm.client.time.sleep")
    def test_transient_error_recovers(self, sleep):
        calls = []
        def reply(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(502, json={"error":"temporary gateway error"})
            return httpx.Response(200, json={"message":{"content":"{}"}, "done":True})
        self.assertEqual(self.client(reply).complete("s", "u"), "{}")
        self.assertEqual(len(calls), 2)

    def test_busy_error_is_distinct(self):
        c = self.client(lambda r: httpx.Response(503, json={"error":"busy"}))
        with self.assertRaises(OllamaBusy):
            c.complete("s", "u")

    def test_init_migrates_legacy_config_without_losing_settings(self):
        from typer.testing import CliRunner
        from mailcheck import config as cfgmod
        from mailcheck.cli import app
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(cfgmod, "config_path", return_value=Path(tmp)/"config.toml"):
            path = cfgmod.config_path()
            original = 'privacy_ack = true\n[llm]\nbase_url = "http://old/v1"\nmodel = "old"\n[watch]\nauto_check = false\ninterval_minutes = 23\n[check]\nretain_days = 19\n'
            path.write_text(original, encoding="utf-8")
            result = CliRunner().invoke(app, ["init", "--base-url", "http://ollama", "--no-test"])
            self.assertEqual(result.exit_code, 0, result.output)
            cfg = cfgmod.load()
            self.assertEqual(cfg.llm.base_url, "http://ollama")
            self.assertEqual(cfg.check.retain_days, 19)
            self.assertEqual(cfg.watch.interval_minutes, 23)
            self.assertNotIn("model", path.read_text(encoding="utf-8"))
            backups = list(path.parent.glob("config.toml.*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
