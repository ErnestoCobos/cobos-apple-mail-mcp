from __future__ import annotations

from cobos_apple_mail_mcp.knowledge.contacts import list_contacts
from cobos_apple_mail_mcp.read.indexer import build_index
from cobos_apple_mail_mcp.storage.database import connect_index
from tests.helpers import write_message


def _seed(tmp_path):
    # Received: 2 from Alice, 1 from Bob.
    alice = "Alice <alice@example.com>"
    write_message(tmp_path, rowid=1, message_id="r1@x.com", sender=alice, mailbox="INBOX")
    write_message(tmp_path, rowid=2, message_id="r2@x.com", sender=alice, mailbox="INBOX")
    write_message(
        tmp_path, rowid=3, message_id="r3@x.com", sender="Bob <bob@example.com>", mailbox="INBOX"
    )
    # Sent: 1 to Carol (never received from — only visible bidirectionally),
    # 1 to Alice (so Alice is both received and sent).
    write_message(
        tmp_path,
        rowid=4,
        message_id="s1@x.com",
        sender="Me <me@example.com>",
        to="Carol Example <carol@example.com>",
        mailbox="Sent",
    )
    write_message(
        tmp_path,
        rowid=5,
        message_id="s2@x.com",
        sender="Me <me@example.com>",
        to="alice@example.com",
        mailbox="Sent",
    )
    conn = connect_index(":memory:")
    build_index(conn, tmp_path, full=True)
    return conn


def test_list_contacts_is_bidirectional(tmp_path):
    conn = _seed(tmp_path)
    contacts = {c.address.lower(): c for c in list_contacts(conn)}

    # Carol was only ever emailed TO — a sender-only list would omit her.
    assert "carol@example.com" in contacts
    assert contacts["carol@example.com"].sent_count == 1
    assert contacts["carol@example.com"].received_count == 0

    alice = contacts["alice@example.com"]
    assert alice.received_count == 2
    assert alice.sent_count == 1
    assert alice.total_count == 3
    assert alice.display_name == "Alice"


def test_list_contacts_ranks_by_total_volume(tmp_path):
    conn = _seed(tmp_path)
    contacts = list_contacts(conn)
    # Alice (3) outranks Bob (1) and Carol (1).
    assert contacts[0].address.lower() == "alice@example.com"


def test_list_contacts_query_matches_name_and_address(tmp_path):
    conn = _seed(tmp_path)
    by_name = list_contacts(conn, query="carol")
    assert [c.address.lower() for c in by_name] == ["carol@example.com"]

    by_addr = list_contacts(conn, query="bob@example")
    assert [c.address.lower() for c in by_addr] == ["bob@example.com"]


def test_list_contacts_respects_limit(tmp_path):
    conn = _seed(tmp_path)
    assert len(list_contacts(conn, limit=1)) == 1


def test_list_contacts_case_insensitive_dedup(tmp_path):
    write_message(
        tmp_path, rowid=1, message_id="c1@x.com", sender="Dave <dave@example.com>", mailbox="INBOX"
    )
    write_message(
        tmp_path, rowid=2, message_id="c2@x.com", sender="Dave <DAVE@example.com>", mailbox="INBOX"
    )
    conn = connect_index(":memory:")
    build_index(conn, tmp_path, full=True)
    contacts = list_contacts(conn)
    dave = [c for c in contacts if c.address.lower() == "dave@example.com"]
    assert len(dave) == 1
    assert dave[0].received_count == 2
