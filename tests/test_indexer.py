from __future__ import annotations

import sqlite3
import time

from cobos_apple_mail_mcp.read.indexer import build_index, get_index_status, inventory_diff
from tests.helpers import write_message


def _conn() -> sqlite3.Connection:
    from cobos_apple_mail_mcp.storage.database import connect_index

    return connect_index(":memory:")


def test_build_index_adds_messages(tmp_path):
    write_message(tmp_path, rowid=1, message_id="m1@example.com", subject="Hello")
    write_message(tmp_path, rowid=2, message_id="m2@example.com", subject="World", mailbox="Sent")

    conn = _conn()
    result = build_index(conn, tmp_path, full=True)

    assert result.added == 2
    assert result.failed == 0
    # Disk iteration order (rglob) is not guaranteed, so sort by a stable
    # key rather than assuming insertion order.
    rows = conn.execute("SELECT subject, mailbox_name FROM emails ORDER BY subject").fetchall()
    assert [r["subject"] for r in rows] == ["Hello", "World"]
    assert {r["mailbox_name"] for r in rows} == {"INBOX", "Sent"}


def test_inventory_diff_detects_change_and_delete(tmp_path):
    path = write_message(tmp_path, rowid=1, message_id="m1@example.com", subject="Hello")
    conn = _conn()
    build_index(conn, tmp_path, full=True)

    diff = inventory_diff(conn, tmp_path)
    assert not diff.added and not diff.changed and not diff.deleted

    # Modify on disk -> should show as changed.
    time.sleep(0.01)
    path.write_bytes(path.read_bytes() + b" ")
    diff = inventory_diff(conn, tmp_path)
    assert len(diff.changed) == 1

    build_index(conn, tmp_path, full=True)
    path.unlink()
    diff = inventory_diff(conn, tmp_path)
    assert diff.deleted == [str(path)]


def test_inventory_diff_detects_move(tmp_path):
    path = write_message(tmp_path, rowid=1, message_id="m1@example.com", mailbox="INBOX")
    conn = _conn()
    build_index(conn, tmp_path, full=True)

    moved_dir = (
        tmp_path / "AAAAAAAA-1111-2222-3333-444444444444" / "Archive.mbox" / "0" / "0" / "Messages"
    )
    moved_dir.mkdir(parents=True, exist_ok=True)
    new_path = moved_dir / "1.emlx"
    new_path.write_bytes(path.read_bytes())
    path.unlink()

    diff = inventory_diff(conn, tmp_path)
    assert diff.added == [] and diff.deleted == []
    assert len(diff.moved) == 1
    old_path, entry = diff.moved[0]
    assert entry.mailbox_name == "Archive"

    build_index(conn, tmp_path, full=True)
    row = conn.execute("SELECT mailbox_name FROM emails WHERE emlx_rowid = 1").fetchone()
    assert row["mailbox_name"] == "Archive"


def test_failed_parse_goes_to_dead_letter(tmp_path):
    messages_dir = (
        tmp_path / "AAAAAAAA-1111-2222-3333-444444444444" / "INBOX.mbox" / "0" / "0" / "Messages"
    )
    messages_dir.mkdir(parents=True, exist_ok=True)
    (messages_dir / "1.emlx").write_bytes(b"not-a-number\nbroken")

    conn = _conn()
    result = build_index(conn, tmp_path, full=True)
    assert result.failed == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM failed_index_jobs").fetchone()["n"] == 1


def test_failed_then_recovered_path_clears_dead_letter(tmp_path):
    messages_dir = (
        tmp_path / "AAAAAAAA-1111-2222-3333-444444444444" / "INBOX.mbox" / "0" / "0" / "Messages"
    )
    messages_dir.mkdir(parents=True, exist_ok=True)
    path = messages_dir / "1.emlx"
    path.write_bytes(b"not-a-number\nbroken")

    conn = _conn()
    result = build_index(conn, tmp_path, full=True)
    assert result.failed == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM failed_index_jobs").fetchone()["n"] == 1

    # The file gets rewritten as valid mail on a later tick (e.g. Mail.app
    # finished a mid-write); the dead-letter entry must clear.
    from tests.helpers import build_emlx_bytes

    path.write_bytes(build_emlx_bytes(message_id="m1@example.com", subject="Now valid"))
    result = build_index(conn, tmp_path, full=True)
    assert result.failed == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM failed_index_jobs").fetchone()["n"] == 0


def test_trigram_table_populated_when_enabled(tmp_path):
    write_message(tmp_path, rowid=1, message_id="m1@example.com", subject="Invoice #12345")
    conn = _conn()

    # Off by default: no crash, table just doesn't exist / isn't queried.
    build_index(conn, tmp_path, full=True)
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "emails_trgm" not in tables

    build_index(conn, tmp_path, full=True, enable_trigram=True)
    count = conn.execute("SELECT COUNT(*) AS n FROM emails_trgm").fetchone()["n"]
    assert count == 1


