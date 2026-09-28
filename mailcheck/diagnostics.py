"""Privacy-safe rotating performance diagnostics.

Only a fixed allow-list of numeric/status fields reaches disk. Email content,
addresses, prompts, credentials, and model output cannot be serialized here.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import data_dir

_SAFE_FIELDS = {
    "event", "outcome", "batch_size", "batch_index", "total_batches",
    "completed", "wall_seconds", "attempts", "retries", "finish_reason",
    "total_seconds", "load_seconds", "prompt_tokens", "prompt_seconds",
    "output_tokens", "output_seconds", "fetch_seconds",
    "classification_seconds", "llm_requests", "llm_timeouts",
    "llm_busy_responses", "llm_retries", "fetched", "classified",
    "from_cache", "retryable",
}


class PerformanceLog:
    def __init__(self, path: Path | None = None, *, max_bytes: int = 2 * 1024 * 1024,
                 backup_count: int = 3) -> None:
        self.path = path or data_dir() / "performance.jsonl"
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.Lock()

    def write(self, values: dict) -> None:
        safe = {key: value for key, value in values.items()
                if key in _SAFE_FIELDS and isinstance(value, (str, int, float, bool, type(None)))}
        safe["at"] = datetime.now(timezone.utc).isoformat()
        line = json.dumps(safe, separators=(",", ":"), sort_keys=True) + "\n"
        encoded_size = len(line.encode("utf-8"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            current_size = self.path.stat().st_size if self.path.exists() else 0
            if current_size and current_size + encoded_size > self.max_bytes:
                self._rotate()
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(line)

    def _rotate(self) -> None:
        if self.backup_count <= 0:
            self.path.unlink(missing_ok=True)
            return
        oldest = Path(f"{self.path}.{self.backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = Path(f"{self.path}.{index}")
            if source.exists():
                source.replace(Path(f"{self.path}.{index + 1}"))
        if self.path.exists():
            self.path.replace(Path(f"{self.path}.1"))
