"""Read -> write resolution: turn a canonical message_id (+ optional hints)
into a live, read-back-verified Mail.app message handle the write layer can
safely mutate. This is the correctness-critical core of the server
(CLAUDE.md invariant #1).

Disk-derived account_uuid/mailbox_path — our index's own identity — have no
guaranteed mapping to the JXA-addressable account/mailbox NAME Mail.app
understands (a Mail account directory UUID is not the same value as the
account's scripting-visible name). Resolution bridges the two via account
name / email-address heuristics scoped in priority order — caller hint,
resolve_cache, the read-backend's own seed (sender address + mailbox name),
then a bounded broad scan — and never silently guesses among ambiguous
matches.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from cobos_apple_mail_mcp.core.errors import MultipleMatches, NotFound
from cobos_apple_mail_mcp.core.identity import (
    is_opaque_handle,
    normalize_message_id,
    to_mail_message_id,
)
from cobos_apple_mail_mcp.write.jxa_executor import JXAExecutor


@dataclass
class ResolvedMessage:
    """A successfully resolved, read-back-verified live Mail message."""

    canonical_id: str
    account_name: str
    mailbox_name: str
    mail_int_id: int


def _cache_get(conn: sqlite3.Connection, canonical_id: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT account_name, mailbox_name FROM resolve_cache "
        "WHERE canonical_id = ? ORDER BY last_verified DESC",
        (canonical_id,),
    ).fetchall()
    return [(r["account_name"], r["mailbox_name"]) for r in rows]


def _cache_put(
    conn: sqlite3.Connection, canonical_id: str, account_name: str, mailbox_name: str
) -> None:
    conn.execute(
        """
        INSERT INTO resolve_cache(canonical_id, account_name, mailbox_name, last_verified)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(canonical_id, account_name, mailbox_name)
        DO UPDATE SET last_verified = excluded.last_verified
        """,
        (canonical_id, account_name, mailbox_name, time.time()),
    )
    conn.commit()


def _index_seed(conn: sqlite3.Connection, canonical_id: str) -> tuple[str | None, str | None]:
    """The read-backend seed: if our index already has this message, its
    sender address narrows the JXA account search (Sent items: the sender
    IS one of the account's own addresses) and its mailbox name is directly
    JXA-addressable.
    """
    row = conn.execute(
        "SELECT sender_addr, mailbox_name FROM emails WHERE message_id = ? "
        "ORDER BY date_received DESC LIMIT 1",
        (canonical_id,),
    ).fetchone()
    if row is None:
        return None, None
    return row["sender_addr"], row["mailbox_name"]


def _candidate_key(candidate: dict) -> tuple[str, str, int]:
    return (candidate["account"], candidate["mailbox"], candidate["mailInternalId"])


def resolve(
    conn: sqlite3.Connection,
    jxa: JXAExecutor,
    canonical_id: str,
    *,
    account_hint: str | None = None,
    mailbox_hint: str | None = None,
) -> ResolvedMessage:
    """Resolve a canonical message id to exactly one live, verified Mail
    message. Raises NotFound (zero matches) or MultipleMatches (more than
    one candidate with no hint to disambiguate) — never guesses among
    ambiguous matches (CLAUDE.md invariant #1).
    """
    if is_opaque_handle(canonical_id):
        # Drafts / no-Message-ID mail: the amid: handle encodes a disk
        # location, but disk location isn't JXA-addressable either, and
        # there is no RFC822 Message-ID to search on. Callers must use an
        # explicit locator (e.g. subject) for these messages instead.
        raise NotFound(
            f"{canonical_id!r} has no RFC822 Message-ID (likely a draft); "
            "resolve it via an explicit locator, not message_id"
        )

    mail_target_id = to_mail_message_id(canonical_id)
    seed_account, seed_mailbox = _index_seed(conn, canonical_id)

    attempts: list[tuple[str | None, str | None]] = []
    if account_hint or mailbox_hint:
        attempts.append((account_hint, mailbox_hint or seed_mailbox))
    attempts.extend(_cache_get(conn, canonical_id))
    if seed_account or seed_mailbox:
        attempts.append((seed_account, seed_mailbox))

    candidates: list[dict] = []
    seen: set[tuple[str, str, int]] = set()

    for acct, mbox in attempts:
        result = jxa.call(
            "resolveMessage",
            {"accountHint": acct, "mailboxHint": mbox, "messageId": mail_target_id},
        )
        for cand in result.get("candidates", []):
            key = _candidate_key(cand)
            if key not in seen:
                seen.add(key)
                candidates.append(cand)
        if candidates:
            break  # a scoped attempt found something; no need to widen further

    if not candidates:
        result = jxa.call(
            "resolveMessage",
            {"accountHint": None, "mailboxHint": None, "messageId": mail_target_id},
        )
        for cand in result.get("candidates", []):
            key = _candidate_key(cand)
            if key not in seen:
                seen.add(key)
                candidates.append(cand)

    # Mandatory read-back verification (CLAUDE.md invariant #1): only trust
    # a candidate whose own reported Message-ID matches what we asked for.
    verified = [c for c in candidates if normalize_message_id(c["messageId"]) == canonical_id]

    if not verified:
        raise NotFound(f"no Mail message found for message_id={canonical_id!r}")

    if len(verified) > 1:
        raise MultipleMatches(
            f"{len(verified)} messages match message_id={canonical_id!r}; "
            "pass account/mailbox to disambiguate",
            candidates=[
                {"account": c["account"], "mailbox": c["mailbox"], "subject": c.get("subject")}
                for c in verified
            ],
        )

    chosen = verified[0]
    _cache_put(conn, canonical_id, chosen["account"], chosen["mailbox"])
    return ResolvedMessage(
        canonical_id=canonical_id,
        account_name=chosen["account"],
        mailbox_name=chosen["mailbox"],
        mail_int_id=chosen["mailInternalId"],
    )
