"""Shared pydantic models for every tool's input/output. Single source of
truth so `tools/*`, `read/*`, `write/*`, and `resources/*` agree on shape.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Identity / locators
# ---------------------------------------------------------------------------


class MessageRefModel(BaseModel):
    """Wire form of core.identity.MessageRef."""

    message_id: str
    account: str | None = None
    mailbox: str | None = None


class Locator(BaseModel):
    """Opt-in fallback locator for write tools when message_id isn't known.

    Demoted relative to message_id per CLAUDE.md invariant #1: even when used,
    resolution goes through a Message-ID first before any mutation.
    """

    subject_keyword: str | None = None
    sender: str | None = None
    account: str | None = None
    mailbox: str | None = None
    date_from: str | None = None
    date_to: str | None = None


# ---------------------------------------------------------------------------
# Accounts / mailboxes
# ---------------------------------------------------------------------------


class Account(BaseModel):
    name: str
    uuid: str | None = None
    addresses: list[str] = Field(default_factory=list)


class Mailbox(BaseModel):
    name: str
    account: str
    role: str | None = None  # inbox|sent|drafts|trash|junk|archive|other
    unread_count: int = 0
    total_count: int | None = None
    path: str | None = None


# ---------------------------------------------------------------------------
# Email summaries / full content
# ---------------------------------------------------------------------------


class Attachment(BaseModel):
    filename: str
    mime_type: str | None = None
    size: int | None = None
    content_id: str | None = None
    saved_path: str | None = None


class EmailSummary(BaseModel):
    message_ref: MessageRefModel
    subject: str | None = None
    sender_name: str | None = None
    sender_addr: str | None = None
    date_received: int | None = None
    date_sent: int | None = None
    is_read: bool = False
    is_flagged: bool = False
    is_answered: bool = False
    flag_color: str | None = None  # red|orange|yellow|green|blue|purple|gray, None if unflagged
    attachment_count: int = 0
    snippet: str | None = None
    mailbox: str | None = None
    account: str | None = None


class EmailFull(EmailSummary):
    recipients_to: list[str] = Field(default_factory=list)
    recipients_cc: list[str] = Field(default_factory=list)
    body_plain: str | None = None
    body_html: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)


class EmailLink(BaseModel):
    url: str
    text: str | None = None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class SearchScope(str, Enum):
    all = "all"
    subject = "subject"
    sender = "sender"
    body = "body"
    attachments = "attachments"


class SearchMode(str, Enum):
    keyword = "keyword"
    semantic = "semantic"
    hybrid = "hybrid"


class SearchHit(BaseModel):
    message_ref: MessageRefModel
    score: float
    subject: str | None = None
    sender_name: str | None = None
    sender_addr: str | None = None
    date_received: int | None = None
    mailbox: str | None = None
    account: str | None = None
    is_read: bool = False
    is_flagged: bool = False
    flag_color: str | None = None
    attachment_count: int = 0
    snippet_html: str | None = None
    thread_id: int | None = None


class SearchResult(BaseModel):
    query: str
    mode: SearchMode
    scope: SearchScope
    total_estimated: int
    returned: int
    offset: int
    hits: list[SearchHit] = Field(default_factory=list)
    timing_ms: float
    degraded: bool = False
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Threading
# ---------------------------------------------------------------------------


class ThreadNode(BaseModel):
    message_ref: MessageRefModel | None = None  # None for JWZ phantom containers
    subject: str | None = None
    sender: str | None = None
    date: int | None = None
    is_read: bool | None = None
    snippet: str | None = None
    children: list[ThreadNode] = Field(default_factory=list)


ThreadNode.model_rebuild()


class EmailThread(BaseModel):
    thread_id: int
    subject: str
    message_count: int
    participants: list[str] = Field(default_factory=list)
    date_span: tuple[int, int] | None = None
    unread_count: int = 0
    awaiting_reply: bool = False
    root: ThreadNode


# ---------------------------------------------------------------------------
# Knowledge / analytics / triage
# ---------------------------------------------------------------------------


class SenderCount(BaseModel):
    sender_addr: str
    sender_name: str | None = None
    count: int
    unread_count: int = 0
    last_received: int | None = None


class AccountCount(BaseModel):
    account: str
    total: int
    unread: int


class InboxOverview(BaseModel):
    total: int
    unread: int
    flagged: int
    today: int
    this_week: int
    top_unread_senders: list[SenderCount] = Field(default_factory=list)
    needs_response_count: int = 0
    awaiting_reply_count: int = 0
    newest_unread: list[EmailSummary] = Field(default_factory=list)
    by_account: list[AccountCount] = Field(default_factory=list)
    index_stale: bool = False


class AwaitingReplyItem(BaseModel):
    message_ref: MessageRefModel
    recipient: str
    subject: str | None = None
    sent_at: int
    days_waiting: float


class NeedsResponseItem(BaseModel):
    message_ref: MessageRefModel
    sender: str | None = None
    subject: str | None = None
    score: int
    urgency: str  # HIGH|MEDIUM|NORMAL
    reasons: list[str] = Field(default_factory=list)
    received_at: int


class Statistics(BaseModel):
    scope: str
    date_range_days: int
    data: dict[str, Any] = Field(default_factory=dict)


class Contact(BaseModel):
    address: str
    display_name: str | None = None
    message_count: int = 0
    last_contact: int | None = None
    recent_messages: list[EmailSummary] = Field(default_factory=list)


class ContactSummary(BaseModel):
    """One row of the browsable, bidirectional contact list. Unlike Contact,
    counts both mail received from and mail sent to the address, and omits
    recent_messages (too heavy for a list projection)."""

    address: str
    display_name: str | None = None
    received_count: int = 0
    sent_count: int = 0
    total_count: int = 0
    last_contact: int | None = None


# ---------------------------------------------------------------------------
# Index build / status
# ---------------------------------------------------------------------------


class IndexBuildResult(BaseModel):
    added: int = 0
    changed: int = 0
    deleted: int = 0
    moved: int = 0
    failed: int = 0
    duration_sec: float = 0.0
    full: bool = False


class IndexStatus(BaseModel):
    mail_dir: str | None = None
    envelope_index_available: bool = False
    total_indexed: int
    pending_added: int
    pending_changed: int
    pending_deleted: int
    dead_letter_count: int
    embed_total: int = 0
    embed_done: int = 0
    last_full_build: float | None = None
    last_watch_tick: float | None = None
    stale: bool


# ---------------------------------------------------------------------------
# Safety / dry-run / undo
# ---------------------------------------------------------------------------


class AffectedMessage(BaseModel):
    message_ref: MessageRefModel
    subject: str | None = None
    from_mailbox: str | None = None
    to_mailbox: str | None = None


class Preview(BaseModel):
    dry_run: bool = True
    operation: str
    would_affect: list[AffectedMessage] = Field(default_factory=list)
    count: int
    blocked_by: list[str] = Field(default_factory=list)
    reversible: bool
    undo_hint: str | None = None


class OperationResult(BaseModel):
    operation: str
    succeeded: list[MessageRefModel] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)  # message_id -> reason
    count: int
    batch_id: str | None = None
    dry_run: bool = False
    preview: Preview | None = None


class UndoResult(BaseModel):
    batch_id: str
    undone: list[MessageRefModel] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)
    count: int
