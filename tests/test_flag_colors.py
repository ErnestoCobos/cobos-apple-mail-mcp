from __future__ import annotations

import time

import pytest

from cobos_apple_mail_mcp.core.flags import color_to_index, index_to_color
from cobos_apple_mail_mcp.read.indexer import build_index
from cobos_apple_mail_mcp.read.search import FTS5Backend
from cobos_apple_mail_mcp.storage.database import connect_index
from cobos_apple_mail_mcp.tools.reading import get_emails
from tests.helpers import write_message


def test_color_index_round_trip():
    assert color_to_index("red") == 0
    assert color_to_index("GREEN") == 3  # case-insensitive
    assert index_to_color(3) == "green"
    assert index_to_color(0) == "red"
    # Unflagged / out of range -> None, never raises.
    assert index_to_color(None) is None
    assert index_to_color(-1) is None
    assert index_to_color(99) is None
    with pytest.raises(ValueError):
        color_to_index("chartreuse")


def _set_color(conn, message_id, index):
    """Simulate what update_email_status's optimistic index update does."""
    conn.execute(
        "UPDATE emails SET flag_color = ?, flag_flagged = 1 WHERE message_id = ?",
        (index, message_id),
    )
    conn.commit()


def test_flag_color_is_preserved_across_reindex(tmp_path):
    # flag_color is only ever set by our own set_flag_color write; the on-disk
    # Envelope Index doesn't carry the per-color flagIndex (it stores 1 for any
    # flagged message), so a reindex must NOT wipe an optimistically-set color.
    path = write_message(tmp_path, rowid=1, message_id="m1@x.com", subject="Report")
    conn = connect_index(":memory:")
    build_index(conn, tmp_path, full=True)
    assert conn.execute("SELECT flag_color FROM emails WHERE emlx_rowid=1").fetchone()[0] is None

    _set_color(conn, "m1@x.com", 3)  # green, as set_flag_color would

    # Reindex the (changed) message; the color must survive.
    time.sleep(0.01)
    path.write_bytes(path.read_bytes() + b" ")
    build_index(conn, tmp_path, full=False)
    assert conn.execute("SELECT flag_color FROM emails WHERE emlx_rowid=1").fetchone()[0] == 3


def test_new_rows_start_uncolored(tmp_path):
    write_message(tmp_path, rowid=1, message_id="m1@x.com")
    conn = connect_index(":memory:")
    build_index(conn, tmp_path, full=True)
    assert conn.execute("SELECT flag_color FROM emails WHERE emlx_rowid=1").fetchone()[0] is None


def test_search_filters_by_flag_color(tmp_path):
    write_message(tmp_path, rowid=1, message_id="m1@x.com", subject="quarterly report")
    write_message(tmp_path, rowid=2, message_id="m2@x.com", subject="quarterly review")
    conn = connect_index(":memory:")
    build_index(conn, tmp_path, full=True)
    _set_color(conn, "m1@x.com", 3)  # green
    # emails_fts is repopulated on full build; flag_color is a plain WHERE on
    # the joined emails row, so the optimistic UPDATE is enough (no FTS change).

    all_hits = FTS5Backend(conn).search("quarterly")
    assert all_hits.returned == 2

    green = FTS5Backend(conn).search("quarterly", flag_color="green")
    assert green.returned == 1
    assert green.hits[0].message_ref.message_id == "m1@x.com"
    assert green.hits[0].flag_color == "green"


def test_get_emails_filters_by_flag_color(tmp_path):
    write_message(tmp_path, rowid=1, message_id="m1@x.com", mailbox="INBOX")
    write_message(tmp_path, rowid=2, message_id="m2@x.com", mailbox="INBOX")
    conn = connect_index(":memory:")
    build_index(conn, tmp_path, full=True)
    _set_color(conn, "m1@x.com", 0)  # red

    red = get_emails(conn, flag_color="red")
    assert [e.message_ref.message_id for e in red] == ["m1@x.com"]
    assert red[0].flag_color == "red"
