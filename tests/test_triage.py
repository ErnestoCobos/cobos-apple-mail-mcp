from __future__ import annotations

from cobos_apple_mail_mcp.knowledge.triage import get_awaiting_reply, get_needs_response
from cobos_apple_mail_mcp.read.indexer import build_index
from cobos_apple_mail_mcp.storage.database import connect_index
from tests.helpers import write_message


def _conn():
    return connect_index(":memory:")


def test_awaiting_reply_flags_unanswered_sent_message(tmp_path):
    write_message(
        tmp_path,
        rowid=1,
        message_id="sent1@x.com",
        subject="Can we meet?",
        sender="Me <me@example.com>",
        to="Bob <bob@example.com>",
        mailbox="Sent",
        date_sent=1000,
    )
    conn = _conn()
    build_index(conn, tmp_path, full=True)

    items = get_awaiting_reply(conn, days_back=36500)
    assert len(items) == 1
    assert items[0].recipient == "bob@example.com"
    assert items[0].message_ref.message_id == "sent1@x.com"


def test_awaiting_reply_excludes_replied_message(tmp_path):
    write_message(
        tmp_path,
        rowid=1,
        message_id="sent2@x.com",
        subject="Can we meet?",
        sender="Me <me@example.com>",
        to="Alice <alice@example.com>",
        mailbox="Sent",
        date_sent=1000,
    )
    write_message(
        tmp_path,
        rowid=2,
        message_id="reply2@x.com",
        subject="Re: Can we meet?",
        sender="Alice <alice@example.com>",
        to="Me <me@example.com>",
        in_reply_to="sent2@x.com",
        references=["sent2@x.com"],
        mailbox="INBOX",
        date_sent=2000,
    )
    conn = _conn()
    build_index(conn, tmp_path, full=True)

    items = get_awaiting_reply(conn, days_back=36500)
    assert items == []


def test_awaiting_reply_excludes_noreply_recipient(tmp_path):
    write_message(
        tmp_path,
        rowid=1,
        message_id="sent3@x.com",
        sender="Me <me@example.com>",
        to="No Reply <no-reply@example.com>",
        mailbox="Sent",
        date_sent=1000,
    )
    conn = _conn()
    build_index(conn, tmp_path, full=True)

    assert get_awaiting_reply(conn, days_back=36500) == []


def test_needs_response_flags_unread_question(tmp_path):
    write_message(
        tmp_path,
        rowid=1,
        message_id="q1@x.com",
        subject="Quick question",
        sender="Bob <bob@example.com>",
        body="Can you review this by Friday?",
        mailbox="INBOX",
        flags=0,  # unread
        date_sent=int(__import__("time").time()) - 86400,
    )
    conn = _conn()
    build_index(conn, tmp_path, full=True)

    items = get_needs_response(conn, days_back=36500, threshold=4)
    assert len(items) == 1
    assert items[0].message_ref.message_id == "q1@x.com"
    assert items[0].score >= 4


def test_needs_response_excludes_read_message(tmp_path):
    from cobos_apple_mail_mcp.read.emlx_parser import FLAG_SEEN

    write_message(
        tmp_path,
        rowid=1,
        message_id="q2@x.com",
        body="Can you review this?",
        mailbox="INBOX",
        flags=FLAG_SEEN,
    )
    conn = _conn()
    build_index(conn, tmp_path, full=True)
    assert get_needs_response(conn) == []


def test_needs_response_excludes_newsletter(tmp_path):
    write_message(
        tmp_path,
        rowid=1,
        message_id="news1@x.com",
        subject="Can you believe this deal?",
        body="Please confirm your subscription urgently.",
        mailbox="INBOX",
        flags=0,
        list_unsubscribe="<https://example.com/unsub>",
    )
    conn = _conn()
    build_index(conn, tmp_path, full=True)
    assert get_needs_response(conn, days_back=36500) == []
