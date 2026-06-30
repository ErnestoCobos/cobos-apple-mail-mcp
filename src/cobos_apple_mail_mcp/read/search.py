"""Search engine: a `SearchBackend` interface with an `FTS5Backend` default
implementation (CLAUDE.md knowledge map: Search).

FTS5 was chosen deliberately for this architecture — embedded, no daemon,
hybrid-ready alongside `sqlite-vec`, trivial incremental updates, sub-100ms
to roughly a million emails. The interface seam exists so a heavier engine
(e.g. Tantivy) could slot in later if ever proven a bottleneck; nothing in
`tools/search_tools.py` should depend on FTS5 specifics.
"""

from __future__ import annotations

import sqlite3
import time
from abc import ABC, abstractmethod

from cobos_apple_mail_mcp.core.models import (
    MessageRefModel,
    SearchHit,
    SearchMode,
    SearchResult,
    SearchScope,
)
from cobos_apple_mail_mcp.core.text import sanitize_fts_query

# subject, sender, recipients, body, attachments — subject/sender weighted
# well above body so a name/title match outranks an incidental body mention.
_BM25_WEIGHTS = "10.0, 8.0, 4.0, 1.0, 3.0"

_SCOPE_COLUMN: dict[SearchScope, str] = {
    SearchScope.subject: "subject",
    SearchScope.sender: "sender",
    SearchScope.body: "body",
    SearchScope.attachments: "attachments",
}


class SearchBackend(ABC):
    """Seam between `tools/search_tools.py` and the concrete search engine."""

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        scope: SearchScope = SearchScope.all,
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
    ) -> SearchResult: ...


def summary_to_hit(
    summary, score: float, *, snippet_html: str | None = None, thread_id: int | None = None
) -> SearchHit:
    """Build a SearchHit from an EmailSummary (read/rowmap.py) — used by the
    optional vector/hybrid path (read/vector_search.py), whose candidate
    rows come straight from `emails`, not joined with `emails_fts`, so they
    have no bm25 score or FTS snippet() of their own.
    """
    return SearchHit(
        message_ref=summary.message_ref,
        score=score,
        subject=summary.subject,
        sender_name=summary.sender_name,
        sender_addr=summary.sender_addr,
        date_received=summary.date_received,
        mailbox=summary.mailbox,
        account=summary.account,
        is_read=summary.is_read,
        is_flagged=summary.is_flagged,
        attachment_count=summary.attachment_count,
        snippet_html=snippet_html or summary.snippet,
        thread_id=thread_id,
    )


def _row_to_hit(row: sqlite3.Row) -> SearchHit:
    account = row["account_name"] or row["account_uuid"]
    return SearchHit(
        message_ref=MessageRefModel(
            message_id=row["message_id"], account=account, mailbox=row["mailbox_name"]
        ),
        score=-row["score"],  # bm25() is negative; flip so higher = better.
        subject=row["subject"],
        sender_name=row["sender_name"],
        sender_addr=row["sender_addr"],
        date_received=row["date_received"],
        mailbox=row["mailbox_name"],
        account=account,
        is_read=bool(row["flag_read"]),
        is_flagged=bool(row["flag_flagged"]),
        attachment_count=row["attachment_count"],
        snippet_html=row["snip"],
        thread_id=row["thread_id"],
    )


class FTS5Backend(SearchBackend):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def search(
        self,
        query: str,
        *,
        scope: SearchScope = SearchScope.all,
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
    ) -> SearchResult:
        start = time.monotonic()
        sanitized = sanitize_fts_query(query)
        column = _SCOPE_COLUMN.get(scope)
        match_expr = f"{column}:({sanitized})" if column else sanitized

        # Filters are always plain SQL WHERE clauses, never folded into the
        # MATCH expression — keeps user input out of FTS5 query syntax.
        where = ["emails_fts MATCH :match"]
        params: dict[str, object] = {"match": match_expr, "limit": limit, "offset": offset}
        if account:
            where.append("(e.account_uuid = :account OR e.account_name = :account)")
            params["account"] = account
        if mailbox:
            where.append("(e.mailbox_name = :mailbox OR e.mailbox_role = :mailbox)")
            params["mailbox"] = mailbox
        if before is not None:
            where.append("e.date_received <= :before")
            params["before"] = before
        if after is not None:
            where.append("e.date_received >= :after")
            params["after"] = after
        if unread_only:
            where.append("e.flag_read = 0")
        if flagged_only:
            where.append("e.flag_flagged = 1")
        if has_attachments is True:
            where.append("e.attachment_count > 0")
        elif has_attachments is False:
            where.append("e.attachment_count = 0")

        where_sql = " AND ".join(where)
        snippet_sql = (
            "snippet(emails_fts, 3, '»', '«', '…', 12) AS snip"
            if highlight
            else "NULL AS snip"
        )

        rows = self._conn.execute(
            f"""
            SELECT e.*, bm25(emails_fts, {_BM25_WEIGHTS}) AS score, {snippet_sql}
            FROM emails_fts
            JOIN emails e ON e.id = emails_fts.rowid
            WHERE {where_sql}
            ORDER BY score
            LIMIT :limit OFFSET :offset
            """,
            params,
        ).fetchall()

        count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
        total = self._conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM emails_fts JOIN emails e ON e.id = emails_fts.rowid
            WHERE {where_sql}
            """,
            count_params,
        ).fetchone()["n"]

        hits = [_row_to_hit(row) for row in rows]
        return SearchResult(
            query=query,
            mode=SearchMode.keyword,
            scope=scope,
            total_estimated=total,
            returned=len(hits),
            offset=offset,
            hits=hits,
            timing_ms=(time.monotonic() - start) * 1000,
        )


class TrigramBackend:
    """Optional substring-search companion (config: index.enable_trigram).
    Routed to by `tools/search_tools.py` for substring-y queries (partial
    filenames, `@domain` fragments) when the porter-stemmed FTS5Backend
    returns too few hits. Same query shape as FTS5Backend, against the
    `emails_trgm` table created by storage.migrations.ensure_trigram_table.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def search(self, query: str, *, limit: int = 25) -> list[SearchHit]:
        sanitized = sanitize_fts_query(query)
        try:
            rows = self._conn.execute(
                """
                SELECT e.*, NULL AS score, NULL AS snip
                FROM emails_trgm
                JOIN emails e ON e.id = emails_trgm.rowid
                WHERE emails_trgm MATCH :match
                LIMIT :limit
                """,
                {"match": sanitized, "limit": limit},
            ).fetchall()
        except sqlite3.OperationalError:
            # enable_trigram was turned on but `index rebuild` hasn't run
            # yet (the table is only (re)built on a full rebuild) -- no
            # crash, just no substring fallback this time.
            return []
        hits = []
        for row in rows:
            hit = _row_to_hit(row)
            hit.score = 0.0
            hits.append(hit)
        return hits


def looks_substring_query(query: str) -> bool:
    """Heuristic: queries with no spaces but punctuation (an address
    fragment, a partial filename) usually want substring matching, not
    porter-stemmed keyword matching.
    """
    stripped = query.strip()
    if " " in stripped or not stripped:
        return False
    return any(ch in stripped for ch in ".@_-") and not stripped.endswith("*")
