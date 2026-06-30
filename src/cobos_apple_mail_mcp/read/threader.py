"""JWZ-style conversation threading (CLAUDE.md knowledge map: Threading and
knowledge), run entirely against the index (message_id/in_reply_to/
references_ids columns already extracted at index time) — no .emlx reparse.

Containers may be "phantom" (a referenced Message-ID with no corresponding
row in our index, e.g. a message in another mailbox we don't index, or one
the sender's References chain mentions that was never delivered to us).
Phantoms still group their real children together correctly, exactly like
the original JWZ algorithm. A root-level fallback also merges orphan
threads that share a normalized subject (Re:/Fwd: stripped), for mail with
missing or broken References/In-Reply-To headers.

Threading is re-run over the whole index on each full build rather than
incrementally re-threading only touched threads — at personal-mailbox scale
(well under a million messages) an in-memory rebuild is a few hundred
milliseconds, so the extra complexity of a touched-threads-only path isn't
worth it yet; revisit if real-world timings say otherwise.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from cobos_apple_mail_mcp.core.models import EmailThread, MessageRefModel, ThreadNode


@dataclass
class _Container:
    message_id: str
    row: sqlite3.Row | None = None
    parent: _Container | None = None
    children: list[_Container] = field(default_factory=list)


def _creates_cycle(candidate_parent: _Container, child: _Container) -> bool:
    current: _Container | None = candidate_parent
    while current is not None:
        if current is child:
            return True
        current = current.parent
    return False


def _link(parent: _Container, child: _Container) -> None:
    if child is parent or _creates_cycle(parent, child):
        return
    child.parent = parent
    if child not in parent.children:
        parent.children.append(child)


def _build_containers(rows: list[sqlite3.Row]) -> dict[str, _Container]:
    containers: dict[str, _Container] = {}

    def get_or_create(message_id: str) -> _Container:
        container = containers.get(message_id)
        if container is None:
            container = _Container(message_id=message_id)
            containers[message_id] = container
        return container

    for row in rows:
        mid = row["message_id"]
        cont = get_or_create(mid)
        cont.row = row  # a real message always supersedes a phantom placeholder

        refs = (row["references_ids"] or "").split()
        in_reply_to = row["in_reply_to"]
        chain = list(refs)
        if in_reply_to and in_reply_to not in chain:
            chain.append(in_reply_to)

        prev: _Container | None = None
        for ref_id in chain:
            ref_cont = get_or_create(ref_id)
            if prev is not None and ref_cont.parent is None:
                _link(prev, ref_cont)
            prev = ref_cont
        if prev is not None and cont.parent is None:
            _link(prev, cont)

    return containers


def _root_date(container: _Container) -> int:
    if container.row is not None:
        return container.row["date_received"] or 0
    return min((_root_date(c) for c in container.children), default=0)


def _merge_orphan_roots_by_subject(roots: list[_Container]) -> list[_Container]:
    """JWZ's "gather subroots by subject" fallback, restricted to root-level
    containers only (merging deeper in the tree risks conflating unrelated
    threads that happen to share a subject)."""
    anchors: dict[str, _Container] = {}
    merged: list[_Container] = []
    for root in sorted(roots, key=_root_date):
        subject = root.row["subject_norm"] if root.row is not None else None
        if not subject:
            merged.append(root)
            continue
        anchor = anchors.get(subject)
        if anchor is None:
            anchors[subject] = root
            merged.append(root)
        else:
            _link(anchor, root)
    return merged


def jwz_thread(rows: list[sqlite3.Row]) -> list[_Container]:
    """Build the conversation forest for a set of index rows. Returns the
    root containers (each the head of one conversation tree)."""
    containers = _build_containers(rows)
    roots = [c for c in containers.values() if c.parent is None]
    roots = _merge_orphan_roots_by_subject(roots)
    for container in containers.values():
        container.children.sort(key=lambda c: (c.row["date_received"] if c.row else 0) or 0)
    return roots


def _first_real_row(container: _Container) -> sqlite3.Row | None:
    if container.row is not None:
        return container.row
    for child in container.children:
        found = _first_real_row(child)
        if found is not None:
            return found
    return None


def index_threads(conn: sqlite3.Connection) -> int:
    """Recompute thread_id/thread_root_id/thread_position for every row.
    thread_id is the `emails.id` of the earliest real message in the tree —
    stable across rebuilds as long as that row's id doesn't change.
    """
    rows = conn.execute(
        "SELECT id, message_id, in_reply_to, references_ids, subject_norm, date_received "
        "FROM emails"
    ).fetchall()
    roots = jwz_thread(rows)

    updates: list[tuple[int, int, int, int]] = []

    def walk(container: _Container, thread_root_id: int, position: list[int]) -> None:
        if container.row is not None:
            updates.append((thread_root_id, thread_root_id, position[0], container.row["id"]))
            position[0] += 1
        for child in container.children:
            walk(child, thread_root_id, position)

    for root in roots:
        anchor_row = _first_real_row(root)
        if anchor_row is None:
            continue
        walk(root, anchor_row["id"], [0])

    conn.executemany(
        "UPDATE emails SET thread_id = ?, thread_root_id = ?, thread_position = ? WHERE id = ?",
        updates,
    )
    conn.commit()
    return len(updates)


def _container_to_node(container: _Container) -> ThreadNode:
    if container.row is not None:
        row = container.row
        ref = MessageRefModel(
            message_id=row["message_id"],
            account=row["account_name"] or row["account_uuid"],
            mailbox=row["mailbox_name"],
        )
        node = ThreadNode(
            message_ref=ref,
            subject=row["subject"],
            sender=row["sender_addr"],
            date=row["date_received"],
            is_read=bool(row["flag_read"]),
            snippet=row["snippet"],
        )
    else:
        node = ThreadNode()
    node.children = [_container_to_node(c) for c in container.children]
    return node


def get_email_thread(
    conn: sqlite3.Connection, *, message_id: str | None = None, thread_id: int | None = None
) -> EmailThread:
    from cobos_apple_mail_mcp.core.errors import NotFound

    if thread_id is None:
        if message_id is None:
            raise ValueError("either message_id or thread_id is required")
        row = conn.execute(
            "SELECT thread_id FROM emails WHERE message_id = ? "
            "ORDER BY date_received DESC LIMIT 1",
            (message_id,),
        ).fetchone()
        if row is None or row["thread_id"] is None:
            raise NotFound(f"no thread found for message_id={message_id!r}")
        thread_id = row["thread_id"]

    rows = conn.execute(
        "SELECT * FROM emails WHERE thread_id = ? ORDER BY thread_position", (thread_id,)
    ).fetchall()
    if not rows:
        raise NotFound(f"no thread found for thread_id={thread_id!r}")

    roots = jwz_thread(list(rows))
    root_node = _container_to_node(roots[0])

    participants = sorted({r["sender_addr"] for r in rows if r["sender_addr"]})
    dates = [r["date_received"] for r in rows if r["date_received"] is not None]
    unread_count = sum(1 for r in rows if not r["flag_read"])
    subject = next((r["subject"] for r in rows if r["subject"]), "")
    last_row = max(rows, key=lambda r: r["date_received"] or 0)

    return EmailThread(
        thread_id=thread_id,
        subject=subject or "",
        message_count=len(rows),
        participants=participants,
        date_span=(min(dates), max(dates)) if dates else None,
        unread_count=unread_count,
        awaiting_reply=(last_row["mailbox_role"] == "sent"),
        root=root_node,
    )
