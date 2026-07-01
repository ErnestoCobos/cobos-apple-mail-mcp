from __future__ import annotations

import sqlite3
from pathlib import Path

from cobos_apple_mail_mcp.read.account_names import resolve_account_names

_SCHEMA = """
CREATE TABLE ZACCOUNT (
  Z_PK INTEGER PRIMARY KEY,
  ZIDENTIFIER VARCHAR,
  ZACCOUNTDESCRIPTION VARCHAR,
  ZUSERNAME VARCHAR,
  ZPARENTACCOUNT INTEGER
)
"""


def _build_fixture(tmp_path, rows):
    path = tmp_path / "Accounts4.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(_SCHEMA)
    conn.executemany(
        "INSERT INTO ZACCOUNT (Z_PK, ZIDENTIFIER, ZACCOUNTDESCRIPTION, ZUSERNAME, ZPARENTACCOUNT) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def test_missing_file_returns_empty():
    assert resolve_account_names(Path("/nonexistent/Accounts4.sqlite")) == {}


def test_resolves_direct_description(tmp_path):
    path = _build_fixture(
        tmp_path,
        [(1, "UUID-DIRECT", "Work", "user@example.com", None)],
    )
    assert resolve_account_names(path) == {"UUID-DIRECT": "Work"}


def test_falls_back_to_username_when_no_description(tmp_path):
    path = _build_fixture(
        tmp_path,
        [(1, "UUID-USER-ONLY", None, "user@example.com", None)],
    )
    assert resolve_account_names(path) == {"UUID-USER-ONLY": "user@example.com"}


def test_resolves_via_parent_chain(tmp_path):
    # Mirrors what a real Accounts4.sqlite looks like for Gmail/Exchange-style
    # accounts added via System Settings: the row matching Mail's UUID has
    # empty description/username, and the real display name lives on a
    # ZPARENTACCOUNT ancestor -- verified against a real 7-account mailbox,
    # 4 of 7 accounts needed exactly this walk.
    path = _build_fixture(
        tmp_path,
        [
            (1, "PARENT-UUID", "Personal", "me@gmail.com", None),
            (2, "CHILD-UUID-MATCHING-MAIL", None, None, 1),
        ],
    )
    assert resolve_account_names(path) == {
        "PARENT-UUID": "Personal",
        "CHILD-UUID-MATCHING-MAIL": "Personal",
    }


def test_cycle_guard_does_not_hang(tmp_path):
    # Malformed/circular ZPARENTACCOUNT data must not infinite-loop.
    path = _build_fixture(
        tmp_path,
        [
            (1, "UUID-A", None, None, 2),
            (2, "UUID-B", None, None, 1),
        ],
    )
    assert resolve_account_names(path) == {}


def test_missing_columns_returns_empty(tmp_path):
    path = tmp_path / "Accounts4.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ZACCOUNT (Z_PK INTEGER PRIMARY KEY, SOMETHING_ELSE TEXT)")
    conn.commit()
    conn.close()
    assert resolve_account_names(path) == {}


def test_rows_with_no_identifier_are_skipped(tmp_path):
    path = _build_fixture(
        tmp_path,
        [(1, None, "Orphan Description", None, None)],
    )
    assert resolve_account_names(path) == {}
