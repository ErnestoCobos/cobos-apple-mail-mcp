"""Undo journal: records reversible writes (move, trash-until-emptied,
status, flag) so `undo_last()` can reverse the most recent batch through
`guard()` itself — batch limits and read-only still apply to undo.

Send/reply/forward and permanent-delete/empty-trash are never journaled —
they are not undoable (CLAUDE.md invariant #3) — and `undo_last()` refuses
honestly for those rather than pretending otherwise. Retains the last 500
batches.
"""

from __future__ import annotations

import json
import sqlite3
import time

from cobos_apple_mail_mcp.core.errors import UndoFailed
from cobos_apple_mail_mcp.core.identity import to_mail_message_id
from cobos_apple_mail_mcp.core.models import MessageRefModel, UndoResult
from cobos_apple_mail_mcp.write.jxa_executor import JXAExecutor

MAX_RETAINED_BATCHES = 500

_UNDOABLE_OPS = {"move", "trash", "mark_read", "mark_unread", "flag", "unflag", "set_flag_color"}


def journal_write(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    canonical_id: str,
    operation: str,
    account_name: str | None = None,
    from_mailbox: str | None = None,
    to_mailbox: str | None = None,
    prev_state: dict | None = None,
    new_state: dict | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO undo_journal(
          ts, batch_id, operation, canonical_id, account_name,
          from_mailbox, to_mailbox, prev_state, new_state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            time.time(),
            batch_id,
            operation,
            canonical_id,
            account_name,
            from_mailbox,
            to_mailbox,
            json.dumps(prev_state) if prev_state is not None else None,
            json.dumps(new_state) if new_state is not None else None,
        ),
    )
    conn.commit()
    _prune_old_batches(conn)


def _prune_old_batches(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT batch_id, MAX(ts) AS last_ts FROM undo_journal "
        "GROUP BY batch_id ORDER BY last_ts DESC"
    ).fetchall()
    stale = [r["batch_id"] for r in rows[MAX_RETAINED_BATCHES:]]
    if stale:
        conn.executemany("DELETE FROM undo_journal WHERE batch_id = ?", [(b,) for b in stale])
        conn.commit()


def _undo_move(conn: sqlite3.Connection, jxa: JXAExecutor, row: sqlite3.Row) -> None:
    from cobos_apple_mail_mcp.core.resolver import resolve

    if not row["from_mailbox"]:
        raise UndoFailed("original mailbox was not recorded; cannot undo")
    resolved = resolve(
        conn,
        jxa,
        row["canonical_id"],
        account_hint=row["account_name"],
        mailbox_hint=row["to_mailbox"],
    )
    jxa.call(
        "moveEmail",
        {
            "accountHint": resolved.account_name,
            "mailboxHint": resolved.mailbox_name,
            "messageId": to_mail_message_id(resolved.canonical_id),
            "toMailbox": row["from_mailbox"],
            # Undo always moves the message back to the account it came from.
            # `resolved` is wherever it is *now* (the move target, possibly a
            # different account); the destination is the recorded source account.
            "toAccount": row["account_name"],
        },
    )


def _undo_status(conn: sqlite3.Connection, jxa: JXAExecutor, row: sqlite3.Row) -> None:
    from cobos_apple_mail_mcp.core.resolver import resolve

    prev_state = json.loads(row["prev_state"]) if row["prev_state"] else {}
    resolved = resolve(conn, jxa, row["canonical_id"], account_hint=row["account_name"])
    base_args = {
        "accountHint": resolved.account_name,
        "mailboxHint": resolved.mailbox_name,
        "messageId": to_mail_message_id(resolved.canonical_id),
    }
    if "is_read" in prev_state:
        action = "mark_read" if prev_state["is_read"] else "mark_unread"
        jxa.call("updateEmailStatus", {**base_args, "action": action})
    # Restore flag color/flag state. A prior flag_color (int) is restored via
    # set_flag_color; otherwise restore the plain flagged bit. Prefer the more
    # specific flag_color when present so undoing a recolor lands exactly.
    prev_color = prev_state.get("flag_color")
    if prev_color is not None:
        jxa.call(
            "updateEmailStatus",
            {**base_args, "action": "set_flag_color", "flagIndex": prev_color},
        )
    elif "is_flagged" in prev_state:
        action = "flag" if prev_state["is_flagged"] else "unflag"
        jxa.call("updateEmailStatus", {**base_args, "action": action})


def undo_last(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    *,
    batch_id: str | None = None,
    dry_run: bool = False,
) -> UndoResult:
    target_batch = batch_id
    if target_batch is None:
        row = conn.execute(
            "SELECT batch_id FROM undo_journal WHERE undone = 0 "
            "AND operation IN ({}) ORDER BY ts DESC LIMIT 1".format(
                ",".join("?" for _ in _UNDOABLE_OPS)
            ),
            tuple(_UNDOABLE_OPS),
        ).fetchone()
        if row is None:
            raise UndoFailed("nothing undoable found")
        target_batch = row["batch_id"]

    rows = conn.execute(
        "SELECT * FROM undo_journal WHERE batch_id = ? AND undone = 0 ORDER BY ts", (target_batch,)
    ).fetchall()
    if not rows:
        raise UndoFailed(f"no undoable entries for batch_id={target_batch!r}")

    undone: list[MessageRefModel] = []
    failed: dict[str, str] = {}

    for row in rows:
        if row["operation"] not in _UNDOABLE_OPS:
            failed[row["canonical_id"]] = f"operation {row['operation']!r} is not undoable"
            continue
        if dry_run:
            ref = MessageRefModel(message_id=row["canonical_id"], account=row["account_name"])
            undone.append(ref)
            continue
        try:
            if row["operation"] in ("move", "trash"):
                _undo_move(conn, jxa, row)
            else:
                _undo_status(conn, jxa, row)
        except Exception as exc:  # noqa: BLE001 - one row failing must not abort the batch
            failed[row["canonical_id"]] = str(exc)
            continue
        conn.execute(
            "UPDATE undo_journal SET undone = 1, undo_ts = ? WHERE id = ?", (time.time(), row["id"])
        )
        conn.commit()
        undone.append(MessageRefModel(message_id=row["canonical_id"], account=row["account_name"]))

    return UndoResult(batch_id=target_batch, undone=undone, failed=failed, count=len(undone))
