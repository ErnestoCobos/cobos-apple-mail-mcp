"""Build/rebuild/status the derived `index.db` from `.emlx` files on disk.

The key trick (CLAUDE.md knowledge map: Indexing and watch): a `.emlx`
filename's numeric stem *is* the Envelope Index ROWID, so the indexer can
diff filesystem path sets (mtime/size) against what's already in `emails`
to classify added/changed/deleted/**moved** without re-parsing everything,
and a delete+add sharing a ROWID is a metadata-only move, not a reparse.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import traceback
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from cobos_apple_mail_mcp.core.identity import make_opaque_handle
from cobos_apple_mail_mcp.core.models import IndexBuildResult, IndexStatus
from cobos_apple_mail_mcp.core.text import normalize_subject
from cobos_apple_mail_mcp.read.emlx_parser import ParsedEmlx, parse_emlx_file, rowid_from_filename
from cobos_apple_mail_mcp.read.envelope_reader import (
    classify_mailbox_role,
    find_envelope_index,
    find_mail_directory,
)
from cobos_apple_mail_mcp.storage.database import get_sync_state, set_sync_state

BATCH_SIZE = 500


@dataclass
class ScanEntry:
    path: Path
    mtime: float
    size: int
    rowid: int
    account_uuid: str
    mailbox_name: str
    mailbox_path: str


def _mbox_chain(messages_dir: Path, account_dir: Path) -> list[Path]:
    chain: list[Path] = []
    current = messages_dir.parent
    while current != account_dir and current != current.parent:
        if current.name.endswith(".mbox"):
            chain.append(current)
        current = current.parent
    chain.reverse()
    return chain


def scan_emlx_files(
    mail_dir: Path, exclude_mailboxes: set[str] | None = None
) -> Iterator[ScanEntry]:
    """Walk every `Messages/` directory under each account, deriving the
    enclosing mailbox from the nearest `.mbox` ancestor (so nested mailbox
    folders don't get double-counted by a naive `.mbox`-then-`Messages` walk).
    """
    from cobos_apple_mail_mcp.read.envelope_reader import list_account_directories

    exclude = {m.lower() for m in (exclude_mailboxes or ())}
    for account_dir in list_account_directories(mail_dir):
        for messages_dir in account_dir.rglob("Messages"):
            if not messages_dir.is_dir():
                continue
            chain = _mbox_chain(messages_dir, account_dir)
            if not chain:
                continue
            mailbox_name = chain[-1].stem
            if mailbox_name.lower() in exclude:
                continue
            mailbox_path = "/".join(p.stem for p in chain)
            for f in messages_dir.iterdir():
                if not f.is_file():
                    continue
                rowid = rowid_from_filename(f)
                if rowid is None:
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                yield ScanEntry(
                    path=f,
                    mtime=st.st_mtime,
                    size=st.st_size,
                    rowid=rowid,
                    account_uuid=account_dir.name,
                    mailbox_name=mailbox_name,
                    mailbox_path=mailbox_path,
                )


@dataclass
class InventoryDiff:
    added: list[ScanEntry]
    changed: list[ScanEntry]
    deleted: list[str]
    moved: list[tuple[str, ScanEntry]]


def inventory_diff(
    conn: sqlite3.Connection, mail_dir: Path, exclude_mailboxes: set[str] | None = None
) -> InventoryDiff:
    disk_entries = {str(e.path): e for e in scan_emlx_files(mail_dir, exclude_mailboxes)}
    db_rows = conn.execute(
        "SELECT emlx_path, emlx_mtime, emlx_size, emlx_rowid FROM emails "
        "WHERE emlx_path IS NOT NULL"
    ).fetchall()
    db_index = {row["emlx_path"]: row for row in db_rows}

    disk_paths = set(disk_entries)
    db_paths = set(db_index)

    added_paths = disk_paths - db_paths
    deleted_paths = db_paths - disk_paths
    changed_paths = {
        p
        for p in disk_paths & db_paths
        if disk_entries[p].mtime != db_index[p]["emlx_mtime"]
        or disk_entries[p].size != db_index[p]["emlx_size"]
    }

    deleted_by_rowid: dict[int, str] = {
        db_index[p]["emlx_rowid"]: p for p in deleted_paths if db_index[p]["emlx_rowid"] is not None
    }

    moved: list[tuple[str, ScanEntry]] = []
    truly_added: list[ScanEntry] = []
    for p in added_paths:
        entry = disk_entries[p]
        old_path = deleted_by_rowid.get(entry.rowid)
        if old_path is not None:
            moved.append((old_path, entry))
        else:
            truly_added.append(entry)

    moved_old_paths = {old for old, _ in moved}
    truly_deleted = [p for p in deleted_paths if p not in moved_old_paths]
    changed_entries = [disk_entries[p] for p in changed_paths]

    return InventoryDiff(
        added=truly_added, changed=changed_entries, deleted=truly_deleted, moved=moved
    )


def _serialize_addresses(addrs: list) -> tuple[str, str]:  # noqa: ANN001
    rendered = [f"{a.name} <{a.addr}>" if a.name else a.addr for a in addrs]
    return json.dumps(rendered), ", ".join(rendered)


def _row_from_parsed(
    entry: ScanEntry,
    parsed: ParsedEmlx,
    account_names: dict[str, str] | None = None,
) -> dict:
    to_json, to_flat = _serialize_addresses(parsed.to)
    cc_json, cc_flat = _serialize_addresses(parsed.cc)
    sender_name = parsed.sender.name if parsed.sender else None
    sender_addr = parsed.sender.addr if parsed.sender else None
    # Every row gets a non-null canonical id: the RFC822 Message-ID when
    # present, otherwise a freshly-minted amid: handle (drafts and other
    # mail with no usable Message-ID) — see core/identity.py.
    canonical_id = parsed.message_id or make_opaque_handle(
        entry.account_uuid, entry.mailbox_path, entry.rowid, entry.mtime
    )
    account_name = (account_names or {}).get(entry.account_uuid, entry.account_uuid)
    return {
        "emlx_rowid": entry.rowid,
        "emlx_path": str(entry.path),
        "emlx_mtime": entry.mtime,
        "emlx_size": entry.size,
        "account_uuid": entry.account_uuid,
        "account_name": account_name,
        "mailbox_url": entry.mailbox_path,
        "mailbox_name": entry.mailbox_name,
        "mailbox_role": classify_mailbox_role(entry.mailbox_name),
        "message_id": canonical_id,
        "in_reply_to": parsed.in_reply_to,
        "references_ids": " ".join(parsed.references) if parsed.references else None,
        "subject": parsed.subject,
        "subject_norm": normalize_subject(parsed.subject),
        "sender_name": sender_name,
        "sender_addr": sender_addr,
        "recipients_to": to_json,
        "recipients_cc": cc_json,
        "recipients_all": f"{to_flat} {cc_flat}".strip(),
        "date_sent": parsed.date_sent,
        "date_received": parsed.date_received,
        "snippet": parsed.snippet,
        "body_plain": parsed.body_plain,
        "flag_read": int(parsed.is_read),
        "flag_flagged": int(parsed.is_flagged),
        "flag_answered": int(parsed.is_answered),
        "flag_draft": int(parsed.is_draft),
        "flag_bulk": int(parsed.is_bulk),
        # flag_color is NULL for a freshly-parsed row and only ever set by our
        # own set_flag_color write (optimistic index update). It is deliberately
        # NOT in the ON CONFLICT update below, so a reindex of an already-colored
        # message preserves the color rather than wiping it — the on-disk
        # Envelope Index doesn't carry the per-color flagIndex in an immutable
        # read (empirically it stores 1 for any flagged message), so disk is not
        # a usable source. See Apple-Mail-on-disk-format / Search.
        "flag_color": None,
        "attachment_count": parsed.attachment_count,
        "attachment_names": json.dumps(parsed.attachment_names),
        "indexed_at": time.time(),
    }


_UPSERT_SQL = """
INSERT INTO emails (
  emlx_rowid, emlx_path, emlx_mtime, emlx_size, account_uuid, account_name,
  mailbox_url, mailbox_name, mailbox_role, message_id, in_reply_to, references_ids,
  subject, subject_norm, sender_name, sender_addr, recipients_to, recipients_cc,
  recipients_all, date_sent, date_received, snippet, body_plain,
  flag_read, flag_flagged, flag_answered, flag_draft, flag_bulk, flag_color, attachment_count,
  attachment_names, indexed_at
) VALUES (
  :emlx_rowid, :emlx_path, :emlx_mtime, :emlx_size, :account_uuid, :account_name,
  :mailbox_url, :mailbox_name, :mailbox_role, :message_id, :in_reply_to, :references_ids,
  :subject, :subject_norm, :sender_name, :sender_addr, :recipients_to, :recipients_cc,
  :recipients_all, :date_sent, :date_received, :snippet, :body_plain,
  :flag_read, :flag_flagged, :flag_answered, :flag_draft, :flag_bulk, :flag_color,
  :attachment_count, :attachment_names, :indexed_at
)
ON CONFLICT(emlx_path) DO UPDATE SET
  emlx_rowid=excluded.emlx_rowid, emlx_mtime=excluded.emlx_mtime, emlx_size=excluded.emlx_size,
  account_uuid=excluded.account_uuid, account_name=excluded.account_name,
  mailbox_url=excluded.mailbox_url, mailbox_name=excluded.mailbox_name,
  mailbox_role=excluded.mailbox_role, message_id=excluded.message_id,
  in_reply_to=excluded.in_reply_to, references_ids=excluded.references_ids,
  subject=excluded.subject, subject_norm=excluded.subject_norm,
  sender_name=excluded.sender_name, sender_addr=excluded.sender_addr,
  recipients_to=excluded.recipients_to, recipients_cc=excluded.recipients_cc,
  recipients_all=excluded.recipients_all, date_sent=excluded.date_sent,
  date_received=excluded.date_received, snippet=excluded.snippet, body_plain=excluded.body_plain,
  flag_read=excluded.flag_read, flag_flagged=excluded.flag_flagged,
  flag_answered=excluded.flag_answered, flag_draft=excluded.flag_draft,
  flag_bulk=excluded.flag_bulk,
  attachment_count=excluded.attachment_count, attachment_names=excluded.attachment_names,
  indexed_at=excluded.indexed_at, embed_state=0
"""


def _record_failure(conn: sqlite3.Connection, path: Path, exc: Exception) -> None:
    now = time.time()
    conn.execute(
        """
        INSERT INTO failed_index_jobs(emlx_path, reason, traceback, attempts, first_seen, last_seen)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(emlx_path) DO UPDATE SET
          reason=excluded.reason, traceback=excluded.traceback,
          attempts=failed_index_jobs.attempts + 1, last_seen=excluded.last_seen
        """,
        (str(path), str(exc), traceback.format_exc(), now, now),
    )


def _flush_batch(conn: sqlite3.Connection, batch: list[dict], recovered_paths: list[str]) -> int:
    """UPSERT one batch. Falls back to one-row-at-a-time + dead-lettering on
    failure, so a single row the parser's sanitization didn't catch (a new
    edge case in real-world mail, not just a parse-time exception) can never
    abort the rest of an otherwise-healthy batch — same isolation contract
    `_index_entries`'s per-entry try/except already gives parse failures.
    """
    try:
        conn.executemany(_UPSERT_SQL, batch)
        conn.executemany(
            "DELETE FROM failed_index_jobs WHERE emlx_path = ?", [(p,) for p in recovered_paths]
        )
        conn.commit()
        return len(batch)
    except Exception:  # noqa: BLE001 - isolate the one bad row, don't lose the whole batch
        # The UPSERT is idempotent (ON CONFLICT DO UPDATE), so re-applying rows
        # that already landed as part of the failed executemany is harmless —
        # no need to track how far it got before raising.
        ok = 0
        for row, path in zip(batch, recovered_paths, strict=True):
            try:
                conn.execute(_UPSERT_SQL, row)
                conn.execute("DELETE FROM failed_index_jobs WHERE emlx_path = ?", (path,))
                ok += 1
            except Exception as exc:  # noqa: BLE001
                _record_failure(conn, Path(path), exc)
        conn.commit()
        return ok


def _index_entries(
    conn: sqlite3.Connection,
    entries: list[ScanEntry],
    account_names: dict[str, str] | None = None,
) -> tuple[int, int]:
    """Parse + UPSERT entries in batches. Returns (succeeded, failed).

    A path that succeeds here is cleared from `failed_index_jobs` if it was
    previously dead-lettered (e.g. a transient read mid-write by Mail.app on
    a prior `--watch` tick) — otherwise the dead-letter table would
    accumulate entries that are no longer actually failing.
    """
    succeeded = 0
    failed = 0
    batch: list[dict] = []
    recovered_paths: list[str] = []
    for entry in entries:
        try:
            parsed = parse_emlx_file(entry.path)
            if parsed is None:
                continue
            batch.append(_row_from_parsed(entry, parsed, account_names))
            recovered_paths.append(str(entry.path))
        except Exception as exc:  # noqa: BLE001 - one bad message must not abort the build
            _record_failure(conn, entry.path, exc)
            failed += 1
            continue
        if len(batch) >= BATCH_SIZE:
            ok = _flush_batch(conn, batch, recovered_paths)
            succeeded += ok
            failed += len(batch) - ok
            batch, recovered_paths = [], []
    if batch:
        ok = _flush_batch(conn, batch, recovered_paths)
        succeeded += ok
        failed += len(batch) - ok
    return succeeded, failed


def _drop_fts_triggers(conn: sqlite3.Connection) -> None:
    for trigger in ("emails_ai", "emails_ad", "emails_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def _recreate_fts_triggers(conn: sqlite3.Connection) -> None:
    from cobos_apple_mail_mcp.storage.migrations import FTS_TRIGGERS_SQL

    conn.executescript(FTS_TRIGGERS_SQL)
    conn.commit()


def _rebuild_fts_index(conn: sqlite3.Connection) -> None:
    """Repopulate emails_fts from emails. emails_fts is a self-contained
    (non-external-content) FTS5 table — see storage/migrations.py
    EMAILS_FTS_DDL — so a plain DELETE + INSERT...SELECT is all that's
    needed; no name-matching requirement to work around.
    """
    conn.execute("DELETE FROM emails_fts")
    conn.execute(
        """
        INSERT INTO emails_fts(rowid, subject, sender, recipients, body, attachments)
        SELECT id, subject,
               coalesce(sender_name,'')||' '||coalesce(sender_addr,''),
               recipients_all, body_plain, attachment_names
        FROM emails
        """
    )
    conn.execute("INSERT INTO emails_fts(emails_fts) VALUES('optimize')")
    conn.commit()


def _rebuild_trigram_index(conn: sqlite3.Connection) -> None:
    """Populate the optional substring-search companion table. Only
    refreshed on a full rebuild (not incrementally on --watch ticks) — an
    accepted lag for an opt-in fallback feature; `index rebuild` refreshes it.
    """
    from cobos_apple_mail_mcp.storage.migrations import ensure_trigram_table

    ensure_trigram_table(conn)
    conn.execute("DELETE FROM emails_trgm")
    conn.execute(
        """
        INSERT INTO emails_trgm(rowid, subject, sender, body)
        SELECT id, subject,
               coalesce(sender_name,'')||' '||coalesce(sender_addr,''),
               body_plain
        FROM emails
        """
    )
    conn.commit()


def _backfill_account_names(conn: sqlite3.Connection, account_names: dict[str, str]) -> None:
    """Update `account_name` for rows already indexed under an old (or
    never-resolved) name -- account-name resolution depends only on
    `account_uuid`, never on message content, so this is a cheap indexed
    UPDATE per known account rather than a full reparse. Runs on every
    build (not just `--full`) so a first `apple-mail-mcp index build`
    against an existing index.db picks up real names without waiting for
    every message to individually change.
    """
    if not account_names:
        return
    for uuid, name in account_names.items():
        conn.execute(
            "UPDATE emails SET account_name = ? WHERE account_uuid = ? AND account_name != ?",
            (name, uuid, name),
        )
    conn.commit()


def build_index(
    conn: sqlite3.Connection,
    mail_dir: Path,
    *,
    exclude_mailboxes: set[str] | None = None,
    full: bool = False,
    enable_trigram: bool = False,
    accounts_db_path: Path | None = None,
) -> IndexBuildResult:
    """Run one indexing pass: diff the filesystem against `emails`, parse
    only what changed, and apply deletes/moves. Crash-safe: each batch
    commits independently, and a crashed build simply resumes from where
    the (mtime, size)-gated diff says work remains.

    `accounts_db_path` overrides where account display names are resolved
    from (default: the real `~/Library/Accounts/Accounts4.sqlite`) -- tests
    pass a synthetic fixture here to stay hermetic rather than depending on
    whatever's on the machine running them; production callers never need it.
    """
    from cobos_apple_mail_mcp.read.account_names import resolve_account_names

    start = time.monotonic()
    diff = inventory_diff(conn, mail_dir, exclude_mailboxes)
    account_names = resolve_account_names(accounts_db_path)
    _backfill_account_names(conn, account_names)

    if full:
        _drop_fts_triggers(conn)

    added_ok, added_failed = _index_entries(conn, diff.added, account_names)
    changed_ok, changed_failed = _index_entries(conn, diff.changed, account_names)

    if diff.deleted:
        conn.executemany("DELETE FROM emails WHERE emlx_path = ?", [(p,) for p in diff.deleted])
        conn.commit()

    moved_count = 0
    for old_path, entry in diff.moved:
        conn.execute(
            """
            UPDATE emails SET emlx_path = ?, emlx_mtime = ?, emlx_size = ?,
              account_uuid = ?, mailbox_url = ?, mailbox_name = ?, mailbox_role = ?
            WHERE emlx_path = ?
            """,
            (
                str(entry.path),
                entry.mtime,
                entry.size,
                entry.account_uuid,
                entry.mailbox_path,
                entry.mailbox_name,
                classify_mailbox_role(entry.mailbox_name),
                old_path,
            ),
        )
        moved_count += 1
    if diff.moved:
        conn.commit()

    if full:
        _rebuild_fts_index(conn)
        _recreate_fts_triggers(conn)
        if enable_trigram:
            _rebuild_trigram_index(conn)

    any_change = bool(diff.added or diff.changed or diff.deleted or diff.moved)
    if any_change:
        from cobos_apple_mail_mcp.read.threader import index_threads

        index_threads(conn)

    envelope_index = find_envelope_index(mail_dir)
    set_sync_state(conn, "last_build" if not full else "last_full_build", str(time.time()))
    set_sync_state(conn, "mail_dir", str(mail_dir))
    if envelope_index is not None:
        with contextlib.suppress(OSError):
            set_sync_state(conn, "envelope_mtime", str(envelope_index.stat().st_mtime))

    return IndexBuildResult(
        added=added_ok,
        changed=changed_ok,
        deleted=len(diff.deleted),
        moved=moved_count,
        failed=added_failed + changed_failed,
        duration_sec=time.monotonic() - start,
        full=full,
    )


def get_index_status(
    conn: sqlite3.Connection, mail_dir: Path | None, *, staleness_hours: float = 24.0
) -> IndexStatus:
    total = conn.execute("SELECT COUNT(*) AS n FROM emails").fetchone()["n"]
    dead_letter = conn.execute("SELECT COUNT(*) AS n FROM failed_index_jobs").fetchone()["n"]
    embed_total = total
    embed_done = conn.execute(
        "SELECT COUNT(*) AS n FROM emails WHERE embed_state = 2"
    ).fetchone()["n"]

    pending_added = pending_changed = pending_deleted = 0
    envelope_available = False
    if mail_dir is not None and mail_dir.is_dir():
        diff = inventory_diff(conn, mail_dir)
        pending_added = len(diff.added)
        pending_changed = len(diff.changed)
        pending_deleted = len(diff.deleted)
        envelope_available = find_envelope_index(mail_dir) is not None

    last_full_build_raw = get_sync_state(conn, "last_full_build")
    last_watch_tick_raw = get_sync_state(conn, "last_watch_tick")
    last_full_build = float(last_full_build_raw) if last_full_build_raw else None
    last_watch_tick = float(last_watch_tick_raw) if last_watch_tick_raw else None

    stale = False
    most_recent = max(filter(None, [last_full_build, last_watch_tick]), default=None)
    if most_recent is not None:
        stale = (time.time() - most_recent) > staleness_hours * 3600
    elif total > 0:
        stale = True

    if pending_added or pending_changed or pending_deleted:
        stale = True

    return IndexStatus(
        mail_dir=str(mail_dir) if mail_dir else None,
        envelope_index_available=envelope_available,
        total_indexed=total,
        pending_added=pending_added,
        pending_changed=pending_changed,
        pending_deleted=pending_deleted,
        dead_letter_count=dead_letter,
        embed_total=embed_total,
        embed_done=embed_done,
        last_full_build=last_full_build,
        last_watch_tick=last_watch_tick,
        stale=stale,
    )


def resolve_mail_dir(configured: str | None = None) -> Path | None:
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_dir() else None
    return find_mail_directory()
