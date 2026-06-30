from __future__ import annotations

from cobos_apple_mail_mcp.knowledge.analytics import (
    get_inbox_overview,
    get_statistics,
    get_top_senders,
)
from cobos_apple_mail_mcp.read.emlx_parser import FLAG_SEEN
from cobos_apple_mail_mcp.read.indexer import build_index
from cobos_apple_mail_mcp.storage.database import connect_index
from tests.helpers import write_message


def _conn():
    return connect_index(":memory:")


def _seed(tmp_path):
    write_message(
        tmp_path,
        rowid=1,
        message_id="m1@x.com",
        sender="Alice <alice@example.com>",
        mailbox="INBOX",
        flags=0,
    )
    write_message(
        tmp_path,
        rowid=2,
        message_id="m2@x.com",
        sender="Alice <alice@example.com>",
        mailbox="INBOX",
        flags=FLAG_SEEN,
    )
    write_message(
        tmp_path,
        rowid=3,
        message_id="m3@x.com",
        sender="Bob <bob@example.com>",
        mailbox="INBOX",
        flags=0,
    )
    conn = _conn()
    build_index(conn, tmp_path, full=True)
    return conn


def test_top_senders_ranks_by_volume(tmp_path):
    conn = _seed(tmp_path)
    senders = get_top_senders(conn, limit=5)
    assert senders[0].sender_addr == "alice@example.com"
    assert senders[0].count == 2
    assert senders[0].unread_count == 1


def test_inbox_overview_counts(tmp_path):
    conn = _seed(tmp_path)
    overview = get_inbox_overview(conn)
    assert overview.total == 3
    assert overview.unread == 2


def test_statistics_account_overview(tmp_path):
    conn = _seed(tmp_path)
    stats = get_statistics(conn, scope="account_overview", date_range_days=36500)
    assert stats.data["total"] == 3
