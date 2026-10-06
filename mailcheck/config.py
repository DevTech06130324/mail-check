"""TOML config living in the user config dir. Secrets are NOT stored here."""

from __future__ import annotations

import os
import re
import tempfile
import threading
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import tomli_w
from platformdirs import user_config_dir, user_data_dir
from pydantic import BaseModel, Field, field_validator

APP_NAME = "mail-check"


def config_dir() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False))


def data_dir() -> Path:
    return Path(user_data_dir(APP_NAME, appauthor=False))


def config_path() -> Path:
    return config_dir() / "config.toml"


def db_path() -> Path:
    return data_dir() / "mailcheck.db"


class LLMConfig(BaseModel):
    base_url: str = ""
    """Ollama server root, e.g. http://192.168.2.230:11440 (no /v1).

    There is deliberately no model setting: each run uses whichever model the
    server has loaded (``LLMClient.resolve_model``). Older configs that still
    carry a ``model`` key load fine; pydantic ignores it and the next save drops it.
    """
    batch_size: int = Field(default=5, ge=1, le=32)
    max_body_chars: int = Field(default=1200, ge=200, le=20000)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout_seconds: int = Field(default=60, ge=5, le=60)
    classification_deadline_seconds: int = Field(default=300, ge=5, le=300)
    max_retries: int = Field(default=2, ge=1, le=10)
    """Legacy values load safely; the client enforces at most two attempts."""
    concurrency: int = Field(default=1, ge=1, le=1)
    """One request at a time avoids competing for local model memory."""
    use_json_mode: bool = True
    """Request native Ollama JSON output."""
    num_ctx: int = Field(default=8192, ge=1024, le=262144)
    think: bool = False
    keep_alive: str = "5m"

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def cap_legacy_timeout(cls, value):
        """Load older 120/300-second configs while enforcing the new bound."""
        try:
            return min(int(value), 60)
        except (TypeError, ValueError):
            return value

    @field_validator("classification_deadline_seconds", mode="before")
    @classmethod
    def cap_legacy_deadline(cls, value):
        try:
            return min(int(value), 300)
        except (TypeError, ValueError):
            return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return ollama_root(value)

    @field_validator("keep_alive")
    @classmethod
    def validate_keep_alive(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"(?:0|\d+(?:\.\d+)?(?:ms|s|m|h))", value):
            raise ValueError("Use an Ollama keep-alive duration such as 5m, 30s, or 0.")
        return value


def ollama_root(value: str) -> str:
    """Validate once for both saved settings and directly constructed clients."""
    value = value.strip().rstrip("/")
    if not value:
        return value
    parts = urlsplit(value)
    if parts.path or parts.query or parts.fragment:
        raise ValueError("Use the Ollama server root (e.g. http://192.168.2.230:11440), "
                         "without /v1 or /api/chat. Migrate the old endpoint in config.toml.")
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Ollama base_url must be an http:// or https:// server root without credentials.")
    # Accessing port also rejects malformed/non-numeric port values.
    _ = parts.port
    return value


class CheckConfig(BaseModel):
    lookback_days: int = Field(default=2, ge=1, le=3650)
    """How far back each *fetch* reaches — the single biggest cost in a run.

    Deliberately short. Every check re-downloads the full body of every unread
    message inside this window before the cache can discard it, so a 30-day
    window costs ~15x a 2-day one on a mailbox that is checked regularly.
    Nothing is lost by shrinking it: mail fetched under an older, wider window
    stays in the database until it ages out of ``retain_days``, and keeps
    showing up in the console's own (separate) display range until then.
    """

    retain_days: int = Field(default=7, ge=1, le=3650)
    """How long triaged mail is kept locally. Older mail is deleted on each check.

    The local store is a rolling window, not an archive: without this it grows
    forever, and a stored message is a full email body sitting on disk. Seven
    days is what a triage queue actually needs — anything older has been dealt
    with or is not going to be.

    Only ever deletes from mail-check's own database. Provider mail is never
    touched, so a pruned message is still in Gmail or Outlook, unread and
    exactly as it was.

    A check never prunes inside its own fetch window, even when that window is
    the wider one: a message about to be re-downloaded must not be deleted and
    re-created, because that would drop its Done state and put it back in the
    queue. So the effective window is ``max(retain_days, lookback_days)``.
    """


