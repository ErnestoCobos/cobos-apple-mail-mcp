"""move_email, update_email_status, create_mailbox, manage_trash — every
mutation goes through core.safety.guard() (CLAUDE.md invariant #2).
Messages are located by canonical message_id via core.resolver.resolve()
before any mutation (CLAUDE.md invariant #1); never by subject matching.
"""

from __future__ import annotations

import sqlite3

from cobos_apple_mail_mcp.config import Config
from cobos_apple_mail_mcp.core.errors import AppleMailMCPError, ReadOnlyMode
from cobos_apple_mail_mcp.core.identity import normalize_message_id, to_mail_message_id
from cobos_apple_mail_mcp.core.models import (
    AffectedMessage,
    MessageRefModel,
    OperationResult,
    Preview,
)
from cobos_apple_mail_mcp.core.resolver import ResolvedMessage, resolve
from cobos_apple_mail_mcp.core.safety import guard
from cobos_apple_mail_mcp.write.jxa_executor import JXAExecutor

_STATUS_ACTIONS = ("mark_read", "mark_unread", "flag", "unflag")
_TRASH_ACTIONS = ("move_to_trash", "delete_permanent")

_BATCH_OPERATION_KIND = {"move": "move", "status": "status", "trash": "trash", "delete": "delete"}


def _require_writable(config: Config, operation_kind: str, operation: str) -> None:
    """Fail fast on --read-only BEFORE any resolution is attempted — a
    blocked write must never touch JXA/Mail.app at all (CLAUDE.md invariant
    #4: never hang, never do needless external work). guard() re-checks
    this too; this early check just avoids the resolve() round-trips that
    would otherwise run first for no reason.
    """
    if config.server.read_only and _BATCH_OPERATION_KIND.get(operation_kind) is not None:
        raise ReadOnlyMode(f"server is running --read-only; {operation!r} is disabled")


def _resolve_many(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    message_ids: list[str],
    *,
    account: str | None,
    mailbox: str | None,
) -> tuple[list[ResolvedMessage], dict[str, str]]:
    resolved: list[ResolvedMessage] = []
    errors: dict[str, str] = {}
    for raw_id in message_ids:
        mid = normalize_message_id(raw_id)
        try:
            resolved.append(resolve(conn, jxa, mid, account_hint=account, mailbox_hint=mailbox))
        except AppleMailMCPError as exc:
            errors[mid] = str(exc)
    return resolved, errors


def move_email(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    config: Config,
    message_ids: list[str],
    to_mailbox: str,
    *,
    account: str | None = None,
    mailbox: str | None = None,
    dry_run: bool = False,
    max_moves: int | None = None,
) -> OperationResult:
    _require_writable(config, "move", "move_email")
    resolved, errors = _resolve_many(conn, jxa, message_ids, account=account, mailbox=mailbox)

    def preview(r: ResolvedMessage) -> AffectedMessage:
        ref = MessageRefModel(
            message_id=r.canonical_id, account=r.account_name, mailbox=r.mailbox_name
        )
        return AffectedMessage(message_ref=ref, from_mailbox=r.mailbox_name, to_mailbox=to_mailbox)

    def apply(r: ResolvedMessage) -> None:
        jxa.call(
            "moveEmail",
            {
                "accountHint": r.account_name,
                "mailboxHint": r.mailbox_name,
                "messageId": to_mail_message_id(r.canonical_id),
                "toMailbox": to_mailbox,
            },
        )

    def undo_record(r: ResolvedMessage) -> dict:
        return {
            "operation": "move",
            "account_name": r.account_name,
            "from_mailbox": r.mailbox_name,
            "to_mailbox": to_mailbox,
        }

    return guard(
        config=config,
        conn=conn,
        operation="move_email",
        operation_kind="move",
        resolved=resolved,
        errors=errors,
        preview_fn=preview,
        apply_fn=apply,
        undo_record_fn=undo_record,
        dry_run=dry_run,
        max_override=max_moves,
    )


