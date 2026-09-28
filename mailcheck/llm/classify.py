"""Batch classification with a run deadline and a busy-server circuit breaker."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from ..models import Classification, NormalizedMessage
from ..taxonomy import UNCLASSIFIED
from .client import LLMClient, LLMError, OllamaBusy
from .prompt import STRICT_SUFFIX, SYSTEM, build_user_message
from .schema import ItemResult, parse_results

ProgressFn = Callable[[int, int], None]
EventFn = Callable[[dict], None]

_BATCH_TOKENS_BASE = 512
_BATCH_TOKENS_PER_ITEM = 128
_SINGLE_TOKENS = (512, 1024)
_TRANSIENT_CATEGORIES = {"connect", "server", "transport"}


@dataclass
class _BusyBatch(Exception):
    error: OllamaBusy
    partial: dict[str, Classification]


def _to_classification(item: ItemResult) -> Classification:
    return Classification(category=item.category, confidence=item.confidence,
                          company=item.company, role=item.role, deadline=item.deadline,
                          action_required=item.action_required, summary=item.summary,
                          source="llm")


def _unclassified(reason: str, *, retryable: bool = False) -> Classification:
    return Classification(category=UNCLASSIFIED, confidence=0.0, action_required=True,
                          summary=f"Could not classify automatically ({reason}).",
                          source="llm", retryable=retryable)


def _truncated(raw: object) -> bool:
    return getattr(raw, "finish_reason", None) == "length"


def _transient_as_busy(exc: LLMError) -> OllamaBusy:
    return OllamaBusy(str(exc), category=exc.category, attempts=exc.attempts,
                      wall_seconds=exc.wall_seconds)


def _emit_request(event: EventFn | None, *, outcome: str, batch_size: int,
                  batch_index: int, total_batches: int, completed: int,
                  raw: object | None = None, error: LLMError | None = None) -> None:
    if not event:
        return
    metrics = getattr(raw, "metrics", None)
    event({
        "type": "llm_request",
        "outcome": outcome,
        "batch_size": batch_size,
        "batch_index": batch_index,
        "total_batches": total_batches,
        "completed": completed,
        "wall_seconds": getattr(raw, "wall_seconds", getattr(error, "wall_seconds", 0.0)),
        "attempts": getattr(raw, "attempts", getattr(error, "attempts", 1)),
        "retries": max(0, getattr(raw, "attempts", getattr(error, "attempts", 1)) - 1),
        "finish_reason": getattr(raw, "finish_reason", None),
        "total_seconds": getattr(metrics, "total_seconds", None),
        "load_seconds": getattr(metrics, "load_seconds", None),
        "prompt_tokens": getattr(metrics, "prompt_tokens", None),
        "prompt_seconds": getattr(metrics, "prompt_seconds", None),
        "output_tokens": getattr(metrics, "output_tokens", None),
        "output_seconds": getattr(metrics, "output_seconds", None),
    })


def _complete(client: LLMClient, system: str, user: str, *, json_mode: bool | None,
              max_tokens: int, deadline: float, event: EventFn | None,
              batch_size: int, batch_index: int, total_batches: int,
              completed: int):
    if event:
        event({"type": "llm_request_started", "batch_size": batch_size,
               "batch_index": batch_index, "total_batches": total_batches,
               "completed": completed, "started_monotonic": time.monotonic()})
    try:
        raw = client.complete(system, user, json_mode=json_mode, max_tokens=max_tokens,
                              deadline=deadline)
    except LLMError as exc:
        _emit_request(event, outcome=exc.category, batch_size=batch_size,
                      batch_index=batch_index, total_batches=total_batches,
                      completed=completed, error=exc)
        raise
    _emit_request(event, outcome="success", batch_size=batch_size,
                  batch_index=batch_index, total_batches=total_batches,
                  completed=completed, raw=raw)
    return raw


def _classify_one(client: LLMClient, msg: NormalizedMessage, errors: list[str], *,
                  deadline: float, event: EventFn | None, batch_index: int,
                  total_batches: int, completed: int) -> Classification:
    cut_short = False
    for attempt, budget in enumerate(_SINGLE_TOKENS):
        system = SYSTEM + (STRICT_SUFFIX if attempt else "")
        raw = None
        try:
            raw = _complete(client, system, build_user_message([("1", msg)]),
                            json_mode=attempt == 0, max_tokens=budget,
                            deadline=deadline, event=event, batch_size=1,
                            batch_index=batch_index, total_batches=total_batches,
                            completed=completed)
            results = parse_results(raw)
            item = results.get("1") or (next(iter(results.values())) if results else None)
            if item:
                return _to_classification(item)
        except OllamaBusy:
            raise
        except LLMError as exc:
            if exc.category in _TRANSIENT_CATEGORIES:
                raise _transient_as_busy(exc) from exc
            errors.append(f"{msg.subject[:40]!r}: {exc}")
        except ValueError as exc:
            errors.append(f"{msg.subject[:40]!r}: {exc}")
        cut_short = cut_short or _truncated(raw)
    return _unclassified("response cut off by the token limit" if cut_short
                         else "model output unparseable")


def _classify_batch(client: LLMClient, batch: list[tuple[str, NormalizedMessage]],
                    errors: list[str], *, deadline: float, event: EventFn | None,
                    batch_index: int, total_batches: int, completed: int
                    ) -> dict[str, Classification]:
    out: dict[str, Classification] = {}
    try:
        raw = _complete(
            client, SYSTEM, build_user_message(batch), json_mode=None,
            max_tokens=_BATCH_TOKENS_BASE + _BATCH_TOKENS_PER_ITEM * len(batch),
            deadline=deadline, event=event, batch_size=len(batch),
            batch_index=batch_index, total_batches=total_batches, completed=completed)
        if _truncated(raw):
            errors.append(f"batch of {len(batch)} hit the token limit; recovering what arrived")
        parsed = parse_results(raw)
    except OllamaBusy as exc:
        raise _BusyBatch(exc, out) from exc
    except LLMError as exc:
        if exc.category in _TRANSIENT_CATEGORIES:
            raise _BusyBatch(_transient_as_busy(exc), out) from exc
        errors.append(f"batch of {len(batch)} failed: {exc}")
        parsed = {}
    except ValueError as exc:
        errors.append(f"batch of {len(batch)} failed: {exc}")
        parsed = {}

    for local_id, _msg in batch:
        item = parsed.get(local_id)
        if item is not None:
            out[local_id] = _to_classification(item)

    missing = [(local_id, msg) for local_id, msg in batch if local_id not in out]
    if missing and len(missing) < len(batch):
        errors.append(f"{len(missing)} of {len(batch)} items missing from batch reply")
    for local_id, msg in missing:
        try:
            out[local_id] = _classify_one(
                client, msg, errors, deadline=deadline, event=event,
                batch_index=batch_index, total_batches=total_batches,
                completed=completed + len(out))
        except OllamaBusy as exc:
            raise _BusyBatch(exc, out) from exc
    return out


def classify(client: LLMClient, messages: list[NormalizedMessage], *, batch_size: int = 5,
             concurrency: int = 1, progress: ProgressFn | None = None,
             classification_deadline_seconds: int = 300,
             event: EventFn | None = None) -> tuple[list[Classification], list[str]]:
    """Classify in order; defer unresolved mail when Ollama becomes busy."""
    if not messages:
        return [], []
    # Concurrency is accepted for compatibility, but Ollama access is
    # intentionally serial to avoid queue and VRAM contention.
    _ = concurrency
    width = max(1, batch_size)
    batches = [[(str(start + offset), msg) for offset, msg in enumerate(messages[start:start + width])]
               for start in range(0, len(messages), width)]
    deadline = time.monotonic() + min(300, max(0.001, classification_deadline_seconds))
    errors: list[str] = []
    merged: dict[str, Classification] = {}
    done = 0

    for index, batch in enumerate(batches, start=1):
        try:
            merged.update(_classify_batch(
                client, batch, errors, deadline=deadline, event=event,
                batch_index=index, total_batches=len(batches), completed=done))
            done += len(batch)
            if progress:
                progress(done, len(messages))
        except _BusyBatch as busy:
            merged.update(busy.partial)
            done += len(busy.partial)
            reason = "Ollama busy; retry next check"
            errors.append(f"{busy.error}. Current and remaining mail will retry next check.")
            for later in batches[index - 1:]:
                for local_id, _msg in later:
                    if local_id not in merged:
                        merged[local_id] = _unclassified(reason, retryable=True)
            if progress:
                progress(done, len(messages))
            break

    return [merged.get(str(i), _unclassified("no result returned"))
            for i in range(len(messages))], errors
