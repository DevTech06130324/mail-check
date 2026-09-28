"""Native Ollama chat client with bounded, queue-safe retries."""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass

import httpx

from ..config import LLMConfig, ollama_root


class LLMError(RuntimeError):
    def __init__(self, message: str, *, category: str = "error", attempts: int = 0,
                 wall_seconds: float = 0.0) -> None:
        super().__init__(message)
        self.category = category
        self.attempts = attempts
        self.wall_seconds = wall_seconds


class OllamaBusy(LLMError):
    """The run must stop because another request may still be queued/running."""


@dataclass(frozen=True)
class OllamaMetrics:
    total_seconds: float | None = None
    load_seconds: float | None = None
    prompt_tokens: int | None = None
    prompt_seconds: float | None = None
    output_tokens: int | None = None
    output_seconds: float | None = None


class Completion(str):
    """Reply text plus Ollama timing data and client-side request metadata."""

    def __new__(cls, text: str, finish_reason: str | None = None, *,
                metrics: OllamaMetrics | None = None, attempts: int = 1,
                wall_seconds: float = 0.0) -> "Completion":
        obj = super().__new__(cls, text)
        obj.finish_reason = finish_reason
        obj.metrics = metrics or OllamaMetrics()
        obj.attempts = attempts
        obj.wall_seconds = wall_seconds
        return obj


def _endpoint(base_url: str) -> str:
    try:
        base = ollama_root(base_url)
    except ValueError as exc:
        raise LLMError(str(exc), category="configuration") from exc
    if not base:
        raise LLMError("No Ollama base_url configured. Run: mail-check init", category="configuration")
    return f"{base}/api/chat"


