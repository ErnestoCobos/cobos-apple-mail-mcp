from __future__ import annotations

import pytest

from cobos_apple_mail_mcp.config import load_config
from cobos_apple_mail_mcp.core.errors import (
    BatchLimitExceeded,
    ConfirmationRequired,
    ReadOnlyMode,
    UndoFailed,
)
from cobos_apple_mail_mcp.core.undo import undo_last
from cobos_apple_mail_mcp.storage.database import connect_index
from cobos_apple_mail_mcp.write import organize
from tests.helpers import FakeJXAExecutor


def _conn():
    return connect_index(":memory:")


def _cfg(**overrides):
    return load_config(cli_overrides=overrides, environ={})


def _resolve_handler(known: dict[str, tuple[str, str, int]]):
    def handler(args):
        bare = args["messageId"].strip("<>")
        if bare in known:
            account, mailbox, mail_id = known[bare]
            return {
                "candidates": [
                    {
                        "account": account,
                        "mailbox": mailbox,
                        "mailInternalId": mail_id,
                        "messageId": args["messageId"],
                        "subject": "Test",
                        "dateSent": None,
                    }
                ]
            }
        return {"candidates": []}

    return handler


def _jxa_for_move():
    jxa = FakeJXAExecutor()
    jxa.on("resolveMessage", _resolve_handler({"m1@x.com": ("Work", "INBOX", 1)}))
    jxa.on("moveEmail", lambda args: {"moved": True, "toMailbox": args["toMailbox"]})
    return jxa


def test_move_email_dry_run_makes_no_jxa_mutation():
    conn = _conn()
    jxa = _jxa_for_move()
    cfg = _cfg()

    result = organize.move_email(conn, jxa, cfg, ["m1@x.com"], "Archive", dry_run=True)

    assert result.dry_run is True
    assert result.preview.count == 1
    assert not any(name == "moveEmail" for name, _ in jxa.calls)
    journal_rows = conn.execute("SELECT COUNT(*) AS n FROM undo_journal").fetchone()
    assert journal_rows["n"] == 0


def test_move_email_succeeds_and_journals_undo():
    conn = _conn()
    jxa = _jxa_for_move()
    cfg = _cfg()

    result = organize.move_email(conn, jxa, cfg, ["m1@x.com"], "Archive")

    assert result.count == 1
    assert result.succeeded[0].message_id == "m1@x.com"
    assert any(name == "moveEmail" for name, _ in jxa.calls)
    row = conn.execute("SELECT operation, from_mailbox, to_mailbox FROM undo_journal").fetchone()
    assert row["operation"] == "move"
    assert row["from_mailbox"] == "INBOX"
    assert row["to_mailbox"] == "Archive"


def test_move_email_batch_limit_rejects_not_truncates():
    conn = _conn()
    jxa = FakeJXAExecutor()
    jxa.on(
        "resolveMessage",
        _resolve_handler({"m1@x.com": ("Work", "INBOX", 1), "m2@x.com": ("Work", "INBOX", 2)}),
    )
    jxa.on("moveEmail", lambda args: {"moved": True})
    cfg = _cfg()  # default move limit = 1

    with pytest.raises(BatchLimitExceeded) as exc_info:
        organize.move_email(conn, jxa, cfg, ["m1@x.com", "m2@x.com"], "Archive")
    assert exc_info.value.details["limit"] == 1
    assert exc_info.value.details["requested"] == 2
    # Rejected outright -- no partial move of either message.
    assert not any(name == "moveEmail" for name, _ in jxa.calls)


def test_update_email_status_blocked_in_read_only():
    conn = _conn()
    jxa = FakeJXAExecutor()
    jxa.on("resolveMessage", _resolve_handler({"m1@x.com": ("Work", "INBOX", 1)}))
    cfg = _cfg(server={"read_only": True})

    with pytest.raises(ReadOnlyMode):
        organize.update_email_status(conn, jxa, cfg, ["m1@x.com"], "mark_read")
    assert jxa.calls == [] or not any(name == "updateEmailStatus" for name, _ in jxa.calls)


