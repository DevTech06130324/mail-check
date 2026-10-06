"""Validation of model output, plus the JSON-extraction ladder.

Free models are unreliable at JSON. This module assumes that: it recovers what
it can, per item, rather than failing a whole batch.

Two habits of reasoning models drove most of the recovery code here. They stop
one brace short of closing the object, or run out of budget mid-string, and the
answer we needed is sitting complete in the part that did arrive; and they key
``results`` by id instead of listing it. Both used to cost the whole reply.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..taxonomy import coerce_category

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)
# Reasoning models leak their scratchpad into `content`. It often contains
# braces and draft JSON, which would derail the extractor below.
_THINK = re.compile(
    r"<(think|thinking|reasoning|scratchpad)\b[^>]*>.*?</\1\s*>", re.I | re.S
)
_UNCLOSED_THINK = re.compile(r"^\s*<(think|thinking|reasoning|scratchpad)\b[^>]*>", re.I)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NULLISH = {"", "null", "none", "n/a", "na", "unknown", "not specified", "unspecified", "-"}


class ItemResult(BaseModel):
    id: str
    category: str
    confidence: float = 0.5
    company: str | None = None
    role: str | None = None
    deadline: str | None = None
    action_required: bool = False
    summary: str = Field(default="", max_length=400)

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, v: Any) -> str:
        return str(v)

    @field_validator("category", mode="before")
    @classmethod
    def _fix_category(cls, v: Any) -> str:
        return coerce_category(str(v) if v is not None else None)

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, v: Any) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.5
        if f > 1.0:  # models sometimes emit 0-100
            f = f / 100.0
        return max(0.0, min(1.0, f))

    @field_validator("company", "role", mode="before")
    @classmethod
    def _nullish(cls, v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return None if s.lower() in _NULLISH else s[:200]

    @field_validator("deadline", mode="before")
    @classmethod
    def _iso_only(cls, v: Any) -> str | None:
        """Keep only a real ISO date. A hallucinated 'next Tuesday' is worse
        than no deadline at all."""
        if v is None:
            return None
        s = str(v).strip()
        if s.lower() in _NULLISH:
            return None
        m = _ISO_DATE.search(s)
        return m.group(0) if m else None

    @field_validator("action_required", mode="before")
    @classmethod
    def _to_bool(cls, v: Any) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in {"true", "yes", "1", "y"}

    @field_validator("summary", mode="before")
    @classmethod
    def _clean_summary(cls, v: Any) -> str:
        if v is None:
            return ""
        return " ".join(str(v).split())[:400]


#: How many times a cut-off reply may be trimmed back looking for a parse.
#: Each step drops one fragment, so a handful covers any real truncation.
_MAX_REPAIR_TRIMS = 8


def _scan(body: str) -> tuple[list[str], bool, list[int]]:
    """Walk JSON text, ignoring brackets inside strings.

    Returns the still-open brackets, whether the walk ended inside a string,
    and the offsets we may safely cut back to - each structural comma, and the
    position just after each opener.
    """
    stack: list[str] = []
    cuts: list[int] = []
    in_string = False
    escape = False
    for i, ch in enumerate(body):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
            cuts.append(i + 1)
        elif ch in "}]":
            if stack:
                stack.pop()
        elif ch == ",":
            cuts.append(i)
    return stack, in_string, cuts


def _close(body: str) -> str | None:
    """Terminate an open string and close every open bracket."""
    stack, in_string, _ = _scan(body)
    if not stack and not in_string:
        return None  # already balanced - whatever is wrong, it is not this
    return (
        body
        + ('"' if in_string else "")
        + "".join("}" if ch == "{" else "]" for ch in reversed(stack))
    )


def repair_json(text: str) -> str | None:
    """Close a reply that was cut off, or that stopped a brace short.

    Reasoning models spend their budget thinking and get truncated mid-answer,
    and this one also simply miscounts its closing braces. Either way the
    fields we need already arrived and only the tail is missing, so close what
    is open and see if it parses. When the cut landed mid-key - `"confidence`
    with no value - closing alone leaves nonsense, so drop the half-written
    fragment and try again from the previous comma.

    Returns the repaired text, or ``None`` if nothing here can be salvaged.
    Never invents a field: an item whose category had not been written yet
    simply fails validation later and is retried like any other missing id.
    """
    first = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if first == -1:
        return None

    body = text[first:]
    for _ in range(_MAX_REPAIR_TRIMS):
        closed = _close(body)
        if closed is not None:
            try:
                json.loads(closed)
                return closed
            except json.JSONDecodeError:
                pass
        _, _, cuts = _scan(body)
        if not cuts or cuts[-1] >= len(body):
            return None  # no fragment left to drop, or dropping it changes nothing
        body = body[: cuts[-1]]
    return None


def extract_json(text: str) -> Any:
    """Get a JSON value out of whatever the model returned.

    Handles: clean JSON, fenced JSON, JSON with prose around it, a bare array
    where an object was requested, and a reply that was cut off before it
    finished.
    """
    if not text:
        raise ValueError("empty response")

    candidate = _THINK.sub("", text).strip()
    if _UNCLOSED_THINK.match(candidate):
        # Truncated scratchpad with no closing tag: keep only what follows the
        # last plausible end of reasoning.
        candidate = re.split(r"</\w+\s*>", candidate)[-1].strip()
    candidate = _FENCE.sub("", candidate.strip())
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    for opener, closer in (("{", "}"), ("[", "]")):
        block = _balanced(candidate, opener, closer)
        if block is not None:
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                continue

    # Last resort: nothing balanced parsed, so assume the reply was cut short
    # and close it ourselves.
    repaired = repair_json(candidate)
    if repaired is not None:
        value = json.loads(repaired)
        # Trimming can strip a reply back to `{}` - true of prose that merely
        # contains a stray brace. An empty container is not a recovery, and
        # returning one would report "parsed, nothing in it" for what is really
        # an unusable reply, so keep treating it as a failure.
        if value:
            return value

    raise ValueError(f"no JSON found in response: {_excerpt(text)}")


def _excerpt(text: str, limit: int = 700) -> str:
    """Keep both ends of an over-long reply.

    A truncated answer fails at its tail, so a head-only excerpt shows the part
    that was fine and hides the part that broke.
    """
    if len(text) <= limit:
        return repr(text)
    half = limit // 2
    return f"{text[:half]!r} ... [{len(text) - limit} chars omitted] ... {text[-half:]!r}"


def _balanced(text: str, opener: str, closer: str) -> str | None:
    """Slice the first balanced bracket block, ignoring brackets inside strings."""
    start = text.find(opener)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _entries(node: dict) -> list[dict]:
    """Flatten a mapping into a list of items, keyed-by-id or single-object."""
    if "category" in node:
        return [node]
    return [
        {**v, "id": v.get("id", k)} for k, v in node.items() if isinstance(v, dict)
    ]


def parse_results(text: str) -> dict[str, ItemResult]:
    """Return {id: ItemResult} for every item that validates.

    Items that fail validation are dropped, not raised — the caller retries the
    missing ids individually, so one malformed entry cannot cost us the rest of
    the batch.
    """
    data = extract_json(text)

    if isinstance(data, dict):
        for key in ("results", "emails", "classifications", "items", "data", "output"):
            node = data.get(key)
            if isinstance(node, list):
                data = node
                break
            if isinstance(node, dict):
                # `{"results": {"0": {...}, "1": {...}}}` - the envelope is
                # there but keyed by id rather than listed. Nothing is wrong
                # with this reply except its shape, so unwrap it rather than
                # throwing away every item in it.
                data = _entries(node)
                break
        else:
            # A single bare object, or an {id: {...}} mapping.
            data = _entries(data)

    if not isinstance(data, list):
        raise ValueError(f"expected a list of results, got {type(data).__name__}")

    out: dict[str, ItemResult] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            item = ItemResult.model_validate(entry)
        except Exception:  # noqa: BLE001 - one bad item must not fail the batch
            continue
        out[item.id] = item
    return out
