"""Locate Apple Mail's data directory and (best-effort) read its live
`Envelope Index` SQLite database, read-only/immutable.

`.emlx` files plus their plist trailers are the authoritative source for our
indexer (see emlx_parser.py) — every field we need (flags, dates, headers)
is also present there. The Envelope Index is read only as a fast
supplementary hint; if its schema differs across a macOS version or it can't
be opened, callers degrade gracefully rather than failing (CLAUDE.md
invariant #4: never hang, never hard-fail on an optional path).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cobos_apple_mail_mcp.storage.database import open_envelope_index_readonly

# Cocoa/Core Foundation reference date: 2001-01-01T00:00:00Z, expressed as an
# offset in seconds from the Unix epoch (1970-01-01T00:00:00Z).
COCOA_EPOCH_OFFSET = 978307200

_ROLE_BY_NAME = {
    "inbox": "inbox",
    "sent": "sent",
    "sent messages": "sent",
    "drafts": "drafts",
    "trash": "trash",
    "deleted messages": "trash",
    "junk": "junk",
    "junk e-mail": "junk",
    "archive": "archive",
    "all mail": "archive",
}


def apple_to_unix(timestamp: float | int | None) -> int | None:
    """Convert a Cocoa-epoch timestamp (seconds since 2001-01-01) to Unix seconds."""
    if timestamp is None:
        return None
    return int(timestamp) + COCOA_EPOCH_OFFSET


def unix_to_apple(timestamp: int | float | None) -> int | None:
    if timestamp is None:
        return None
    return int(timestamp) - COCOA_EPOCH_OFFSET


def classify_mailbox_role(mailbox_name: str | None) -> str:
    if not mailbox_name:
        return "other"
    return _ROLE_BY_NAME.get(mailbox_name.strip().lower(), "other")


def find_mail_directory(home: Path | None = None) -> Path | None:
    """Find the highest `V{N}` Mail data directory under ~/Library/Mail/.

    Multiple version directories can coexist on a system that's been
    upgraded across several macOS releases; the highest number is current.
    """
    library_mail = (home or Path.home()) / "Library" / "Mail"
    if not library_mail.is_dir():
        return None
    versions: list[tuple[int, Path]] = []
    for entry in library_mail.iterdir():
        if entry.is_dir() and entry.name.startswith("V") and entry.name[1:].isdigit():
            versions.append((int(entry.name[1:]), entry))
    if not versions:
        return None
    versions.sort(key=lambda pair: pair[0], reverse=True)
    return versions[0][1]


def find_envelope_index(mail_dir: Path) -> Path | None:
    candidate = mail_dir / "MailData" / "Envelope Index"
    return candidate if candidate.is_file() else None


def list_account_directories(mail_dir: Path) -> list[Path]:
    """UUID-named account directories directly under the version directory."""
    return [
        entry
        for entry in mail_dir.iterdir()
        if entry.is_dir() and entry.name != "MailData" and "-" in entry.name
    ]


def open_envelope_index(mail_dir: Path) -> sqlite3.Connection | None:
    """Best-effort open; returns None instead of raising when the Envelope
    Index is missing or unreadable so callers can fall back to .emlx-only.
    """
    path = find_envelope_index(mail_dir)
    if path is None:
        return None
    try:
        conn = open_envelope_index_readonly(path)
        conn.execute("SELECT 1").fetchone()
        return conn
    except sqlite3.Error:
        return None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {row["name"] for row in rows}


def read_envelope_flags(conn: sqlite3.Connection, rowid: int) -> dict[str, int] | None:
    """Best-effort supplementary read/flagged/answered status for one ROWID.

    Schema is reverse-engineered and may shift between macOS releases; this
    introspects available columns rather than assuming a fixed layout, and
    returns None on any mismatch so the caller relies on the .emlx plist
    (the authoritative source) instead.
    """
    columns = _table_columns(conn, "messages")
    if "ROWID" not in columns and not columns:
        return None
    select_cols = [c for c in ("flags", "read") if c in columns]
    if not select_cols:
        return None
    try:
        row = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM messages WHERE ROWID = ?", (rowid,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {col: row[col] for col in select_cols}
