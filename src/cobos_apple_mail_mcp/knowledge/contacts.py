"""email://contacts/{addr} projection — derived purely from the index. No
separate contacts database; integration with macOS Contacts/AddressBook is
out of scope (CLAUDE.md knowledge map: Threading and knowledge).
"""

from __future__ import annotations

import json
import sqlite3
from email.utils import getaddresses

from cobos_apple_mail_mcp.core.models import Contact, ContactSummary
from cobos_apple_mail_mcp.read.rowmap import row_to_summary


def get_contact(conn: sqlite3.Connection, address: str) -> Contact:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n,
               MAX(date_received) AS last,
               (SELECT sender_name FROM emails
                WHERE sender_addr = :addr AND sender_name IS NOT NULL
                ORDER BY date_received DESC LIMIT 1) AS name
        FROM emails WHERE sender_addr = :addr
        """,
        {"addr": address},
    ).fetchone()
    recent_rows = conn.execute(
        "SELECT * FROM emails WHERE sender_addr = ? ORDER BY date_received DESC LIMIT 5",
        (address,),
    ).fetchall()
    return Contact(
        address=address,
        display_name=row["name"],
        message_count=row["n"] or 0,
        last_contact=row["last"],
        recent_messages=[row_to_summary(r) for r in recent_rows],
    )


class _Agg:
    __slots__ = ("address", "display_name", "received_count", "sent_count", "last_contact")

    def __init__(self, address: str) -> None:
        self.address = address
        self.display_name: str | None = None
        self.received_count = 0
        self.sent_count = 0
        self.last_contact: int | None = None

    def touch_name(self, name: str | None) -> None:
        # Keep the first non-empty display name we see; received names are
        # merged first (most authoritative — how the person addressed us).
        if name and not self.display_name:
            self.display_name = name

    def touch_time(self, ts: int | None) -> None:
        if ts is not None and (self.last_contact is None or ts > self.last_contact):
            self.last_contact = ts


def list_contacts(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    account: str | None = None,
    limit: int = 25,
) -> list[ContactSummary]:
    """Browsable, bidirectional contact list. Counts both mail received from
    an address (grouped `sender_addr`) and mail sent to it (recipients on
    `mailbox_role='sent'` rows) so people the user emails but who rarely
    reply still appear — a plain sender-only list would silently omit them.

    Sent-side recipients live as JSON in `recipients_to`/`recipients_cc`,
    which can't be cheaply GROUP-BY'd in SQL, so that half is a bounded
    Python merge over sent rows — fine at personal-mailbox scale.
    """
    limit = max(1, min(limit, 500))
    agg: dict[str, _Agg] = {}

    account_clause = ""
    params: dict[str, object] = {}
    if account:
        account_clause = " AND (account_uuid = :account OR account_name = :account)"
        params["account"] = account

    # Received side: bare sender_name is well-defined next to MAX() in SQLite —
    # it comes from the same row as the maximum date_received (latest name).
    received = conn.execute(
        f"""
        SELECT sender_addr, sender_name, COUNT(*) AS n, MAX(date_received) AS last
        FROM emails
        WHERE sender_addr IS NOT NULL AND sender_addr != ''{account_clause}
        GROUP BY lower(sender_addr)
        """,
        params,
    ).fetchall()
    for r in received:
        addr = r["sender_addr"]
        key = addr.lower()
        entry = agg.setdefault(key, _Agg(addr))
        entry.received_count += r["n"]
        entry.touch_name(r["sender_name"])
        entry.touch_time(r["last"])

    # Sent side: expand recipient JSON per sent row.
    sent_rows = conn.execute(
        f"""
        SELECT recipients_to, recipients_cc, date_sent
        FROM emails
        WHERE mailbox_role = 'sent'{account_clause}
        """,
        params,
    ).fetchall()
    for r in sent_rows:
        ts = r["date_sent"]
        pairs: list[str] = []
        for col in ("recipients_to", "recipients_cc"):
            raw = r[col]
            if raw:
                try:
                    pairs.extend(json.loads(raw))
                except (ValueError, TypeError):
                    continue
        for name, addr in getaddresses(pairs):
            if not addr:
                continue
            key = addr.lower()
            entry = agg.setdefault(key, _Agg(addr))
            entry.sent_count += 1
            entry.touch_name(name or None)
            entry.touch_time(ts)

    contacts = list(agg.values())
    if query:
        q = query.lower()
        contacts = [
            c
            for c in contacts
            if q in c.address.lower() or (c.display_name and q in c.display_name.lower())
        ]

    contacts.sort(
        key=lambda c: (c.received_count + c.sent_count, c.last_contact or 0), reverse=True
    )
    return [
        ContactSummary(
            address=c.address,
            display_name=c.display_name,
            received_count=c.received_count,
            sent_count=c.sent_count,
            total_count=c.received_count + c.sent_count,
            last_contact=c.last_contact,
        )
        for c in contacts[:limit]
    ]