def _accounts_db(dir_path, uuid, name):
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / "Accounts4.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE ZACCOUNT (Z_PK INTEGER PRIMARY KEY, ZIDENTIFIER VARCHAR, "
        "ZACCOUNTDESCRIPTION VARCHAR, ZUSERNAME VARCHAR, ZPARENTACCOUNT INTEGER)"
    )
    conn.execute(
        "INSERT INTO ZACCOUNT (Z_PK, ZIDENTIFIER, ZACCOUNTDESCRIPTION) VALUES (1, ?, ?)",
        (uuid, name),
    )
    conn.commit()
    conn.close()
    return path


def test_build_index_resolves_account_display_name(tmp_path):
    uuid = "AAAAAAAA-1111-2222-3333-444444444444"
    write_message(tmp_path, rowid=1, message_id="m1@example.com", account_uuid=uuid)
    accounts_db = _accounts_db(tmp_path / "acct", uuid, "Work")

    conn = _conn()
    build_index(conn, tmp_path, full=True, accounts_db_path=accounts_db)

    row = conn.execute("SELECT account_name FROM emails WHERE emlx_rowid = 1").fetchone()
    assert row["account_name"] == "Work"


def test_build_index_backfills_account_name_on_already_indexed_rows(tmp_path):
    # account-name resolution must not require a reparse: an index built
    # before Accounts4.sqlite was consulted (or before this feature existed)
    # should pick up real names on the very next build via a cheap UPDATE,
    # not require waiting for every message to individually change.
    uuid = "AAAAAAAA-1111-2222-3333-444444444444"
    write_message(tmp_path, rowid=1, message_id="m1@example.com", account_uuid=uuid)

    conn = _conn()
    build_index(conn, tmp_path, full=True, accounts_db_path=tmp_path / "nonexistent.sqlite")
    row = conn.execute("SELECT account_name FROM emails WHERE emlx_rowid = 1").fetchone()
    assert row["account_name"] == uuid  # no resolver data yet -> falls back to the UUID

    accounts_db = _accounts_db(tmp_path / "acct", uuid, "Work")
    build_index(conn, tmp_path, full=False, accounts_db_path=accounts_db)

    row = conn.execute("SELECT account_name FROM emails WHERE emlx_rowid = 1").fetchone()
    assert row["account_name"] == "Work"


def test_build_index_survives_unsanitized_surrogate_row(tmp_path, monkeypatch):
    """Defense in depth: even if some future parse path slips a lone
    surrogate past emlx_parser's sanitization (see test_emlx_parser.py for
    the primary fix), a single bad row must be dead-lettered, not crash the
    whole batch/build -- this is exactly what happened against a real
    209k-message mailbox before the fix (UnicodeEncodeError out of a bare
    executemany aborted the entire index build)."""
    write_message(tmp_path, rowid=1, message_id="m1@example.com", subject="Fine")
    write_message(tmp_path, rowid=2, message_id="m2@example.com", subject="Also fine")

    import cobos_apple_mail_mcp.read.indexer as indexer_mod

    real_parse = indexer_mod.parse_emlx_file

    def poisoned_parse(path):
        parsed = real_parse(path)
        if parsed is not None and parsed.rowid == 2:
            parsed.subject = "poisoned \udcff subject"
        return parsed

    monkeypatch.setattr(indexer_mod, "parse_emlx_file", poisoned_parse)

    conn = _conn()
    result = build_index(conn, tmp_path, full=True)

    assert result.added == 1
    assert result.failed == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM failed_index_jobs").fetchone()["n"] == 1
    row = conn.execute("SELECT subject FROM emails WHERE emlx_rowid = 1").fetchone()
    assert row["subject"] == "Fine"


def test_index_status_reports_pending_and_stale(tmp_path):
    write_message(tmp_path, rowid=1, message_id="m1@example.com")
    conn = _conn()

    status = get_index_status(conn, tmp_path)
    assert status.total_indexed == 0
    assert status.pending_added == 1
    assert status.stale is True

    build_index(conn, tmp_path, full=True)
    status = get_index_status(conn, tmp_path)
    assert status.total_indexed == 1
    assert status.pending_added == 0
    assert status.stale is False


def test_find_mail_directory_degrades_when_unreadable(tmp_path):
    """~/Library/Mail present but unreadable (the signature of missing Full Disk
    Access) must degrade to None rather than raise PermissionError — the MCP
    server has to start regardless, then report the problem via its tools."""
    import os
    import stat

    import pytest

    from cobos_apple_mail_mcp.read.envelope_reader import (
        find_mail_directory,
        library_mail_permission_denied,
    )

    library_mail = tmp_path / "Library" / "Mail"
    (library_mail / "V10").mkdir(parents=True)
    assert find_mail_directory(home=tmp_path) == library_mail / "V10"
    assert library_mail_permission_denied(home=tmp_path) is False

    os.chmod(library_mail, 0)
    try:
        if os.access(library_mail, os.R_OK):  # root ignores the mode bits
            pytest.skip("cannot simulate permission denial (running as root)")
        assert find_mail_directory(home=tmp_path) is None  # no raise
        assert library_mail_permission_denied(home=tmp_path) is True
    finally:
        os.chmod(library_mail, stat.S_IRWXU)
