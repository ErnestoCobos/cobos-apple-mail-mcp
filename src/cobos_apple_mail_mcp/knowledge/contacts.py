"""email://contacts/{addr} projection — derived purely from the index. No
separate contacts database; integration with macOS Contacts/AddressBook is
out of scope (CLAUDE.md knowledge map: Threading and knowledge).
"""

from __future__ import annotations

import sqlite3

from cobos_apple_mail_mcp.core.models import Contact
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
