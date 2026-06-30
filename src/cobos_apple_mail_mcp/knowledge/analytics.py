"""Inbox analytics — get_inbox_overview, get_top_senders, get_statistics.
Computed entirely from `index.db` (CLAUDE.md knowledge map: Threading and
knowledge); never via AppleScript, which would be ~1000x slower for the
same aggregate queries.
"""

from __future__ import annotations

import sqlite3
import time

from cobos_apple_mail_mcp.core.models import AccountCount, InboxOverview, SenderCount, Statistics
from cobos_apple_mail_mcp.read.rowmap import row_to_summary


def get_top_senders(
    conn: sqlite3.Connection,
    *,
    account: str | None = None,
    mailbox: str | None = None,
    since: int | None = None,
    limit: int = 10,
) -> list[SenderCount]:
    where = ["sender_addr IS NOT NULL"]
    params: dict[str, object] = {"limit": limit}
    if account:
        where.append("(account_uuid = :account OR account_name = :account)")
        params["account"] = account
    if mailbox:
        where.append("(mailbox_name = :mailbox OR mailbox_role = :mailbox)")
        params["mailbox"] = mailbox
    if since is not None:
        where.append("date_received >= :since")
        params["since"] = since

    where_sql = " AND ".join(where)
    rows = conn.execute(
        f"""
        SELECT sender_addr, sender_name,
               COUNT(*) AS n,
               SUM(CASE WHEN flag_read = 0 THEN 1 ELSE 0 END) AS unread,
               MAX(date_received) AS last_received
        FROM emails WHERE {where_sql}
        GROUP BY sender_addr
        ORDER BY n DESC
        LIMIT :limit
        """,
        params,
    ).fetchall()
    return [
        SenderCount(
            sender_addr=r["sender_addr"],
            sender_name=r["sender_name"],
            count=r["n"],
            unread_count=r["unread"] or 0,
            last_received=r["last_received"],
        )
        for r in rows
    ]


def get_inbox_overview(conn: sqlite3.Connection, *, account: str | None = None) -> InboxOverview:
    where = ["mailbox_role = 'inbox'"]
    params: dict[str, object] = {}
    if account:
        where.append("(account_uuid = :account OR account_name = :account)")
        params["account"] = account
    where_sql = " AND ".join(where)

    now = int(time.time())
    today_start = now - (now % 86400)
    week_start = now - 7 * 86400

    counts = conn.execute(
        f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN flag_read = 0 THEN 1 ELSE 0 END) AS unread,
               SUM(CASE WHEN flag_flagged = 1 THEN 1 ELSE 0 END) AS flagged,
               SUM(CASE WHEN date_received >= :today THEN 1 ELSE 0 END) AS today,
               SUM(CASE WHEN date_received >= :week THEN 1 ELSE 0 END) AS this_week
        FROM emails WHERE {where_sql}
        """,
        {**params, "today": today_start, "week": week_start},
    ).fetchone()

    top_senders = get_top_senders(conn, account=account, mailbox="inbox", limit=5)
    unread_senders = [s for s in top_senders if s.unread_count > 0]

    newest_unread_rows = conn.execute(
        f"SELECT * FROM emails WHERE {where_sql} AND flag_read = 0 "
        f"ORDER BY date_received DESC LIMIT 10",
        params,
    ).fetchall()

    by_account_rows = conn.execute(
        """
        SELECT account_uuid, account_name, COUNT(*) AS total,
               SUM(CASE WHEN flag_read = 0 THEN 1 ELSE 0 END) AS unread
        FROM emails WHERE mailbox_role = 'inbox'
        GROUP BY account_uuid
        """
    ).fetchall()

    from cobos_apple_mail_mcp.knowledge.triage import get_awaiting_reply, get_needs_response

    return InboxOverview(
        total=counts["total"] or 0,
        unread=counts["unread"] or 0,
        flagged=counts["flagged"] or 0,
        today=counts["today"] or 0,
        this_week=counts["this_week"] or 0,
        top_unread_senders=unread_senders or top_senders,
        needs_response_count=len(get_needs_response(conn, account=account)),
        awaiting_reply_count=len(get_awaiting_reply(conn, account=account)),
        newest_unread=[row_to_summary(r) for r in newest_unread_rows],
        by_account=[
            AccountCount(
                account=r["account_name"] or r["account_uuid"],
                total=r["total"],
                unread=r["unread"] or 0,
            )
            for r in by_account_rows
        ],
    )


def get_statistics(
    conn: sqlite3.Connection,
    *,
    scope: str = "account_overview",
    date_range_days: int = 30,
    account: str | None = None,
    sender: str | None = None,
) -> Statistics:
    since = int(time.time()) - date_range_days * 86400
    data: dict[str, object]

    if scope == "account_overview":
        where = ["date_received >= :since"]
        params: dict[str, object] = {"since": since}
        if account:
            where.append("(account_uuid = :account OR account_name = :account)")
            params["account"] = account
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN flag_read = 1 THEN 1 ELSE 0 END) AS read_count,
                   SUM(CASE WHEN flag_flagged = 1 THEN 1 ELSE 0 END) AS flagged_count,
                   SUM(CASE WHEN mailbox_role = 'sent' THEN 1 ELSE 0 END) AS sent_count,
                   SUM(attachment_count) AS attachment_total
            FROM emails WHERE {' AND '.join(where)}
            """,
            params,
        ).fetchone()
        total = row["total"] or 0
        data = {
            "total": total,
            "read_pct": round(100 * (row["read_count"] or 0) / total, 1) if total else 0.0,
            "flagged": row["flagged_count"] or 0,
            "sent": row["sent_count"] or 0,
            "attachment_total": row["attachment_total"] or 0,
        }
    elif scope == "sender_stats":
        if not sender:
            raise ValueError("scope='sender_stats' requires a sender address")
        row = conn.execute(
            """
            SELECT COUNT(*) AS total, SUM(attachment_count) AS attachments
            FROM emails WHERE sender_addr = ? AND date_received >= ?
            """,
            (sender, since),
        ).fetchone()
        data = {
            "sender": sender,
            "total": row["total"] or 0,
            "attachments": row["attachments"] or 0,
        }
    elif scope == "mailbox_breakdown":
        rows = conn.execute(
            """
            SELECT mailbox_name, COUNT(*) AS total,
                   SUM(CASE WHEN flag_read = 0 THEN 1 ELSE 0 END) AS unread
            FROM emails WHERE date_received >= ?
            GROUP BY mailbox_name ORDER BY total DESC
            """,
            (since,),
        ).fetchall()
        data = {
            "mailboxes": [
                {"name": r["mailbox_name"], "total": r["total"], "unread": r["unread"] or 0}
                for r in rows
            ]
        }
    else:
        raise ValueError(f"unknown statistics scope: {scope!r}")

    return Statistics(scope=scope, date_range_days=date_range_days, data=data)
