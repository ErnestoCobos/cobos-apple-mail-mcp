"""Shared `emails` row -> pydantic model mapping, used by both tools/reading.py
and knowledge/analytics.py|triage.py. Kept in `read/` (not `tools/`) so the
knowledge layer depends only on read/storage, never on the tools layer.
"""

from __future__ import annotations

import json
import sqlite3

from cobos_apple_mail_mcp.core.models import Attachment, EmailFull, EmailSummary, MessageRefModel
from cobos_apple_mail_mcp.read.emlx_parser import ParsedEmlx


def row_to_summary(row: sqlite3.Row) -> EmailSummary:
    account = row["account_name"] or row["account_uuid"]
    ref = MessageRefModel(
        message_id=row["message_id"], account=account, mailbox=row["mailbox_name"]
    )
    return EmailSummary(
        message_ref=ref,
        subject=row["subject"],
        sender_name=row["sender_name"],
        sender_addr=row["sender_addr"],
        date_received=row["date_received"],
        date_sent=row["date_sent"],
        is_read=bool(row["flag_read"]),
        is_flagged=bool(row["flag_flagged"]),
        is_answered=bool(row["flag_answered"]),
        attachment_count=row["attachment_count"],
        snippet=row["snippet"],
        mailbox=row["mailbox_name"],
        account=account,
    )


def row_to_full(row: sqlite3.Row, parsed: ParsedEmlx | None) -> EmailFull:
    base = row_to_summary(row)
    recipients_to = json.loads(row["recipients_to"]) if row["recipients_to"] else []
    recipients_cc = json.loads(row["recipients_cc"]) if row["recipients_cc"] else []

    if parsed is not None:
        attachments = [Attachment(filename=name) for name in parsed.attachment_names]
        body_html = parsed.body_html
    else:
        names = json.loads(row["attachment_names"]) if row["attachment_names"] else []
        attachments = [Attachment(filename=name) for name in names]
        body_html = None

    return EmailFull(
        **base.model_dump(),
        recipients_to=recipients_to,
        recipients_cc=recipients_cc,
        body_plain=row["body_plain"] or "",
        body_html=body_html,
        attachments=attachments,
        in_reply_to=row["in_reply_to"],
        references=row["references_ids"].split() if row["references_ids"] else [],
        headers={},
    )
