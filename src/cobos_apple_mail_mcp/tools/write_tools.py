"""Write tools: compose/reply/forward/drafts/rich-draft/move/status/
mailbox/trash/attach + undo_last (CLAUDE.md knowledge map: Tools
reference). Thin wrappers — all batch mutations go through
core.safety.guard() inside write/organize.py; compose/drafts gate
read-only directly since they're single-target, not batch, operations.
"""

from __future__ import annotations

import sqlite3

from cobos_apple_mail_mcp.config import Config
from cobos_apple_mail_mcp.core.models import (
    Attachment,
    OperationResult,
    Rule,
    UndoResult,
    UnsubscribeResult,
)
from cobos_apple_mail_mcp.core.undo import undo_last as _undo_last
from cobos_apple_mail_mcp.write import attachments, compose, drafts, organize, rules, unsubscribe
from cobos_apple_mail_mcp.write.jxa_executor import JXAExecutor


def compose_email(conn: sqlite3.Connection, jxa: JXAExecutor, config: Config, **kwargs) -> dict:
    return compose.compose_email(conn, jxa, config, **kwargs)


def unsubscribe_from_sender(
    conn: sqlite3.Connection, jxa: JXAExecutor, config: Config, message_id: str, **kwargs
) -> UnsubscribeResult:
    return unsubscribe.unsubscribe_from_sender(conn, jxa, config, message_id, **kwargs)


def list_rules(jxa: JXAExecutor) -> list[Rule]:
    return rules.list_rules(jxa)


def set_rule_enabled(
    jxa: JXAExecutor, config: Config, name: str, enabled: bool, **kwargs
) -> dict:
    return rules.set_rule_enabled(jxa, config, name, enabled, **kwargs)


def delete_rule(jxa: JXAExecutor, config: Config, name: str, **kwargs) -> dict:
    return rules.delete_rule(jxa, config, name, **kwargs)


def reply_to_email(
    conn: sqlite3.Connection, jxa: JXAExecutor, config: Config, message_id: str, **kwargs
) -> dict:
    return compose.reply_to_email(conn, jxa, config, message_id, **kwargs)


def forward_email(
    conn: sqlite3.Connection, jxa: JXAExecutor, config: Config, message_id: str, **kwargs
) -> dict:
    return compose.forward_email(conn, jxa, config, message_id, **kwargs)


def create_rich_email_draft(
    conn: sqlite3.Connection, jxa: JXAExecutor, config: Config, **kwargs
) -> dict:
    return compose.create_rich_email_draft(conn, jxa, config, **kwargs)


def manage_drafts(conn: sqlite3.Connection, jxa: JXAExecutor, config: Config, **kwargs) -> dict:
    return drafts.manage_drafts(conn, jxa, config, **kwargs)


def move_email(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    config: Config,
    message_ids: list[str],
    to_mailbox: str,
    **kwargs,
) -> OperationResult:
    return organize.move_email(conn, jxa, config, message_ids, to_mailbox, **kwargs)


def update_email_status(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    config: Config,
    message_ids: list[str],
    action: str,
    **kwargs,
) -> OperationResult:
    return organize.update_email_status(conn, jxa, config, message_ids, action, **kwargs)


def create_mailbox(conn: sqlite3.Connection, jxa: JXAExecutor, config: Config, **kwargs) -> dict:
    return organize.create_mailbox(conn, jxa, config, **kwargs)


def manage_trash(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    config: Config,
    action: str,
    *,
    account: str,
    message_ids: list[str] | None = None,
    mailbox: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    max_deletes: int | None = None,
) -> OperationResult:
    """action: move_to_trash|delete_permanent|empty_trash. empty_trash acts
    on the whole Trash mailbox and so doesn't take message_ids."""
    if action == "empty_trash":
        return organize.empty_trash(
            conn, jxa, config, account=account, dry_run=dry_run, confirm=confirm
        )
    return organize.manage_trash(
        conn,
        jxa,
        config,
        action,
        message_ids or [],
        account=account,
        mailbox=mailbox,
        dry_run=dry_run,
        confirm=confirm,
        max_deletes=max_deletes,
    )


def save_email_attachment(
    conn: sqlite3.Connection, message_id: str, attachment_name: str, save_path: str, **kwargs
) -> Attachment:
    return attachments.save_email_attachment(conn, message_id, attachment_name, save_path, **kwargs)


def undo_last(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    *,
    batch_id: str | None = None,
    dry_run: bool = False,
) -> UndoResult:
    return _undo_last(conn, jxa, batch_id=batch_id, dry_run=dry_run)
