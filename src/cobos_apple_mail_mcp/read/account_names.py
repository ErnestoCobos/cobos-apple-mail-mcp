"""Best-effort resolution of human-readable account display names.

Apple Mail's own on-disk data has no display-name mapping for its
`~/Library/Mail/V{N}/{UUID}/` account directories -- verified against a real
7-account mailbox: the Envelope Index has no `accounts` table and
`mailboxes.url` embeds only the UUID. The real mapping lives in macOS's
system-wide Internet Accounts store, `~/Library/Accounts/Accounts4.sqlite`
(also backs Calendar/Contacts/Messages, not Mail-specific), read here with
the same read-only/immutable discipline as envelope_reader.py.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cobos_apple_mail_mcp.storage.database import open_envelope_index_readonly

# Real accounts (verified) can be several ZPARENTACCOUNT hops deep; this
# bounds the walk against malformed/circular data rather than looping.
_MAX_PARENT_HOPS = 5

_REQUIRED_COLUMNS = {"Z_PK", "ZIDENTIFIER", "ZACCOUNTDESCRIPTION", "ZUSERNAME", "ZPARENTACCOUNT"}


def default_accounts_db_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "Accounts" / "Accounts4.sqlite"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {row["name"] for row in rows}


def resolve_account_names(accounts_db_path: Path | None = None) -> dict[str, str]:
    """Map Mail account UUID -> human display name/email, best-effort.

    `ZACCOUNT.ZIDENTIFIER` matches Mail's account UUID directory names
    directly (verified against a real Accounts4.sqlite), but many real
    accounts -- Gmail/Exchange-style, added via System Settings rather than
    directly in Mail -- carry an empty `ZACCOUNTDESCRIPTION`/`ZUSERNAME` on
    their own row and store the real display name/email on a
    `ZPARENTACCOUNT` ancestor instead (confirmed: 4 of 7 real accounts on
    the verification mailbox needed this walk). Returns `{}` on any missing
    file, schema mismatch, or permission error -- this is a supplementary
    lookup a caller falls back from, never a hard requirement, matching
    envelope_reader.py::read_envelope_flags()'s defensive style.
    """
    path = accounts_db_path or default_accounts_db_path()
    if not path.is_file():
        return {}

    try:
        conn = open_envelope_index_readonly(path)
        conn.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        return {}

    try:
        if not _REQUIRED_COLUMNS.issubset(_table_columns(conn, "ZACCOUNT")):
            return {}
        rows = conn.execute(
            "SELECT Z_PK, ZIDENTIFIER, ZACCOUNTDESCRIPTION, ZUSERNAME, ZPARENTACCOUNT "
            "FROM ZACCOUNT"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

    by_pk = {row["Z_PK"]: row for row in rows}

    def _display_name(pk: int) -> str | None:
        seen: set[int] = set()
        current = by_pk.get(pk)
        for _ in range(_MAX_PARENT_HOPS):
            if current is None or current["Z_PK"] in seen:
                return None
            seen.add(current["Z_PK"])
            # Real-world data has stray leading/trailing whitespace on
            # ZACCOUNTDESCRIPTION (verified: one real account's description
            # was literally " Account-C") -- strip before the truthiness check so
            # a whitespace-only description falls through to ZUSERNAME.
            description = (current["ZACCOUNTDESCRIPTION"] or "").strip()
            username = (current["ZUSERNAME"] or "").strip()
            name = description or username
            if name:
                return name
            parent_pk = current["ZPARENTACCOUNT"]
            if parent_pk is None:
                return None
            current = by_pk.get(parent_pk)
        return None

    resolved: dict[str, str] = {}
    for row in rows:
        identifier = row["ZIDENTIFIER"]
        if not identifier:
            continue
        name = _display_name(row["Z_PK"])
        if name:
            resolved[identifier] = name
    return resolved
