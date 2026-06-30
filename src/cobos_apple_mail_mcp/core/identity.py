"""Canonical message identity: the bridge between the fast-read path and the
AppleScript/JXA write path (CLAUDE.md invariant #1).

Apple Mail exposes two distinct identifiers per message:

- Mail's internal integer id (JXA `message.id()`).
- the RFC822 `Message-ID` header (JXA `message.messageId()`), stored *with*
  angle brackets, and queryable via `messages whose message id is "<...>"`.

We use the RFC822 Message-ID, normalized, as the canonical id exposed to MCP
clients, because it is the one value extractable from both the on-disk
`.emlx` plist and the live AppleScript/JXA object model. Drafts and other
messages with no usable Message-ID get an opaque `amid:` handle instead.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from enum import Enum

_AMID_PREFIX = "amid:v1:"
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_message_id(raw: str) -> str:
    """Normalize an RFC822 Message-ID to its canonical (bracket-stripped) form.

    Case is preserved (RFC 5322 msg-id halves are case-sensitive tokens) —
    never lowercase. Accepts bracketed or bare input; output is always bare.
    """
    value = _WHITESPACE_RE.sub("", raw.strip())
    if value.startswith("<") and value.endswith(">") and len(value) >= 2:
        value = value[1:-1]
    return value


def to_mail_message_id(canonical_id: str) -> str:
    """Re-add the angle brackets Mail.app expects for `whose message id is X`."""
    if canonical_id.startswith("<"):
        return canonical_id
    return f"<{canonical_id}>"


def is_opaque_handle(canonical_id: str) -> bool:
    return canonical_id.startswith(_AMID_PREFIX)


@dataclass(frozen=True)
class OpaqueHandlePayload:
    """The decoded contents of an `amid:` handle (see CLAUDE.md invariant #1)."""

    account_uuid: str
    mailbox_path: str
    rowid: int
    emlx_mtime: float

    def encode(self) -> str:
        raw = f"{self.account_uuid}|{self.mailbox_path}|{self.rowid}|{self.emlx_mtime}"
        token = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
        return f"{_AMID_PREFIX}{token}"

    @classmethod
    def decode(cls, canonical_id: str) -> OpaqueHandlePayload:
        if not canonical_id.startswith(_AMID_PREFIX):
            raise ValueError(f"not an opaque handle: {canonical_id!r}")
        token = canonical_id[len(_AMID_PREFIX) :]
        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + padding).decode("utf-8")
        account_uuid, mailbox_path, rowid, emlx_mtime = raw.split("|", 3)
        return cls(
            account_uuid=account_uuid,
            mailbox_path=mailbox_path,
            rowid=int(rowid),
            emlx_mtime=float(emlx_mtime),
        )


def make_opaque_handle(account_uuid: str, mailbox_path: str, rowid: int, emlx_mtime: float) -> str:
    """Mint an `amid:` handle for a message with no usable RFC822 Message-ID
    (drafts, malformed mail). Ephemeral by design: changes when the message
    is sent and gets a real Message-ID (see HandleSuperseded).
    """
    return OpaqueHandlePayload(account_uuid, mailbox_path, rowid, emlx_mtime).encode()


class RefSource(str, Enum):
    ENVELOPE = "envelope"
    EMLX = "emlx"
    JXA = "jxa"


@dataclass
class MessageRef:
    """The inter-layer contract: a read tool produces this, `guard()` and the
    write backend consume it. Location hints (account/mailbox/rowid/mail_int_id)
    flow read -> write for free, letting resolution skip a broad Mail scan.
    """

    canonical_id: str
    account_uuid: str | None = None
    account_name: str | None = None
    mailbox_path: str | None = None
    mailbox_name: str | None = None
    rowid: int | None = None
    mail_int_id: int | None = None
    source: RefSource = RefSource.ENVELOPE

    @property
    def is_opaque(self) -> bool:
        return is_opaque_handle(self.canonical_id)

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "message_id": self.canonical_id,
            "account": self.account_name or self.account_uuid,
            "mailbox": self.mailbox_name or self.mailbox_path,
        }

    @classmethod
    def from_rfc822(
        cls,
        message_id: str,
        *,
        account_uuid: str | None = None,
        account_name: str | None = None,
        mailbox_path: str | None = None,
        mailbox_name: str | None = None,
        rowid: int | None = None,
        source: RefSource = RefSource.ENVELOPE,
    ) -> MessageRef:
        return cls(
            canonical_id=normalize_message_id(message_id),
            account_uuid=account_uuid,
            account_name=account_name,
            mailbox_path=mailbox_path,
            mailbox_name=mailbox_name,
            rowid=rowid,
            source=source,
        )
