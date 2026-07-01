"""Connection factories for our derived `index.db` and for read-only access
to Apple Mail's live `Envelope Index`.

Never opens `Envelope Index` for writing (CLAUDE.md invariant #6): it is
opened `immutable=1`, which sidesteps SQLite's locking protocol entirely
rather than racing Mail.app for a lock (and avoids touching the `-wal`/`-shm`
files it owns).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cobos_apple_mail_mcp.storage.migrations import migrate

_PRAGMAS = (
    "PRAGMA journal_mode = WAL;",
    "PRAGMA synchronous = NORMAL;",
    "PRAGMA cache_size = -65536;",
    "PRAGMA temp_store = MEMORY;",
    "PRAGMA mmap_size = 268435456;",
    "PRAGMA foreign_keys = ON;",
    "PRAGMA busy_timeout = 5000;",
)


def connect_index(path: str | Path, *, run_migrations: bool = True) -> sqlite3.Connection:
    """Open (or create) our derived index.db with the standard PRAGMAs."""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    if run_migrations:
        migrate(conn)
    return conn


def open_envelope_index_readonly(envelope_index_path: str | Path) -> sqlite3.Connection:
    """Open Apple Mail's live Envelope Index strictly read-only/immutable.

    `immutable=1` tells SQLite the file (and its -wal/-shm siblings) will not
    change for the lifetime of the connection from this process's point of
    view, which skips locking entirely — safe because we never write, and it
    means a busy Mail.app can never block or be blocked by our reads.
    """
    uri = f"file:{Path(envelope_index_path).as_posix()}?immutable=1"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Best-effort load of the sqlite-vec extension. Returns False (never
    raises) when the optional [semantic] dependency isn't installed, *or* when
    the running interpreter's sqlite3 was built without loadable-extension
    support — some prebuilt Python distributions (notably the macOS builds
    GitHub's setup-python ships) omit `Connection.enable_load_extension`
    entirely, and calling it would raise `AttributeError`. Both cases must
    degrade gracefully per CLAUDE.md packaging notes, not crash.
    """
    try:
        import sqlite_vec
    except ImportError:
        return False
    if not hasattr(conn, "enable_load_extension"):
        return False
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
    except (AttributeError, sqlite3.Error):
        return False
    return True


def get_sync_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_sync_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    import time

    conn.execute(
        "INSERT INTO sync_state(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, time.time()),
    )
    conn.commit()