def test_read_only_blocks_before_any_jxa_call():
    """A blocked write must fail before resolution -- never touch JXA at
    all (CLAUDE.md invariant #4: never hang, never do needless external
    work). A previous version called resolve() (and thus JXA) before
    guard() ever checked read_only.
    """
    conn = _conn()
    cfg = _cfg(server={"read_only": True})

    jxa = FakeJXAExecutor()  # no handlers registered -- any .call() raises AssertionError
    with pytest.raises(ReadOnlyMode):
        organize.move_email(conn, jxa, cfg, ["m1@x.com"], "Archive")
    assert jxa.calls == []

    with pytest.raises(ReadOnlyMode):
        organize.update_email_status(conn, jxa, cfg, ["m1@x.com"], "mark_read")
    assert jxa.calls == []

    with pytest.raises(ReadOnlyMode):
        organize.manage_trash(conn, jxa, cfg, "move_to_trash", ["m1@x.com"])
    assert jxa.calls == []

    with pytest.raises(ReadOnlyMode):
        organize.manage_trash(conn, jxa, cfg, "delete_permanent", ["m1@x.com"], confirm=True)
    assert jxa.calls == []


def test_manage_trash_permanent_delete_requires_confirm():
    conn = _conn()
    jxa = FakeJXAExecutor()
    jxa.on("resolveMessage", _resolve_handler({"m1@x.com": ("Work", "INBOX", 1)}))
    jxa.on("manageTrash", lambda args: {"deleted": True})
    cfg = _cfg()

    with pytest.raises(ConfirmationRequired):
        organize.manage_trash(
            conn, jxa, cfg, "delete_permanent", ["m1@x.com"], dry_run=False, confirm=False
        )

    result = organize.manage_trash(
        conn, jxa, cfg, "delete_permanent", ["m1@x.com"], dry_run=False, confirm=True
    )
    assert result.count == 1
    # Permanent delete is never journaled -- not undoable.
    assert conn.execute("SELECT COUNT(*) AS n FROM undo_journal").fetchone()["n"] == 0


def test_manage_trash_move_to_trash_is_undoable():
    conn = _conn()
    jxa = FakeJXAExecutor()
    jxa.on("resolveMessage", _resolve_handler({"m1@x.com": ("Work", "INBOX", 1)}))
    jxa.on("manageTrash", lambda args: {"trashed": True})
    cfg = _cfg()

    result = organize.manage_trash(conn, jxa, cfg, "move_to_trash", ["m1@x.com"], dry_run=False)
    assert result.count == 1
    row = conn.execute("SELECT operation, to_mailbox FROM undo_journal").fetchone()
    assert row["operation"] == "trash"
    assert row["to_mailbox"] == "Trash"


def test_undo_last_reverses_a_move():
    conn = _conn()
    jxa = _jxa_for_move()
    cfg = _cfg()
    organize.move_email(conn, jxa, cfg, ["m1@x.com"], "Archive")

    # undo_last re-resolves at the NEW location (Archive) and moves back.
    jxa.on(
        "resolveMessage", _resolve_handler({"m1@x.com": ("Work", "Archive", 1)})
    )
    result = undo_last(conn, jxa)
    assert result.count == 1
    assert result.undone[0].message_id == "m1@x.com"

    move_calls = [args for name, args in jxa.calls if name == "moveEmail"]
    assert move_calls[-1]["toMailbox"] == "INBOX"  # back to where it started

    row = conn.execute("SELECT undone FROM undo_journal").fetchone()
    assert row["undone"] == 1


def test_undo_last_nothing_to_undo_raises():
    conn = _conn()
    jxa = FakeJXAExecutor()
    with pytest.raises(UndoFailed):
        undo_last(conn, jxa)
