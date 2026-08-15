"""The pluggable seam. Everything downstream consumes RawMessage.

Adding Gmail API or Microsoft Graph later means a new module here that
satisfies this protocol — not a refactor of the pipeline.
"""

from __future__ import annotations

from datetime import date
from typing import Iterator, Protocol, runtime_checkable

from ..models import RawMessage


class SourceError(RuntimeError):
    """Anything that stops one account from being read.

    The pipeline catches these per-account so one broken mailbox cannot abort
    a whole run.
    """


@runtime_checkable
class MailSource(Protocol):
    def test(self) -> None:
        """Connect and authenticate. Raises SourceError on failure."""

    def fetch_unread(self, since: date) -> Iterator[RawMessage]:
        """Yield unread messages newer than ``since``, without mutating them."""
