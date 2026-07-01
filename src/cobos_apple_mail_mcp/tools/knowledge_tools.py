"""get_inbox_overview, get_awaiting_reply, get_needs_response,
get_top_senders, get_statistics (CLAUDE.md knowledge map: Tools reference).
Thin wrappers over knowledge/analytics.py and knowledge/triage.py — all
computed from the index, never AppleScript.
"""

from __future__ import annotations

import sqlite3

from cobos_apple_mail_mcp.core.models import (
    AwaitingReplyItem,
    ContactSummary,
    InboxOverview,
    NeedsResponseItem,
    SenderCount,
    Statistics,
)
from cobos_apple_mail_mcp.knowledge import analytics, contacts, triage


def get_inbox_overview(conn: sqlite3.Connection, *, account: str | None = None) -> InboxOverview:
    return analytics.get_inbox_overview(conn, account=account)


def get_awaiting_reply(
    conn: sqlite3.Connection, *, days_back: int = 7, account: str | None = None
) -> list[AwaitingReplyItem]:
    return triage.get_awaiting_reply(conn, days_back=days_back, account=account)


def get_needs_response(
    conn: sqlite3.Connection, *, days_back: int = 7, account: str | None = None
) -> list[NeedsResponseItem]:
    return triage.get_needs_response(conn, days_back=days_back, account=account)


def get_top_senders(
    conn: sqlite3.Connection,
    *,
    account: str | None = None,
    mailbox: str | None = None,
    limit: int = 10,
) -> list[SenderCount]:
    return analytics.get_top_senders(conn, account=account, mailbox=mailbox, limit=limit)


def get_statistics(
    conn: sqlite3.Connection,
    *,
    scope: str = "account_overview",
    date_range_days: int = 30,
    account: str | None = None,
    sender: str | None = None,
) -> Statistics:
    return analytics.get_statistics(
        conn, scope=scope, date_range_days=date_range_days, account=account, sender=sender
    )


def list_contacts(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    account: str | None = None,
    limit: int = 25,
) -> list[ContactSummary]:
    return contacts.list_contacts(conn, query=query, account=account, limit=limit)
