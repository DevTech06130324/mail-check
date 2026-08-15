"""OmniRoute client. Assumes an OpenAI-compatible /chat/completions endpoint.

The user supplies base_url, model and auth token; nothing here is provider-specific
beyond that shape.
"""

from __future__ import annotations

import json
import random
import time

import httpx


class LLMError(RuntimeError):
    pass


class RateLimited(LLMError):
    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("Rate limited")
        self.retry_after = retry_after


def _endpoint(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


class LLMClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        model: str,
        timeout: int = 120,
        max_retries: int = 4,
        temperature: float = 0.0,
        use_json_mode: bool = True,
    ) -> None:
        if not base_url:
            raise LLMError("No base_url configured. Run: mail-check init")
        if not model:
            raise LLMError("No model configured. Run: mail-check init")
        self.url = _endpoint(base_url)
        self.model = model
        # A caller passing 0 would mean "never even try the request"; config.py
        # validates against this too, but a hardcoded call site could still hit
        # it, so it is clamped here as well.
        self.max_retries = max(1, max_retries)
        self.temperature = temperature
        self.use_json_mode = use_json_mode
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def complete(
        self, system: str, user: str, *, json_mode: bool | None = None, max_tokens: int = 2048
    ) -> str:
        """``json_mode=None`` (the default) defers to ``self.use_json_mode`` —
        the configured ``llm.use_json_mode`` setting. Pass an explicit bool to
        override it for one call, as the retry ladder in classify.py does."""
        if json_mode is None:
            json_mode = self.use_json_mode
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            # Some proxies stream by default; ask for a whole body. We parse SSE
            # anyway if they ignore this.
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return self._once(payload)
            except RateLimited as exc:
                last_error = exc
                delay = exc.retry_after if exc.retry_after else _backoff(attempt)
                time.sleep(min(delay, 60))
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    # 4xx other than 429 will not fix themselves.
                    raise LLMError(_describe(exc)) from exc
                time.sleep(_backoff(attempt))
        raise LLMError(f"Gave up after {self.max_retries} attempts: {last_error}")

    def _once(self, payload: dict) -> str:
        resp = self._client.post(self.url, json=payload)
        if resp.status_code == 429:
            raise RateLimited(_retry_after(resp))
        if resp.status_code == 400 and "response_format" in resp.text:
            # Endpoint does not support JSON mode; retry once without it. The
            # parser downstream copes with prose-wrapped JSON anyway.
            payload = {k: v for k, v in payload.items() if k != "response_format"}
            resp = self._client.post(self.url, json=payload)
        resp.raise_for_status()
        body = resp.text

        # Many OpenAI-compatible proxies stream regardless of `stream: false`.
        if "text/event-stream" in resp.headers.get("content-type", "") or body.lstrip().startswith(
            "data:"
        ):
            content = _collect_sse(body)
            if not content:
                raise LLMError(f"Stream carried no content: {body[:200]}")
            return content

        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"Non-JSON response from {self.url}: {body[:200]}") from exc

        if "error" in data and not data.get("choices"):
            raise LLMError(f"API error: {data['error']}")
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected response shape: {str(data)[:300]}") from exc
        content = (choice.get("message") or choice.get("delta") or {}).get("content")
        if not content:
            raise LLMError("Model returned empty content")
        return content

    def ping(self) -> str:
        """Cheap round-trip used by `mail-check init` to validate settings."""
        return self.complete(
            "You are a health check. Reply with JSON only.",
            'Reply with exactly: {"ok": true}',
            json_mode=True,
            max_tokens=32,
        )


def _collect_sse(body: str) -> str:
    """Reassemble the text from an SSE chat-completion stream.

    Only `delta.content` is kept: reasoning models also emit `reasoning_content`
    or `thinking` deltas, which are not part of the answer and would corrupt the
    JSON we are trying to parse.
    """
    parts: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        for choice in data.get("choices") or []:
            node = choice.get("delta") or choice.get("message") or {}
            piece = node.get("content")
            if piece:
                parts.append(piece)
    return "".join(parts)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _backoff(attempt: int) -> float:
    return min(2**attempt, 30) + random.uniform(0, 1)


def _describe(exc: httpx.HTTPStatusError) -> str:
    code = exc.response.status_code
    body = exc.response.text[:300]
    if code == 401:
        return f"401 Unauthorized - check your auth token. ({body})"
    if code == 404:
        return (
            f"404 Not Found at {exc.request.url}. Check base_url; it usually ends "
            f"in /v1. ({body})"
        )
    return f"HTTP {code} from {exc.request.url}: {body}"
