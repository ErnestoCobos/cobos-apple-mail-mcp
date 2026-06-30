"""save_email_attachment: extract one attachment and save it to an explicit
path (CLAUDE.md knowledge map: Tools reference). Resolution is by canonical
message_id, not subject_keyword. Effectively read-only (extracts from
.emlx; never mutates Mail), so unlike the rest of write/* it is not gated
by --read-only or core.safety.guard() — matching tools/reading.py's
get_email_attachment, which this complements with an explicit save_path
(patrickfreyer's original tool shape) rather than a default directory.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cobos_apple_mail_mcp.core.errors import NotFound
from cobos_apple_mail_mcp.core.identity import normalize_message_id
from cobos_apple_mail_mcp.core.models import Attachment
from cobos_apple_mail_mcp.core.paths import validate_output_path
from cobos_apple_mail_mcp.read.emlx_parser import extract_attachment_bytes


def save_email_attachment(
    conn: sqlite3.Connection,
    message_id: str,
    attachment_name: str,
    save_path: str,
    *,
    account: str | None = None,
    mailbox: str | None = None,
) -> Attachment:
    mid = normalize_message_id(message_id)
    where = ["message_id = :mid"]
    params: dict[str, object] = {"mid": mid}
    if account:
        where.append("(account_uuid = :account OR account_name = :account)")
        params["account"] = account
    if mailbox:
        where.append("(mailbox_name = :mailbox OR mailbox_role = :mailbox)")
        params["mailbox"] = mailbox

    row = conn.execute(
        f"SELECT emlx_path FROM emails WHERE {' AND '.join(where)} ORDER BY date_received DESC",
        params,
    ).fetchone()
    if row is None or not row["emlx_path"]:
        raise NotFound(f"no email found for message_id={mid!r}")

    path = Path(row["emlx_path"])
    if not path.exists():
        raise NotFound(f"source .emlx is no longer on disk for message_id={mid!r}")

    data = extract_attachment_bytes(path, attachment_name)
    if data is None:
        raise NotFound(f"attachment {attachment_name!r} not found on message {mid!r}")

    target = validate_output_path(save_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o600)
    return Attachment(filename=attachment_name, size=len(data), saved_path=str(target))
