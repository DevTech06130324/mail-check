"""Validation of model output, plus the JSON-extraction ladder.

Free models are unreliable at JSON. This module assumes that: it recovers what
it can, per item, rather than failing a whole batch.
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


def extract_json(text: str) -> Any:
    """Get a JSON value out of whatever the model returned.

    Handles: clean JSON, fenced JSON, JSON with prose around it, and a bare
    array where an object was requested.
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
    raise ValueError(f"no JSON found in response: {text[:200]!r}")


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


def parse_results(text: str) -> dict[str, ItemResult]:
    """Return {id: ItemResult} for every item that validates.

    Items that fail validation are dropped, not raised — the caller retries the
    missing ids individually, so one malformed entry cannot cost us the rest of
    the batch.
    """
    data = extract_json(text)

    if isinstance(data, dict):
        for key in ("results", "emails", "classifications", "items", "data", "output"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            # A single bare object, or an {id: {...}} mapping.
            if "category" in data:
                data = [data]
            else:
                data = [
                    {**v, "id": v.get("id", k)}
                    for k, v in data.items()
                    if isinstance(v, dict)
                ]

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
