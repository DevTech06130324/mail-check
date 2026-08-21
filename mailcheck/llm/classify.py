"""Batching and the JSON repair ladder.

Order of attack, per §8 of the design:
  1. batch request, JSON mode
  2. parse leniently (fences, prose-wrapped, alternate envelopes, cut-off JSON)
  3. ids missing or invalid -> retry those individually, with more room and a
     stricter prompt
  4. still missing -> `unclassified`, stored and surfaced, never silently dropped

A batch never fails as a unit; one bad item cannot take down the other seven.

The budgets below are sized for reasoning models, which is what a "free" model
usually turns out to be. Their thinking is billed against `max_tokens` before a
single character of the answer is written - measured at ~1200 reasoning tokens
to think about a batch of eight - so a budget sized for the JSON alone buys a
reply that stops mid-answer. Every number here is answer plus thinking.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from ..models import Classification, NormalizedMessage
from ..taxonomy import UNCLASSIFIED
from .client import LLMClient, LLMError
from .prompt import STRICT_SUFFIX, SYSTEM, build_user_message
from .schema import ItemResult, parse_results

ProgressFn = Callable[[int, int], None]

#: Per-message allowance for a batch call, plus a fixed slice for the envelope
#: and the thinking that precedes it.
_BATCH_TOKENS_PER_ITEM = 512
_BATCH_TOKENS_BASE = 1024

#: The single-message ladder: a normal budget, then a markedly larger one. An
#: under-budgeted reply is a total loss, while an unused allowance costs
#: nothing, so the second attempt buys room rather than just rewording.
_SINGLE_TOKENS = (2048, 6144)


def _to_classification(item: ItemResult) -> Classification:
    return Classification(
        category=item.category,
        confidence=item.confidence,
        company=item.company,
        role=item.role,
        deadline=item.deadline,
        action_required=item.action_required,
        summary=item.summary,
        source="llm",
    )


def _unclassified(reason: str) -> Classification:
    return Classification(
        category=UNCLASSIFIED,
        confidence=0.0,
        action_required=True,  # unknown means "look at it yourself"
        summary=f"Could not classify automatically ({reason}).",
        source="llm",
    )


def _truncated(raw: object) -> bool:
    """Did the model stop because it ran out of budget rather than ideas?"""
    return getattr(raw, "finish_reason", None) == "length"


def _classify_one(
    client: LLMClient, msg: NormalizedMessage, errors: list[str]
) -> Classification:
    """Single-email fallback: more room each pass, then a stricter prompt."""
    cut_short = False
    for attempt, budget in enumerate(_SINGLE_TOKENS):
        system = SYSTEM + (STRICT_SUFFIX if attempt else "")
        raw = None
        try:
            raw = client.complete(
                system,
                build_user_message([("1", msg)]),
                # Second pass drops JSON mode: if the endpoint silently ignores
                # it, the stricter wording is what actually helps.
                json_mode=attempt == 0,
                max_tokens=budget,
            )
            results = parse_results(raw)
            item = results.get("1") or (next(iter(results.values())) if results else None)
            if item:
                return _to_classification(item)
        except (LLMError, ValueError) as exc:
            errors.append(f"{msg.subject[:40]!r}: {exc}")
        cut_short = cut_short or _truncated(raw)
    return _unclassified(
        "response cut off by the token limit" if cut_short else "model output unparseable"
    )


def _classify_batch(
    client: LLMClient,
    batch: list[tuple[str, NormalizedMessage]],
    errors: list[str],
) -> dict[str, Classification]:
    out: dict[str, Classification] = {}
    try:
        raw = client.complete(
            SYSTEM,
            build_user_message(batch),
            # None defers to the client's configured llm.use_json_mode, so an
            # endpoint that mishandles response_format can have it turned off
            # from the start rather than only after a 400 forces a fallback.
            json_mode=None,
            max_tokens=_BATCH_TOKENS_PER_ITEM * len(batch) + _BATCH_TOKENS_BASE,
        )
        if _truncated(raw):
            errors.append(
                f"batch of {len(batch)} hit the token limit; "
                "recovering what arrived and retrying the rest singly"
            )
        parsed = parse_results(raw)
    except (LLMError, ValueError) as exc:
        errors.append(f"batch of {len(batch)} failed: {exc}")
        parsed = {}

    for local_id, msg in batch:
        item = parsed.get(local_id)
        if item is not None:
            out[local_id] = _to_classification(item)

    missing = [(lid, msg) for lid, msg in batch if lid not in out]
    if missing and len(missing) < len(batch):
        errors.append(f"{len(missing)} of {len(batch)} items missing from batch reply")
    for local_id, msg in missing:
        out[local_id] = _classify_one(client, msg, errors)
    return out


def classify(
    client: LLMClient,
    messages: list[NormalizedMessage],
    *,
    batch_size: int = 8,
    concurrency: int = 1,
    progress: ProgressFn | None = None,
) -> tuple[list[Classification], list[str]]:
    """Classify in input order. Returns (classifications, non-fatal errors)."""
    if not messages:
        return [], []

    batches: list[list[tuple[str, NormalizedMessage]]] = []
    for start in range(0, len(messages), max(1, batch_size)):
        chunk = messages[start : start + max(1, batch_size)]
        batches.append([(str(start + i), msg) for i, msg in enumerate(chunk)])

    errors: list[str] = []
    merged: dict[str, Classification] = {}
    done = 0

    def run(batch):
        return _classify_batch(client, batch, errors)

    if concurrency > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for result, batch in zip(pool.map(run, batches), batches):
                merged.update(result)
                done += len(batch)
                if progress:
                    progress(done, len(messages))
    else:
        for batch in batches:
            merged.update(run(batch))
            done += len(batch)
            if progress:
                progress(done, len(messages))

    return [
        merged.get(str(i), _unclassified("no result returned"))
        for i in range(len(messages))
    ], errors