class WatchConfig(BaseModel):
    interval_minutes: int = Field(default=10, ge=1, le=1440)
    auto_check: bool = False
    """Let the web console run checks on the interval by itself.

    Off by default: opening a page should never start spending tokens without
    the user asking for it.
    """


class OutlookConfig(BaseModel):
    client_id: str = ""
    """Public application (client) ID from a Microsoft Entra app registration.

    A public identifier, not a credential — it is safe in this file. Without it
    the Accounts page explains how to configure one rather than starting a
    sign-in that cannot succeed.
    """


class PrefilterRule(BaseModel):
    category: str
    sender: str | None = None
    """Exact match (case-insensitive) on the From address."""
    sender_domain: str | None = None
    subject_contains: str | None = None


class Config(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    check: CheckConfig = Field(default_factory=CheckConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    outlook: OutlookConfig = Field(default_factory=OutlookConfig)
    prefilter_rules: list[PrefilterRule] = Field(default_factory=list)
    privacy_ack: bool = False
    """Set once the user has been shown the "bodies leave your machine" notice."""

    def is_llm_ready(self) -> bool:
        return bool(self.llm.base_url)


#: Parsed config.toml keyed by path, guarded by (mtime_ns, size). The web
#: console reads the config on every page render, on every /api/status poll —
#: which runs about once a second during a check — and once every two seconds
#: in the scheduler thread, so re-reading and re-parsing the file each time is
#: pure waste. Only the *parsed dict* is cached, never a Config instance:
#: callers routinely mutate what load() hands back (``cfg.watch.auto_check =
#: ...``, ``cfg.prefilter_rules.append(...)``) and a shared instance would let
#: one of those mutations leak into every later reader.
_cache_lock = threading.Lock()
_cache: dict[str, tuple[tuple[int, int], dict]] = {}


def _read_toml(path: Path) -> dict:
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        with _cache_lock:
            _cache.pop(key, None)
        return {}
    stamp = (st.st_mtime_ns, st.st_size)

    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == stamp:
            return hit[1]

    with path.open("rb") as fh:
        data = tomllib.load(fh)
    with _cache_lock:
        _cache[key] = (stamp, data)
    return data


def load() -> Config:
    return Config.model_validate(_read_toml(config_path()))


#: Serializes writes within this process — the web console's request-handling
#: threads and its background scheduler thread can both call save().
_save_lock = threading.Lock()


def save(cfg: Config) -> Path:
    """Write config.toml atomically.

    A direct ``open("wb")`` truncates the file before writing the replacement,
    so a crash or a second concurrent writer mid-write can corrupt it, and
    every page in the app fails to load until it is fixed by hand. Writing to
    a temp file and renaming into place is atomic on both POSIX and Windows
    (``os.replace`` uses ``MoveFileEx`` with ``MOVEFILE_REPLACE_EXISTING``), so
    a reader always sees either the old file or the fully-written new one.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json", exclude_none=True)
    # tomli_w cannot serialise None; drop them recursively.
    payload = _strip_none(data)

    with _save_lock:
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=".config-", suffix=".toml.tmp"
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                tomli_w.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
            # Drop the parse cache explicitly rather than relying on the new
            # mtime: filesystem timestamp granularity is coarse enough on
            # Windows that a save immediately following a read can land in the
            # same tick, and a stale hit would silently discard the write.
            with _cache_lock:
                _cache.pop(str(path), None)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    return path


def _strip_none(obj):
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none(v) for v in obj]
    return obj


def set_dotted(cfg: Config, key: str, value: str) -> Config:
    """Apply ``llm.num_ctx=x`` style updates, re-validating through pydantic."""
    data = cfg.model_dump(mode="json")
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            raise KeyError(key)
        node = node[part]
    leaf = parts[-1]
    if leaf not in node:
        raise KeyError(key)
    node[leaf] = value
    return Config.model_validate(data)
