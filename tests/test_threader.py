from __future__ import annotations

from cobos_apple_mail_mcp.read.indexer import build_index
from cobos_apple_mail_mcp.read.threader import get_email_thread
from cobos_apple_mail_mcp.storage.database import connect_index
from tests.helpers import write_message


def _conn():
    return connect_index(":memory:")


def test_linear_thread(tmp_path):
    write_message(
        tmp_path, rowid=1, message_id="m1@x.com", subject="Project kickoff", date_sent=100
    )
    write_message(
        tmp_path,
        rowid=2,
        message_id="m2@x.com",
        subject="Re: Project kickoff",
        in_reply_to="m1@x.com",
        references=["m1@x.com"],
        date_sent=200,
    )
    write_message(
        tmp_path,
        rowid=3,
        message_id="m3@x.com",
        subject="Re: Project kickoff",
        in_reply_to="m2@x.com",
        references=["m1@x.com", "m2@x.com"],
        date_sent=300,
    )
    conn = _conn()
    build_index(conn, tmp_path, full=True)

    thread = get_email_thread(conn, message_id="m1@x.com")
    assert thread.message_count == 3
    assert thread.root.message_ref.message_id == "m1@x.com"
    assert len(thread.root.children) == 1
    assert thread.root.children[0].message_ref.message_id == "m2@x.com"
    assert thread.root.children[0].children[0].message_ref.message_id == "m3@x.com"

    # Looking up from any message in the thread finds the same thread.
    thread_via_leaf = get_email_thread(conn, message_id="m3@x.com")
    assert thread_via_leaf.thread_id == thread.thread_id


def test_phantom_container_groups_siblings(tmp_path):
    # Both reply to a message we never indexed (e.g. it lives in a mailbox
    # we don't index, or was never delivered to us) -> a phantom container
    # should still group them as siblings.
    write_message(
        tmp_path,
        rowid=1,
        message_id="a@x.com",
        subject="Re: Missing root",
        in_reply_to="ghost@x.com",
        references=["ghost@x.com"],
        date_sent=100,
    )
    write_message(
        tmp_path,
        rowid=2,
        message_id="b@x.com",
        subject="Re: Missing root",
        in_reply_to="ghost@x.com",
        references=["ghost@x.com"],
        date_sent=200,
    )
    conn = _conn()
    build_index(conn, tmp_path, full=True)

    thread = get_email_thread(conn, message_id="a@x.com")
    assert thread.message_count == 2
    # root is the phantom (no message_ref); both real messages are its children.
    assert thread.root.message_ref is None
    child_ids = {c.message_ref.message_id for c in thread.root.children}
    assert child_ids == {"a@x.com", "b@x.com"}


def test_subject_fallback_merges_orphan_roots(tmp_path):
    # No References/In-Reply-To at all, but same normalized subject and one
    # looks like a reply -> merged by the subject-gathering fallback.
    write_message(tmp_path, rowid=1, message_id="orig@x.com", subject="Budget Q3", date_sent=100)
    write_message(
        tmp_path, rowid=2, message_id="reply@x.com", subject="Re: Budget Q3", date_sent=200
    )
    conn = _conn()
    build_index(conn, tmp_path, full=True)

    thread = get_email_thread(conn, message_id="orig@x.com")
    assert thread.message_count == 2
    assert thread.root.message_ref.message_id == "orig@x.com"
    assert thread.root.children[0].message_ref.message_id == "reply@x.com"


def test_unrelated_messages_are_separate_threads(tmp_path):
    write_message(tmp_path, rowid=1, message_id="one@x.com", subject="Topic A", date_sent=100)
    write_message(tmp_path, rowid=2, message_id="two@x.com", subject="Topic B", date_sent=200)
    conn = _conn()
    build_index(conn, tmp_path, full=True)

    t1 = get_email_thread(conn, message_id="one@x.com")
    t2 = get_email_thread(conn, message_id="two@x.com")
    assert t1.thread_id != t2.thread_id
    assert t1.message_count == 1
    assert t2.message_count == 1
