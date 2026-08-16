"""Plain data carried between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawMessage:
    """What a MailSource yields. Provider-agnostic."""

    message_id: str
    uid: str
    folder: str
    from_addr: str
    from_name: str
    subject: str
    date_utc: datetime | None
    text: str
    """Best-effort plain text, not yet cleaned."""
    provider_url: str | None = None
    """Deep link to the original, when the provider supplies one (Graph webLink).
    IMAP has no equivalent, so Gmail falls back to a Message-ID search URL."""


@dataclass
class NormalizedMessage:
    """A RawMessage after cleaning, ready for the LLM."""

    account_id: int
    account_label: str
    message_id: str
    uid: str
    folder: str
    from_addr: str
    from_name: str
    subject: str
    date_utc: datetime | None
    body: str
    """Cleaned, truncated plain text."""
    provider_url: str | None = None

    @property
    def snippet(self) -> str:
        return self.body[:200].replace("\n", " ").strip()


@dataclass
class Classification:
    category: str
    confidence: float = 0.0
    company: str | None = None
    role: str | None = None
    deadline: str | None = None
    action_required: bool = False
    summary: str = ""
    source: str = "llm"
    """``llm`` or ``prefilter``."""


@dataclass
class TriagedMessage:
    message: NormalizedMessage
    classification: Classification
    from_cache: bool = False


@dataclass
class RunResult:
    started_at: datetime
    finished_at: datetime | None = None
    accounts_checked: int = 0
    fetched: int = 0
    classified: int = 0
    from_cache: int = 0
    prefiltered: int = 0
    pruned: int = 0
    """Stored messages deleted for ageing out of the retention window."""
    items: list[TriagedMessage] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def urgent(self) -> list[TriagedMessage]:
        from .taxonomy import is_urgent

        return [i for i in self.items if is_urgent(i.classification.category)]