class LLMClient:
    def __init__(self, *, base_url: str, model: str, timeout: int | None = None,
                 timeout_seconds: int | None = None, max_retries: int = 2,
                 temperature: float = 0.0, use_json_mode: bool = True,
                 num_ctx: int = 8192, think: bool = False, keep_alive: str = "5m",
                 transport: httpx.BaseTransport | None = None) -> None:
        if not base_url:
            raise LLMError("No base_url configured. Run: mail-check init", category="configuration")
        if not model:
            raise LLMError("No model configured. Run: mail-check init", category="configuration")
        self.url = _endpoint(base_url)
        self.model = model
        # This value is attempts, despite the historic name. Never allow more
        # than one retry: more requests amplify an already-busy Ollama queue.
        self.max_retries = max(1, min(max_retries, 2))
        self.temperature = temperature
        self.use_json_mode = use_json_mode
        self.num_ctx = num_ctx
        self.think = think
        self.keep_alive = keep_alive
        self.timeout_seconds = min(
            60.0,
            float(timeout_seconds if timeout_seconds is not None else (timeout or 60)),
        )
        self._client = httpx.Client(timeout=self.timeout_seconds, trust_env=False, transport=transport)

    @classmethod
    def from_config(cls, cfg: LLMConfig) -> "LLMClient":
        return cls(base_url=cfg.base_url, model=cfg.model,
                   timeout_seconds=cfg.timeout_seconds, max_retries=cfg.max_retries,
                   temperature=cfg.temperature, use_json_mode=cfg.use_json_mode,
                   num_ctx=cfg.num_ctx, think=cfg.think, keep_alive=cfg.keep_alive)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def complete(self, system: str, user: str, *, json_mode: bool | None = None,
                 max_tokens: int = 2048, deadline: float | None = None) -> Completion:
        if json_mode is None:
            json_mode = self.use_json_mode
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx,
                        "num_predict": max_tokens},
            "think": self.think,
            "keep_alive": self.keep_alive,
            "stream": False,
        }
        if json_mode:
            payload["format"] = "json"

        started = time.monotonic()
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            remaining = _remaining(deadline)
            if remaining is not None and remaining <= 0:
                raise OllamaBusy(
                    "Classification deadline reached before an Ollama request could start",
                    category="deadline", attempts=attempt - 1,
                    wall_seconds=time.monotonic() - started)
            request_timeout = min(self.timeout_seconds, remaining) if remaining is not None else self.timeout_seconds
            try:
                completion = self._once(payload, timeout=max(0.001, request_timeout))
                completion.attempts = attempt
                completion.wall_seconds = time.monotonic() - started
                return completion
            except httpx.ReadTimeout as exc:
                raise OllamaBusy(
                    "Ollama response timed out; the server may still be processing it",
                    category="timeout", attempts=attempt,
                    wall_seconds=time.monotonic() - started) from exc
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code in (429, 503):
                    raise OllamaBusy(
                        f"Ollama is busy (HTTP {code}); remaining mail will retry next check",
                        category="busy", attempts=attempt,
                        wall_seconds=time.monotonic() - started) from exc
                if code not in (500, 502, 504):
                    raise LLMError(_describe(exc), category="http", attempts=attempt,
                                   wall_seconds=time.monotonic() - started) from exc
                last_error = exc
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
            except httpx.TransportError as exc:
                raise LLMError(f"Ollama transport error: {exc}", category="transport",
                               attempts=attempt, wall_seconds=time.monotonic() - started) from exc
            except LLMError as exc:
                exc.attempts = attempt
                exc.wall_seconds = time.monotonic() - started
                raise

            if attempt >= self.max_retries:
                break
            delay = _backoff(attempt - 1)
            remaining = _remaining(deadline)
            if remaining is not None and remaining <= delay:
                raise OllamaBusy("Classification deadline reached before the Ollama retry",
                                 category="deadline", attempts=attempt,
                                 wall_seconds=time.monotonic() - started) from last_error
            time.sleep(delay)

        raise LLMError(
            f"Gave up after {self.max_retries} attempts: {last_error}",
            category="server" if isinstance(last_error, httpx.HTTPStatusError) else "connect",
            attempts=self.max_retries, wall_seconds=time.monotonic() - started)

    def _once(self, payload: dict, *, timeout: float) -> Completion:
        resp = self._client.post(self.url, json=payload, timeout=timeout)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"Non-JSON response from Ollama at {self.url}", category="response") from exc
        if not isinstance(data, dict):
            raise LLMError("Unexpected Ollama response: expected an object", category="response")
        if data.get("error"):
            raise LLMError(f"Ollama error: {str(data['error'])[:300]}", category="response")
        message = data.get("message")
        if not isinstance(message, dict) or data.get("done") is not True:
            raise LLMError("Unexpected or incomplete Ollama response", category="response")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMError("Ollama returned empty or invalid content", category="response")
        return Completion(content, data.get("done_reason"), metrics=OllamaMetrics(
            total_seconds=_ns_seconds(data.get("total_duration")),
            load_seconds=_ns_seconds(data.get("load_duration")),
            prompt_tokens=_int_or_none(data.get("prompt_eval_count")),
            prompt_seconds=_ns_seconds(data.get("prompt_eval_duration")),
            output_tokens=_int_or_none(data.get("eval_count")),
            output_seconds=_ns_seconds(data.get("eval_duration"))))

    def ping(self) -> Completion:
        reply = self.complete("You are a health check. Reply with JSON only.",
                              'Reply with exactly: {"ok": true}',
                              json_mode=True, max_tokens=256)
        try:
            health = json.loads(reply)
        except ValueError as exc:
            raise LLMError("Ollama health check did not return valid JSON", category="response") from exc
        if not isinstance(health, dict) or health.get("ok") is not True:
            raise LLMError("Ollama health check did not return ok: true", category="response")
        return reply


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.monotonic()


def _ns_seconds(value: object) -> float | None:
    return float(value) / 1_000_000_000 if isinstance(value, (int, float)) else None


def _int_or_none(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _backoff(attempt: int) -> float:
    return min(2**attempt, 30) + random.uniform(0, 1)


def _describe(exc: httpx.HTTPStatusError) -> str:
    code = exc.response.status_code
    body = exc.response.text[:300]
    if code == 401:
        return f"401 Unauthorized - this integration expects a token-free Ollama server. ({body})"
    if code == 404:
        return (f"404 Not Found at {exc.request.url}. Check the Ollama server root and "
                f"that the selected model is installed. ({body})")
    return f"HTTP {code} from {exc.request.url}: {body}"