def update_email_status(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    config: Config,
    message_ids: list[str],
    action: str,
    *,
    account: str | None = None,
    mailbox: str | None = None,
    dry_run: bool = False,
    max_updates: int | None = None,
) -> OperationResult:
    if action not in _STATUS_ACTIONS:
        raise ValueError(f"unknown status action: {action!r}; expected one of {_STATUS_ACTIONS}")
    _require_writable(config, "status", "update_email_status")

    resolved, errors = _resolve_many(conn, jxa, message_ids, account=account, mailbox=mailbox)

    def prior_state(r: ResolvedMessage) -> dict:
        # Best-effort from our index (usually fresh); undo restores this
        # exact prior value rather than guessing the opposite of the target.
        row = conn.execute(
            "SELECT flag_read, flag_flagged FROM emails WHERE message_id = ?", (r.canonical_id,)
        ).fetchone()
        if row is None:
            return {}
        return {"is_read": bool(row["flag_read"]), "is_flagged": bool(row["flag_flagged"])}

    def preview(r: ResolvedMessage) -> AffectedMessage:
        ref = MessageRefModel(
            message_id=r.canonical_id, account=r.account_name, mailbox=r.mailbox_name
        )
        return AffectedMessage(message_ref=ref)

    def apply(r: ResolvedMessage) -> None:
        jxa.call(
            "updateEmailStatus",
            {
                "accountHint": r.account_name,
                "mailboxHint": r.mailbox_name,
                "messageId": to_mail_message_id(r.canonical_id),
                "action": action,
            },
        )

    def undo_record(r: ResolvedMessage) -> dict:
        return {"operation": action, "account_name": r.account_name, "prev_state": prior_state(r)}

    return guard(
        config=config,
        conn=conn,
        operation="update_email_status",
        operation_kind="status",
        resolved=resolved,
        errors=errors,
        preview_fn=preview,
        apply_fn=apply,
        undo_record_fn=undo_record,
        dry_run=dry_run,
        max_override=max_updates,
    )


def manage_trash(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    config: Config,
    action: str,
    message_ids: list[str],
    *,
    account: str | None = None,
    mailbox: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    max_deletes: int | None = None,
) -> OperationResult:
    if action not in _TRASH_ACTIONS:
        raise ValueError(f"unknown trash action: {action!r}; expected one of {_TRASH_ACTIONS}")
    operation_kind = "trash" if action == "move_to_trash" else "delete"
    _require_writable(config, operation_kind, f"manage_trash:{action}")

    resolved, errors = _resolve_many(conn, jxa, message_ids, account=account, mailbox=mailbox)

    def preview(r: ResolvedMessage) -> AffectedMessage:
        ref = MessageRefModel(
            message_id=r.canonical_id, account=r.account_name, mailbox=r.mailbox_name
        )
        to_mb = "Trash" if action == "move_to_trash" else None
        return AffectedMessage(message_ref=ref, from_mailbox=r.mailbox_name, to_mailbox=to_mb)

    def apply(r: ResolvedMessage) -> None:
        jxa.call(
            "manageTrash",
            {
                "accountHint": r.account_name,
                "mailboxHint": r.mailbox_name,
                "messageId": to_mail_message_id(r.canonical_id),
                "action": action,
            },
        )

    undo_record_fn = None
    if action == "move_to_trash":

        def undo_record(r: ResolvedMessage) -> dict:
            return {
                "operation": "trash",
                "account_name": r.account_name,
                "from_mailbox": r.mailbox_name,
                "to_mailbox": "Trash",
            }

        undo_record_fn = undo_record

    requires_confirm = (
        action == "delete_permanent" and "permanent_delete" in config.confirmation.require_confirm
    )

    return guard(
        config=config,
        conn=conn,
        operation=f"manage_trash:{action}",
        operation_kind="trash" if action == "move_to_trash" else "delete",
        resolved=resolved,
        errors=errors,
        preview_fn=preview,
        apply_fn=apply,
        undo_record_fn=undo_record_fn,
        dry_run=dry_run,
        confirm=confirm,
        max_override=max_deletes,
        requires_confirm=requires_confirm,
    )


def empty_trash(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    config: Config,
    *,
    account: str,
    dry_run: bool = True,
    confirm: bool = False,
) -> OperationResult:
    """Acts on the whole Trash mailbox, not specific message ids — kept
    separate from manage_trash's per-message guard() plumbing.
    """
    if config.server.read_only:
        raise ReadOnlyMode("server is running --read-only; empty_trash is disabled")

    count = jxa.call("trashCount", {"accountHint": account})["count"]
    preview = Preview(
        dry_run=True,
        operation="empty_trash",
        would_affect=[],
        count=count,
        blocked_by=[],
        reversible=False,
        undo_hint="not undoable",
    )

    if dry_run:
        return OperationResult(operation="empty_trash", count=count, dry_run=True, preview=preview)

    requires_confirm = "empty_trash" in config.confirmation.require_confirm
    if requires_confirm and not confirm:
        from cobos_apple_mail_mcp.core.errors import ConfirmationRequired

        raise ConfirmationRequired(
            "empty_trash requires confirm=true", preview=preview.model_dump()
        )

    result = jxa.call("emptyTrash", {"accountHint": account})
    return OperationResult(operation="empty_trash", count=result.get("count", count), dry_run=False)


def create_mailbox(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    config: Config,
    *,
    account: str,
    name: str,
    parent_mailbox: str | None = None,
) -> dict:
    if config.server.read_only:
        raise ReadOnlyMode("server is running --read-only; create_mailbox is disabled")
    full_name = f"{parent_mailbox}/{name}" if parent_mailbox else name
    return jxa.call("createMailbox", {"account": account, "name": full_name})
