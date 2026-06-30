"""search, get_email_thread (CLAUDE.md knowledge map: Tools reference)."""

from __future__ import annotations

import sqlite3

from cobos_apple_mail_mcp.config import Config
from cobos_apple_mail_mcp.core.models import EmailThread, SearchMode, SearchResult, SearchScope
from cobos_apple_mail_mcp.read.search import FTS5Backend, TrigramBackend, looks_substring_query
from cobos_apple_mail_mcp.read.threader import get_email_thread as _get_email_thread


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    scope: SearchScope = SearchScope.all,
    mode: SearchMode = SearchMode.keyword,
    account: str | None = None,
    mailbox: str | None = None,
    before: int | None = None,
    after: int | None = None,
    unread_only: bool = False,
    flagged_only: bool = False,
    has_attachments: bool | None = None,
    limit: int = 25,
    offset: int = 0,
    highlight: bool = True,
    enable_trigram: bool = False,
    config: Config | None = None,
) -> SearchResult:
    if mode != SearchMode.keyword and config is not None:
        from cobos_apple_mail_mcp.read.vector_search import (
            get_backend,
            hybrid_search,
            semantic_search,
        )

        backend = get_backend(config)
        if backend is not None:
            if mode == SearchMode.semantic:
                return semantic_search(conn, backend, query, limit=limit, offset=offset)
            return hybrid_search(
                conn,
                backend,
                query,
                scope=scope,
                account=account,
                mailbox=mailbox,
                before=before,
                after=after,
                unread_only=unread_only,
                flagged_only=flagged_only,
                has_attachments=has_attachments,
                limit=limit,
                offset=offset,
                highlight=highlight,
            )

    result = FTS5Backend(conn).search(
        query,
        scope=scope,
        account=account,
        mailbox=mailbox,
        before=before,
        after=after,
        unread_only=unread_only,
        flagged_only=flagged_only,
        has_attachments=has_attachments,
        limit=limit,
        offset=offset,
        highlight=highlight,
    )
    if result.returned == 0 and enable_trigram and looks_substring_query(query):
        hits = TrigramBackend(conn).search(query, limit=limit)
        if hits:
            result.hits = hits
            result.returned = len(hits)
            result.notes.append(
                "keyword search returned no hits; fell back to trigram substring match"
            )
    if mode != SearchMode.keyword:
        result.mode = mode
        result.degraded = True
        result.notes.append(
            f"mode={mode.value!r} requested but semantic search is not enabled or unavailable; "
            "ran keyword search"
        )
    return result


def get_email_thread(
    conn: sqlite3.Connection, *, message_id: str | None = None, thread_id: int | None = None
) -> EmailThread:
    return _get_email_thread(conn, message_id=message_id, thread_id=thread_id)
