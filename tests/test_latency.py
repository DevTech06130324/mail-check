from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from mailcheck import db
from mailcheck.diagnostics import PerformanceLog
from mailcheck.llm.classify import classify
from mailcheck.llm.client import Completion, LLMClient, OllamaBusy
from mailcheck.models import Classification, NormalizedMessage


def _message(index: int) -> NormalizedMessage:
    return NormalizedMessage(
        account_id=1,
        account_label="mail",
        uid=str(index),
        folder="INBOX",
        message_id=f"message-{index}",
        from_addr="sender@example.com",
        from_name="Sender",
        subject=f"Subject {index}",
        date_utc=None,
        body="A short synthetic email body.",
    )


class OllamaLatencyTests(unittest.TestCase):
    def _client(self, handler, *, max_retries: int = 4) -> LLMClient:
        return LLMClient(
            base_url="http://ollama.test:11434",
            model="qwen3.5:35b-a3b",
            timeout_seconds=60,
            max_retries=max_retries,
            transport=httpx.MockTransport(handler),
        )

    def test_read_timeout_opens_circuit_without_retry(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("still generating", request=request)

        with self._client(handler) as client:
            with self.assertRaises(OllamaBusy) as raised:
                client.complete("system", "user")

        self.assertEqual(calls, 1)
        self.assertEqual(raised.exception.category, "timeout")
        self.assertEqual(raised.exception.attempts, 1)

    def test_503_opens_circuit_without_retry(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503, json={"error": "server busy"})

        with self._client(handler) as client:
            with self.assertRaises(OllamaBusy) as raised:
                client.complete("system", "user")

        self.assertEqual(calls, 1)
        self.assertEqual(raised.exception.category, "busy")

    def test_retryable_500_retries_only_once_and_extracts_metrics(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(500, json={"error": "temporary"})
            return httpx.Response(
                200,
                json={
                    "message": {"content": "{}"},
                    "done": True,
                    "done_reason": "stop",
                    "total_duration": 7_000_000_000,
                    "load_duration": 100_000_000,
                    "prompt_eval_count": 1376,
                    "prompt_eval_duration": 500_000_000,
                    "eval_count": 292,
                    "eval_duration": 6_200_000_000,
                },
            )

        with self._client(handler) as client:
            completion = client.complete("system", "user")

        self.assertEqual(calls, 2)
        self.assertEqual(completion.attempts, 2)
        self.assertEqual(completion.metrics.prompt_tokens, 1376)
        self.assertEqual(completion.metrics.output_tokens, 292)
        self.assertAlmostEqual(completion.metrics.total_seconds, 7.0)
        self.assertAlmostEqual(completion.metrics.load_seconds, 0.1)

    def test_deadline_expiry_does_not_submit_request(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"message": {"content": "{}"}})

        with self._client(handler) as client:
            with self.assertRaises(OllamaBusy) as raised:
                client.complete("system", "user", deadline=time.monotonic() - 0.01)

        self.assertEqual(calls, 0)
        self.assertEqual(raised.exception.category, "deadline")


class ClassificationCircuitTests(unittest.TestCase):
    def test_busy_batch_defers_current_and_remaining_mail_without_fallback(self) -> None:
        events: list[dict] = []

        class BusyClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, *_args, **_kwargs):
                self.calls += 1
                raise OllamaBusy("Ollama timed out", category="timeout", attempts=1)

        client = BusyClient()
        results, errors = classify(
            client,
            [_message(i) for i in range(1, 13)],
            batch_size=5,
            classification_deadline_seconds=300,
            event=events.append,
        )

        self.assertEqual(client.calls, 1)
        self.assertEqual(len(results), 12)
        self.assertTrue(all(item.category == "unclassified" for item in results))
        self.assertTrue(all(item.retryable for item in results))
        self.assertTrue(any("retry next check" in error.lower() for error in errors))
        self.assertEqual([e["outcome"] for e in events if e["type"] == "llm_request"], ["timeout"])

    def test_output_budgets_are_bounded(self) -> None:
        calls: list[int] = []

        class RecordingClient:
            def complete(self, _system, _user, *, max_tokens, **_kwargs):
                calls.append(max_tokens)
                return Completion('{"results": []}', "stop")

        messages = [_message(i) for i in range(1, 4)]
        classify(RecordingClient(), messages, batch_size=3)

        self.assertEqual(calls[0], 512 + 128 * 3)
        self.assertEqual(calls[1:], [512, 1024] * 3)

    def test_deadline_preserves_completed_batches_and_defers_the_rest(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            time.sleep(0.04)
            results = [{"id": str(i), "category": "rejection"} for i in range(5)]
            return httpx.Response(
                200,
                json={"message": {"content": json.dumps({"results": results})}, "done": True},
            )

        started = time.monotonic()
        with self._client_for_classification(handler) as client:
            results, errors = classify(
                client,
                [_message(i) for i in range(12)],
                batch_size=5,
                classification_deadline_seconds=0.02,
            )

        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(calls, 1)
        self.assertTrue(all(not item.retryable for item in results[:5]))
        self.assertTrue(all(item.retryable for item in results[5:]))
        self.assertTrue(any("deadline" in error.lower() for error in errors))

    def test_exhausted_server_retry_defers_without_individual_fallback(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500, json={"error": "temporary server failure"})

        with self._client_for_classification(handler) as client:
            with patch("mailcheck.llm.client.time.sleep"):
                results, errors = classify(client, [_message(1), _message(2)], batch_size=2)

        self.assertEqual(calls, 2)
        self.assertTrue(all(item.retryable for item in results))
        self.assertTrue(any("retry next check" in error.lower() for error in errors))

    @staticmethod
    def _client_for_classification(handler) -> LLMClient:
        return LLMClient(
            base_url="http://ollama.test:11434",
            model="qwen3.5:35b-a3b",
            timeout_seconds=60,
            transport=httpx.MockTransport(handler),
        )


class PersistenceAndDiagnosticsTests(unittest.TestCase):
    def test_retryable_result_is_visible_but_not_a_cache_hit_and_success_replaces_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "mailcheck.db")
            account_id = db.add_account(
                conn,
                label="mail",
                email="user@example.com",
                imap_host="imap.example.com",
                imap_port=993,
                use_ssl=True,
            )
            message = _message(1)
            message.account_id = account_id
            db.upsert_message(conn, message)

            deferred = Classification(category="unclassified", summary="Ollama busy", retryable=True)
            message_pk = db.upsert_message(conn, message)
            db.save_classification(conn, message_pk, deferred, "model", "v1")
            self.assertIsNone(db.get_cached(conn, message_pk, "model", "v1"))
            stored = conn.execute("SELECT retryable FROM classifications").fetchone()
            self.assertEqual(stored[0], 1)

            success = Classification(category="interview", summary="Interview invitation")
            db.save_classification(conn, message_pk, success, "model", "v1")
            cached = db.get_cached(conn, message_pk, "model", "v1")
            self.assertEqual(cached.category, "interview")
            self.assertFalse(cached.retryable)
            conn.close()

    def test_run_aggregates_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "mailcheck.db")
            run_id = db.start_run(conn)
            db.finish_run(
                conn,
                run_id,
                accounts_checked=1,
                fetched=12,
                classified=7,
                from_cache=2,
                errors=[],
                fetch_seconds=1.25,
                classification_seconds=6.5,
                llm_requests=2,
                llm_timeouts=1,
                llm_busy_responses=0,
                llm_retries=1,
            )
            row = conn.execute(
                "SELECT fetch_seconds, classification_seconds, llm_requests, llm_timeouts, "
                "llm_busy_responses, llm_retries FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            self.assertEqual(tuple(row), (1.25, 6.5, 2, 1, 0, 1))
            conn.close()

    def test_performance_log_redacts_unknown_fields_and_rotates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "performance.jsonl"
            log = PerformanceLog(path, max_bytes=220, backup_count=3)
            for index in range(20):
                log.write(
                    {
                        "event": "llm_request",
                        "outcome": "success",
                        "batch_size": 5,
                        "wall_seconds": index / 10,
                        "subject": "SECRET SUBJECT",
                        "prompt": "SECRET BODY",
                        "model_output": "SECRET OUTPUT",
                    }
                )

            files = list(Path(tmp).glob("performance.jsonl*"))
            self.assertGreater(len(files), 1)
            self.assertLessEqual(len(files), 4)
            combined = "".join(file.read_text(encoding="utf-8") for file in files)
            self.assertNotIn("SECRET", combined)
            for line in combined.splitlines():
                json.loads(line)


if __name__ == "__main__":
    unittest.main()
