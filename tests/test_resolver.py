from __future__ import annotations

import pytest

from cobos_apple_mail_mcp.core.errors import MultipleMatches, NotFound
from cobos_apple_mail_mcp.core.identity import make_opaque_handle
from cobos_apple_mail_mcp.core.resolver import resolve
from cobos_apple_mail_mcp.storage.database import connect_index
from tests.helpers import FakeJXAExecutor


def _conn():
    return connect_index(":memory:")


def _candidate(account="Work", mailbox="INBOX", mail_id=1, message_id="<abc@x.com>"):
    return {
        "account": account,
        "mailbox": mailbox,
        "mailInternalId": mail_id,
        "messageId": message_id,
        "subject": "Test",
        "dateSent": None,
    }


def test_resolve_scoped_hint_finds_single_match():
    conn = _conn()
    jxa = FakeJXAExecutor()
    def handler(args):
        if args["accountHint"] == "Work":
            return {"candidates": [_candidate()]}
        return {"candidates": []}

    jxa.on("resolveMessage", handler)

    resolved = resolve(conn, jxa, "abc@x.com", account_hint="Work", mailbox_hint="INBOX")
    assert resolved.account_name == "Work"
    assert resolved.mailbox_name == "INBOX"
    assert resolved.mail_int_id == 1

    # The scoped hint found it on the first attempt -> only one call made.
    assert len(jxa.calls) == 1


def test_resolve_caches_then_reuses_on_next_call():
    conn = _conn()
    jxa = FakeJXAExecutor()
    jxa.on("resolveMessage", lambda args: {"candidates": [_candidate()]})

    resolve(conn, jxa, "abc@x.com", account_hint="Work", mailbox_hint="INBOX")
    row = conn.execute(
        "SELECT account_name, mailbox_name FROM resolve_cache WHERE canonical_id = 'abc@x.com'"
    ).fetchone()
    assert row["account_name"] == "Work"
    assert row["mailbox_name"] == "INBOX"

    # Second resolve with NO hints should hit the cache attempt before any
    # broad scan.
    jxa.calls.clear()
    resolve(conn, jxa, "abc@x.com")
    first_call_args = jxa.calls[0][1]
    assert first_call_args["accountHint"] == "Work"
    assert first_call_args["mailboxHint"] == "INBOX"


def test_resolve_not_found_when_no_candidates():
    conn = _conn()
    jxa = FakeJXAExecutor()
    jxa.on("resolveMessage", lambda args: {"candidates": []})

    with pytest.raises(NotFound):
        resolve(conn, jxa, "missing@x.com")


def test_resolve_multiple_matches_never_auto_picks():
    conn = _conn()
    jxa = FakeJXAExecutor()
    jxa.on(
        "resolveMessage",
        lambda args: {
            "candidates": [
                _candidate(account="Work", mail_id=1),
                _candidate(account="Personal", mail_id=2),
            ]
        },
    )

    with pytest.raises(MultipleMatches) as exc_info:
        resolve(conn, jxa, "abc@x.com")
    assert len(exc_info.value.details["candidates"]) == 2


def test_resolve_filters_out_unverified_candidates():
    conn = _conn()
    jxa = FakeJXAExecutor()
    # A candidate whose own messageId does NOT match what we asked for must
    # never be trusted (CLAUDE.md invariant #1: mandatory read-back verify).
    jxa.on(
        "resolveMessage",
        lambda args: {"candidates": [_candidate(message_id="<totally-different@y.com>")]},
    )

    with pytest.raises(NotFound):
        resolve(conn, jxa, "abc@x.com")


def test_resolve_opaque_handle_never_calls_jxa():
    conn = _conn()
    jxa = FakeJXAExecutor()
    handle = make_opaque_handle("uuid-1", "INBOX", 1, 123.0)

    with pytest.raises(NotFound):
        resolve(conn, jxa, handle)
    assert jxa.calls == []
