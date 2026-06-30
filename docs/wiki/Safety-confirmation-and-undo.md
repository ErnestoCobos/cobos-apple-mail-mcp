---
covers:
  - src/cobos_apple_mail_mcp/core/safety.py
  - src/cobos_apple_mail_mcp/core/undo.py
last_verified: 2026-06-30
---

# Safety, confirmation & undo

`core/safety.py::guard()` is the single wrapper every write tool passes through. No write tool
calls `write/*.py` directly.

## `guard()` checks, in order

1. **`--read-only`** (`config.server.read_only`) blocks every send/modify operation —
   `operation_kind != "draft"`. Draft creation stays allowed (it mutates nothing the user already
   has). Checked **before** resolution is attempted in the batch write tools
   (`write/organize.py::_require_writable()`) so a blocked call never touches JXA/Mail.app at
   all — confirmed by `tests/test_write_layer.py::test_read_only_blocks_before_any_jxa_call`,
   which catches a real regression where an earlier version resolved first and only checked
   read-only inside `guard()`, adding a real (and once observed: ~20-second) JXA round-trip to
   every blocked call.
2. **Batch limits** (`config.batch_limits`: `move=1, status=10, trash=5, delete=1` by default) —
   exceeding the limit **rejects** the whole call (`BatchLimitExceeded`), it never silently
   truncates to the first N messages.
3. **`dry_run`** — runs full resolution (so the preview reflects exactly what would be acted on,
   including any `MultipleMatches`/`NotFound`), performs zero mutation, returns a `Preview`.
4. **`confirm`** — operations in `config.confirmation.require_confirm` (`permanent_delete`,
   `empty_trash` by default) need `confirm=true`; otherwise `ConfirmationRequired` is raised,
   carrying the preview. Because resolution is always redone fresh on every call (never trusting
   a stale snapshot), a confirmed call naturally re-validates against current mailbox state —
   there's no separate "stale confirmation token" mechanism needed.

## What's actually undoable

| Operation | Undoable? | Mechanism |
|---|---|---|
| move | Yes | `core/undo.py::_undo_move()` re-resolves at the new location and moves back |
| trash (move_to_trash) | Yes, until emptied | journaled as a move to "Trash"; reversing moves it back to the recorded origin mailbox |
| mark_read/unread, flag/unflag | Yes | the prior `is_read`/`is_flagged` value (read from the index at write time) is restored |
| permanent delete | **No** | never journaled — `manage_trash(action="delete_permanent")` passes `undo_record_fn=None` |
| empty_trash | **No** | a separate function (`organize.py::empty_trash()`), not journaled |
| send/reply/forward | **No** | never journaled — sending cannot be undone |

`undo_last(batch_id=None, dry_run=False)`:

- With no `batch_id`, finds the most recent batch with an undoable operation
  (`operation IN ('move','trash','mark_read','mark_unread','flag','unflag')`) and reverses it.
- Each row's reversal goes through the normal resolve+JXA path again — if the message has moved
  again or been deleted since, that row's undo fails with a clear per-row reason
  (`UndoResult.failed`), while the rest of the batch still attempts to undo.
- The journal retains the last 500 batches (`core/undo.py::MAX_RETAINED_BATCHES`); older batches
  are pruned automatically on every write.

## `undo_journal` schema

```sql
CREATE TABLE undo_journal (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, batch_id TEXT NOT NULL,
  operation TEXT NOT NULL, canonical_id TEXT NOT NULL,
  account_name TEXT, from_mailbox TEXT, to_mailbox TEXT,
  prev_state TEXT, new_state TEXT,           -- JSON, for status/flag operations
  undone INTEGER NOT NULL DEFAULT 0, undo_ts REAL
);
```

One `batch_id` per tool call; one row per affected message. `account_name` (despite the column
name) stores the JXA-addressable account name, not the disk UUID — see
[Identity & resolution](Identity-and-resolution.md).

## Honesty over completeness

The undo system does not pretend to cover everything. `Preview.reversible` and `Preview.undo_hint`
tell the caller up front whether an operation can be undone at all, and permanent
delete/empty-trash/send are reported as non-undoable rather than silently accepted with a fake
promise of recoverability.
