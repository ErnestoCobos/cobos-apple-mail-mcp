from __future__ import annotations

from cobos_apple_mail_mcp.core.models import SearchScope
from cobos_apple_mail_mcp.read.indexer import build_index
from cobos_apple_mail_mcp.read.search import FTS5Backend
from cobos_apple_mail_mcp.storage.database import connect_index
from tests.helpers import write_message


def _build(tmp_path):
    write_message(
        tmp_path,
        rowid=1,
        message_id="m1@example.com",
        subject="Quarterly budget review",
        sender="Alice Example <alice@example.com>",
        body="Please review the numbers by Friday.",
    )
    write_message(
        tmp_path,
        rowid=2,
        message_id="m2@example.com",
        subject="Lunch plans",
        sender="Bob Test <bob@example.com>",
        body="Want to grab lunch tomorrow? No budget talk I promise.",
    )
    conn = connect_index(":memory:")
    build_index(conn, tmp_path, full=True)
    return conn


def test_search_all_scope_ranks_subject_match_first(tmp_path):
    conn = _build(tmp_path)
    result = FTS5Backend(conn).search("budget")
    assert result.returned == 2
    assert result.hits[0].subject == "Quarterly budget review"
    assert result.total_estimated == 2


def test_search_subject_scope_excludes_body_only_match(tmp_path):
    conn = _build(tmp_path)
    result = FTS5Backend(conn).search("budget", scope=SearchScope.subject)
    assert result.returned == 1
    assert result.hits[0].subject == "Quarterly budget review"


def test_search_sender_scope(tmp_path):
    conn = _build(tmp_path)
    result = FTS5Backend(conn).search("alice", scope=SearchScope.sender)
    assert result.returned == 1
    assert result.hits[0].sender_addr == "alice@example.com"


def test_search_unread_only_filter(tmp_path):
    conn = _build(tmp_path)
    conn.execute("UPDATE emails SET flag_read = 1 WHERE message_id = 'm1@example.com'")
    conn.commit()
    result = FTS5Backend(conn).search("budget", unread_only=True)
    assert result.returned == 0


def test_search_highlight_snippet(tmp_path):
    conn = _build(tmp_path)
    result = FTS5Backend(conn).search("budget", highlight=True)
    assert any(h.snippet_html and "»" in h.snippet_html for h in result.hits)


def test_search_query_with_special_characters_does_not_raise(tmp_path):
    conn = _build(tmp_path)
    result = FTS5Backend(conn).search('budget* OR "lunch"')
    assert result.returned >= 0  # must not raise


def test_trigram_backend_returns_empty_when_table_missing(tmp_path):
    """enable_trigram=true but `index rebuild` hasn't run yet -- must
    degrade to no results, not crash with OperationalError."""
    from cobos_apple_mail_mcp.read.search import TrigramBackend

    conn = _build(tmp_path)
    assert TrigramBackend(conn).search("budget") == []
