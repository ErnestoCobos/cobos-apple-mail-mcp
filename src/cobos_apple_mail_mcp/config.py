"""Configuration: typed sections + a loader with the documented precedence
CLI > env (APPLE_MAIL_*) > config.toml > defaults.

Implemented by hand (rather than pydantic-settings' env/source magic) so the
merge order is explicit, testable without touching real env state, and the
flat `APPLE_MAIL_<SECTION>_<KEY>` env naming matches the spec exactly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_DIR = Path.home() / ".cobos-apple-mail-mcp"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"
DEFAULT_INDEX_PATH = DEFAULT_CONFIG_DIR / "index.db"

_ENV_PREFIX = "APPLE_MAIL_"
_SECTIONS = (
    "defaults",
    "index",
    "server",
    "batch_limits",
    "confirmation",
    "embeddings",
    "timeouts",
)


class DefaultsSection(BaseModel):
    account: str | None = None
    mailbox: str = "INBOX"


class IndexSection(BaseModel):
    path: str = str(DEFAULT_INDEX_PATH)
    max_emails: int | None = None
    staleness_hours: float = 24.0
    exclude_mailboxes: list[str] = Field(default_factory=lambda: ["Drafts"])
    include_mailboxes: list[str] | None = None
    exclude_accounts: list[str] = Field(default_factory=list)
    enable_trigram: bool = False

    @field_validator("path")
    @classmethod
    def _expand_path(cls, value: str) -> str:
        return str(Path(value).expanduser())


class ServerSection(BaseModel):
    read_only: bool = False


class BatchLimitsSection(BaseModel):
    """Conservative defaults per CLAUDE.md invariant #2 — exceeding rejects, never truncates."""

    move: int = 1
    status: int = 10
    trash: int = 5
    delete: int = 1


class ConfirmationSection(BaseModel):
    require_confirm: list[str] = Field(
        default_factory=lambda: ["permanent_delete", "empty_trash", "delete_rule"]
    )


class EmbeddingsSection(BaseModel):
    """Off by default. Apple NaturalLanguage (PyObjC, no model download) is
    the frugal default backend; MiniLM is an opt-in fallback."""

    enabled: bool = False
    backend: Literal["apple_nl", "minilm"] = "apple_nl"
    model: str | None = None


class TimeoutsSection(BaseModel):
    """The never-hang knobs (CLAUDE.md invariant #4)."""

    jxa_call_sec: float = 20.0
    broad_scan_sec: float = 30.0
    mail_launch_sec: float = 15.0
    http_sec: float = 15.0  # bound for the RFC-8058 one-click unsubscribe POST


class AttachmentsSection(BaseModel):
    """Off by default. When enabled (and the [attachments] extra is installed),
    a low-priority backfill extracts PDF/DOCX text so search(scope=attachments)
    matches attachment content, not just filenames. Off adds no dependency."""

    extract_text: bool = False
    max_file_size_mb: int = 25  # skip attachments larger than this (extraction cost)


class Config(BaseModel):
    config_version: int = 1
    defaults: DefaultsSection = Field(default_factory=DefaultsSection)
    index: IndexSection = Field(default_factory=IndexSection)
    server: ServerSection = Field(default_factory=ServerSection)
    batch_limits: BatchLimitsSection = Field(default_factory=BatchLimitsSection)
    confirmation: ConfirmationSection = Field(default_factory=ConfirmationSection)
    embeddings: EmbeddingsSection = Field(default_factory=EmbeddingsSection)
    attachments: AttachmentsSection = Field(default_factory=AttachmentsSection)
    timeouts: TimeoutsSection = Field(default_factory=TimeoutsSection)


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - py3.10 fallback
        import tomli as tomllib  # type: ignore[no-redef]
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _coerce_env_value(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("none", "null", ""):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    stripped = raw.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    if "," in raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def _env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Map flat `APPLE_MAIL_<SECTION>_<KEY>` env vars onto the nested config
    dict, e.g. `APPLE_MAIL_SERVER_READ_ONLY=true` -> {"server": {"read_only": True}}.
    """
    env = environ if environ is not None else os.environ
    overrides: dict[str, Any] = {}
    sorted_sections = sorted(_SECTIONS, key=len, reverse=True)
    for env_key, raw_value in env.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue
        if env_key == "APPLE_MAIL_CONFIG_PATH":
            continue
        rest = env_key[len(_ENV_PREFIX) :].lower()
        matched_section = next(
            (s for s in sorted_sections if rest == s or rest.startswith(s + "_")), None
        )
        if matched_section is None or rest == matched_section:
            continue
        key = rest[len(matched_section) + 1 :]
        overrides.setdefault(matched_section, {})[key] = _coerce_env_value(raw_value)
    return overrides


def load_config(
    cli_overrides: dict[str, Any] | None = None,
    config_path: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> Config:
    """Build the effective Config with precedence CLI > env > config.toml > defaults."""
    env = environ if environ is not None else os.environ
    resolved_path = config_path or env.get("APPLE_MAIL_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    path = Path(resolved_path)

    merged: dict[str, Any] = {}
    merged = _deep_merge(merged, _load_toml(path))
    merged = _deep_merge(merged, _env_overrides(env))
    if cli_overrides:
        merged = _deep_merge(merged, cli_overrides)
    return Config.model_validate(merged)


def generate_default_config_toml() -> str:
    """Fully-commented config.toml template, written by `apple-mail-mcp init`."""
    return f'''\
# cobos-apple-mail-mcp configuration
# Precedence: CLI flags > APPLE_MAIL_* env vars > this file > built-in defaults.
config_version = 1

[defaults]
# account = "Work"        # default account name when a tool call omits one
mailbox = "INBOX"          # default mailbox when a tool call omits one

[index]
# Where the derived FTS5/search index lives. Always rebuildable from disk.
path = "{DEFAULT_INDEX_PATH}"
# max_emails = 50000       # uncomment to cap per-mailbox indexed emails
staleness_hours = 24.0     # `index status` flags the index stale past this age
exclude_mailboxes = ["Drafts"]
# include_mailboxes = ["INBOX", "Sent"]   # uncomment to index only these
exclude_accounts = []
enable_trigram = false     # substring search; ~doubles index size

[server]
read_only = false          # disable all send/modify tools (drafts still allowed)

[batch_limits]
# Per-call ceilings. Exceeding rejects the call; never silently truncated.
move = 1
status = 10
trash = 5
delete = 1

[confirmation]
require_confirm = ["permanent_delete", "empty_trash", "delete_rule"]

[embeddings]
enabled = false            # optional hybrid/semantic search layer, off by default
backend = "apple_nl"       # "apple_nl" (built into macOS, no download) | "minilm"
# model = "all-MiniLM-L6-v2"  # only used when backend = "minilm"

[attachments]
extract_text = false       # extract PDF/DOCX text so search(scope=attachments) matches
                           # content, not just filenames (needs the [attachments] extra;
                           # run `apple-mail-mcp index extract-attachments` to backfill)
max_file_size_mb = 25      # skip attachments larger than this

[timeouts]
# The never-hang knobs: every external call is bounded by one of these.
jxa_call_sec = 20.0
broad_scan_sec = 30.0
mail_launch_sec = 15.0
http_sec = 15.0            # bound for the one-click unsubscribe POST
'''
