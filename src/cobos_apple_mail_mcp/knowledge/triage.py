"""Awaiting-reply / needs-response triage heuristics, computed entirely
from `index.db` (CLAUDE.md knowledge map: Threading and knowledge).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time

from cobos_apple_mail_mcp.core.models import AwaitingReplyItem, MessageRefModel, NeedsResponseItem
from cobos_apple_mail_mcp.core.text import looks_like_noreply

_REQUEST_CUES = re.compile(
    r"\b(can you|could you|please|let me know|need|request|review|approve|confirm|deadline)\b",
    re.IGNORECASE,
)
_URGENCY_CUES = re.compile(r"\b(urgent|asap|eod|by end of day|by tomorrow)\b", re.IGNORECASE)
_ADDR_IN_ANGLE_BRACKETS = re.compile(r"<([^<>]+)>")


def _primary_recipient(row: sqlite3.Row) -> str | None:
    to_list = json.loads(row["recipients_to"]) if row["recipients_to"] else []
    if not to_list:
        return None
    primary = to_list[0]
    match = _ADDR_IN_ANGLE_BRACKETS.search(primary)
    return match.group(1) if match else primary


def _was_replied_to(conn: sqlite3.Connection, sent_row: sqlite3.Row, recipient_addr: str) -> bool:
    """Scoped to candidates from the recipient after the send date — cheap,
    and avoids a fragile substring LIKE on the references column (Python-side
    exact membership check on the parsed References list instead)."""
    candidates = conn.execute(
        """
        SELECT in_reply_to, references_ids, subject_norm
        FROM emails
        WHERE sender_addr = :recipient AND date_received > :sent_at
        """,
        {"recipient": recipient_addr, "sent_at": sent_row["date_sent"] or 0},
    ).fetchall()
    for candidate in candidates:
        if candidate["in_reply_to"] == sent_row["message_id"]:
            return True
        if sent_row["message_id"] in (candidate["references_ids"] or "").split():
            return True
        if candidate["subject_norm"] and candidate["subject_norm"] == sent_row["subject_norm"]:
            return True
    return False


def get_awaiting_reply(
    conn: sqlite3.Connection, *, days_back: int = 7, account: str | None = None
) -> list[AwaitingReplyItem]:
    since = int(time.time()) - days_back * 86400
    where = ["mailbox_role = 'sent'", "date_sent >= :since", "flag_bulk = 0"]
    params: dict[str, object] = {"since": since}
    if account:
        where.append("(account_uuid = :account OR account_name = :account)")
        params["account"] = account

    sent_rows = conn.execute(
        f"SELECT * FROM emails WHERE {' AND '.join(where)} ORDER BY date_sent DESC", params
    ).fetchall()

    items: list[AwaitingReplyItem] = []
    for row in sent_rows:
        recipient_addr = _primary_recipient(row)
        if not recipient_addr or looks_like_noreply(recipient_addr):
            continue
        if _was_replied_to(conn, row, recipient_addr):
            continue

        sent_at = row["date_sent"] or 0
        days_waiting = max(0.0, (time.time() - sent_at) / 86400)
        ref = MessageRefModel(
            message_id=row["message_id"],
            account=row["account_name"] or row["account_uuid"],
            mailbox=row["mailbox_name"],
        )
        items.append(
            AwaitingReplyItem(
                message_ref=ref,
                recipient=recipient_addr,
                subject=row["subject"],
                sent_at=sent_at,
                days_waiting=round(days_waiting, 1),
            )
        )

    items.sort(key=lambda i: i.days_waiting, reverse=True)
    return items[:20]


def get_needs_response(
    conn: sqlite3.Connection,
    *,
    days_back: int = 7,
    account: str | None = None,
    threshold: int = 4,
) -> list[NeedsResponseItem]:
    since = int(time.time()) - days_back * 86400
    where = [
        "mailbox_role = 'inbox'",
        "flag_read = 0",
        "flag_answered = 0",
        "flag_bulk = 0",
        "date_received >= :since",
    ]
    params: dict[str, object] = {"since": since}
    if account:
        where.append("(account_uuid = :account OR account_name = :account)")
        params["account"] = account

    rows = conn.execute(
        f"SELECT * FROM emails WHERE {' AND '.join(where)} ORDER BY date_received DESC", params
    ).fetchall()

    items: list[NeedsResponseItem] = []
    now = time.time()
    for row in rows:
        if looks_like_noreply(row["sender_addr"]):
            continue

        text = f"{row['subject'] or ''} {row['snippet'] or ''}"
        score = 0
        reasons: list[str] = []
        if "?" in text:
            score += 3
            reasons.append("contains a question")
        if _REQUEST_CUES.search(text):
            score += 2
            reasons.append("contains a request phrase")
        if _URGENCY_CUES.search(text) or row["flag_flagged"]:
            score += 3
            reasons.append("urgency cue or flagged")
        days_old = max(0.0, (now - (row["date_received"] or now)) / 86400)
        score += min(3, int(days_old))
        if days_old >= 1:
            reasons.append(f"unanswered for {int(days_old)}+ day(s)")

        if score < threshold:
            continue

        urgency = "HIGH" if score >= 7 else "MEDIUM" if score >= 5 else "NORMAL"
        ref = MessageRefModel(
            message_id=row["message_id"],
            account=row["account_name"] or row["account_uuid"],
            mailbox=row["mailbox_name"],
        )
        items.append(
            NeedsResponseItem(
                message_ref=ref,
                sender=row["sender_addr"],
                subject=row["subject"],
                score=score,
                urgency=urgency,
                reasons=reasons,
                received_at=row["date_received"] or 0,
            )
        )

    items.sort(key=lambda i: i.score, reverse=True)
    return items
