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
from cobos_apple_mail_mcp.write import compose, organize
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


def test_compose_email_passes_account_to_jxa():
    # Regression: compose_email used to drop the account, so the JXA layer
    # couldn't send from the requested account and fell back to the default
    # one (found only by sending a real email against real Mail).
    conn = _conn()
    jxa = FakeJXAExecutor()
    jxa.on("composeEmail", lambda args: {"status": "sent", "_args": args})
    compose.compose_email(
        conn, jxa, _cfg(), account="Work", to="x@example.com", subject="hi", body="b", mode="send"
    )
    call_args = next(args for name, args in jxa.calls if name == "composeEmail")
    assert call_args["account"] == "Work"
    assert call_args["subject"] == "hi"


def _seed_one_message(conn):
    """Put a single indexed row in place so set_flag_color's prior_state read
    and optimistic index update have a row to act on."""
    conn.execute(
        "INSERT INTO emails (emlx_path, account_uuid, mailbox_url, message_id, "
        "flag_read, flag_flagged, flag_color, indexed_at) "
        "VALUES ('/p/1.emlx', 'U', 'mbox', 'm1@x.com', 0, 0, NULL, 0)"
    )
    conn.commit()


def _jxa_for_status():
    jxa = FakeJXAExecutor()
    jxa.on("resolveMessage", _resolve_handler({"m1@x.com": ("Work", "INBOX", 1)}))
    jxa.on("updateEmailStatus", lambda args: {"updated": True, "action": args["action"]})
    return jxa


def test_set_flag_color_sets_index_and_is_undoable():
    conn = _conn()
    _seed_one_message(conn)
    jxa = _jxa_for_status()
    cfg = _cfg()

    result = organize.update_email_status(
        conn, jxa, cfg, ["m1@x.com"], "set_flag_color", color="green"
    )
    assert result.count == 1

    # JXA received the mapped integer (green == 3), not the color name.
    status_calls = [args for name, args in jxa.calls if name == "updateEmailStatus"]
    assert status_calls[-1]["flagIndex"] == 3

    # Optimistic index update: searchable immediately, before any reindex.
    row = conn.execute(
        "SELECT flag_color, flag_flagged FROM emails WHERE message_id='m1@x.com'"
    ).fetchone()
    assert row["flag_color"] == 3
    assert row["flag_flagged"] == 1

    # Journaled as undoable, with the prior (NULL) color captured.
    jrow = conn.execute("SELECT operation, prev_state FROM undo_journal").fetchone()
    assert jrow["operation"] == "set_flag_color"

    # Undo restores the prior state (was unflagged) -> unflag call.
    result = undo_last(conn, jxa)
    assert result.count == 1
    undo_calls = [args for name, args in jxa.calls if name == "updateEmailStatus"]
    assert undo_calls[-1]["action"] == "unflag"


def test_set_flag_color_requires_a_color():
    conn = _conn()
    _seed_one_message(conn)
    jxa = _jxa_for_status()
    cfg = _cfg()
    with pytest.raises(ValueError, match="requires a color"):
        organize.update_email_status(conn, jxa, cfg, ["m1@x.com"], "set_flag_color")
    # Failed before any JXA call.
    assert not any(name == "updateEmailStatus" for name, _ in jxa.calls)


def test_set_flag_color_rejects_unknown_color():
    conn = _conn()
    _seed_one_message(conn)
    jxa = _jxa_for_status()
    cfg = _cfg()
    with pytest.raises(ValueError):
        organize.update_email_status(
            conn, jxa, cfg, ["m1@x.com"], "set_flag_color", color="chartreuse"
        )
    assert not any(name == "updateEmailStatus" for name, _ in jxa.calls)


def test_set_flag_color_blocked_in_read_only():
    conn = _conn()
    _seed_one_message(conn)
    jxa = FakeJXAExecutor()  # no handlers -> any call raises
    cfg = _cfg(server={"read_only": True})
    with pytest.raises(ReadOnlyMode):
        organize.update_email_status(
            conn, jxa, cfg, ["m1@x.com"], "set_flag_color", color="green"
        )
    assert jxa.calls == []


def test_prune_old_eml_bounds_the_outbox(tmp_path, monkeypatch):
    # create_rich_email_draft writes a .eml and hands it to Mail; those must
    # not pile up forever. _prune_old_eml deletes ones older than the cutoff.
    import time as _time

    from cobos_apple_mail_mcp.write import compose

    monkeypatch.setattr(compose, "_EML_OUTBOX", tmp_path)
    old = tmp_path / "old.eml"
    fresh = tmp_path / "fresh.eml"
    old.write_bytes(b"old")
    fresh.write_bytes(b"fresh")
    # Backdate the old one well past the cutoff.
    stale = _time.time() - compose._EML_MAX_AGE_SEC - 10
    import os as _os

    _os.utime(old, (stale, stale))

    compose._prune_old_eml()

    assert not old.exists()  # pruned
    assert fresh.exists()  # recent one kept (Mail may still be importing it)
