"""guard(): the single safety wrapper every write tool passes through
(CLAUDE.md invariant #2). No write tool calls write/*.py directly.

Enforces, in order: --read-only blocks all send/modify operations (draft
creation stays allowed); batch caps reject oversized requests rather than
silently truncating them; dry_run returns a Preview with zero mutation;
confirm gating protects permanent_delete/empty_trash. Reversible operations
are journaled so undo_last() can reverse them (core/undo.py).
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable

from cobos_apple_mail_mcp.config import Config
from cobos_apple_mail_mcp.core.errors import (
    AppleMailMCPError,
    BatchLimitExceeded,
    ConfirmationRequired,
    ReadOnlyMode,
)
from cobos_apple_mail_mcp.core.models import (
    AffectedMessage,
    MessageRefModel,
    OperationResult,
    Preview,
)
from cobos_apple_mail_mcp.core.resolver import ResolvedMessage
from cobos_apple_mail_mcp.core.undo import journal_write

_BATCH_LIMIT_ATTR = {"move": "move", "status": "status", "trash": "trash", "delete": "delete"}


def batch_limit_for(config: Config, operation_kind: str) -> int | None:
    attr = _BATCH_LIMIT_ATTR.get(operation_kind)
    return getattr(config.batch_limits, attr) if attr else None


def guard(
    *,
    config: Config,
    conn: sqlite3.Connection,
    operation: str,
    operation_kind: str,
    resolved: list[ResolvedMessage],
    errors: dict[str, str],
    preview_fn: Callable[[ResolvedMessage], AffectedMessage],
    apply_fn: Callable[[ResolvedMessage], None],
    undo_record_fn: Callable[[ResolvedMessage], dict] | None = None,
    dry_run: bool = False,
    confirm: bool = False,
    max_override: int | None = None,
    requires_confirm: bool = False,
) -> OperationResult:
    """Run one batch write operation through every safety check.

    `resolved`/`errors` must already reflect a FRESH resolution for this
    call (core/resolver.py re-verifies live every time, so dry_run and a
    subsequent confirm=true call naturally see current mailbox state rather
    than a stale snapshot).
    """
    is_draft_op = operation_kind == "draft"
    if config.server.read_only and not is_draft_op:
        raise ReadOnlyMode(f"server is running --read-only; {operation!r} is disabled")

    limit = max_override or batch_limit_for(config, operation_kind)
    if limit is not None and len(resolved) > limit:
        raise BatchLimitExceeded(
            f"{operation!r} requested for {len(resolved)} messages, exceeds the "
            f"configured limit of {limit}",
            limit=limit,
            requested=len(resolved),
        )

    affected = [preview_fn(r) for r in resolved]
    reversible = undo_record_fn is not None
    preview = Preview(
        dry_run=True,
        operation=operation,
        would_affect=affected,
        count=len(affected),
        blocked_by=list(errors.keys()),
        reversible=reversible,
        undo_hint="undo_last() reverses this" if reversible else "not undoable",
    )

    if dry_run:
        return OperationResult(
            operation=operation, count=len(affected), dry_run=True, preview=preview, failed=errors
        )

    if requires_confirm and not confirm:
        raise ConfirmationRequired(
            f"{operation!r} requires confirm=true", preview=preview.model_dump()
        )

    batch_id = str(uuid.uuid4())
    succeeded: list[MessageRefModel] = []
    failed = dict(errors)

    for r in resolved:
        try:
            apply_fn(r)
        except AppleMailMCPError as exc:
            failed[r.canonical_id] = str(exc)
            continue
        ref = MessageRefModel(
            message_id=r.canonical_id, account=r.account_name, mailbox=r.mailbox_name
        )
        succeeded.append(ref)
        if undo_record_fn is not None:
            journal_write(conn, batch_id=batch_id, canonical_id=r.canonical_id, **undo_record_fn(r))

    return OperationResult(
        operation=operation,
        succeeded=succeeded,
        failed=failed,
        count=len(succeeded),
        batch_id=batch_id if succeeded and reversible else None,
        dry_run=False,
    )
