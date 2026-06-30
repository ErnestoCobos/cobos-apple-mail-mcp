"""Ordered schema migrations for our derived `index.db`. Idempotent
(`IF NOT EXISTS` throughout) and gated by a `schema_version` table.

`index.db` is always disposable/rebuildable from disk (CLAUDE.md invariant
#6) — these migrations never touch Apple Mail's own `Envelope Index`.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

# Public: read/indexer.py drops+recreates this virtual table for a full
# rebuild.
#
# Deliberately NOT an "external content" table (no content=/content_rowid=):
# FTS5's auxiliary functions that need the original text — snippet(),
# highlight(), and the bare 'rebuild'/'delete-all' commands — fetch it from
# the content table by looking up columns with IDENTICAL NAMES to the FTS5
# columns. Ours don't (sender_name/sender_addr/body_plain vs. sender/body;
# "sender"/"recipients" are composed from multiple source columns), so those
# would silently fail ("no such column"). Storing FTS5's own copy of the
# searchable text avoids that whole class of bug, at the cost of some
# duplicated storage — an easy trade at personal-mailbox scale.
EMAILS_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
  subject, sender, recipients, body, attachments,
  tokenize='porter unicode61 remove_diacritics 2',
  prefix='2 3 4'
);
"""

# Public so read/indexer.py can re-issue these (idempotent CREATE TRIGGER IF
# NOT EXISTS) after a full-build pass that temporarily dropped them.
FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS emails_ai AFTER INSERT ON emails BEGIN
  INSERT INTO emails_fts(rowid, subject, sender, recipients, body, attachments)
  VALUES (new.id, new.subject,
          coalesce(new.sender_name,'')||' '||coalesce(new.sender_addr,''),
          new.recipients_all, new.body_plain, new.attachment_names);
END;

CREATE TRIGGER IF NOT EXISTS emails_ad AFTER DELETE ON emails BEGIN
  DELETE FROM emails_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS emails_au AFTER UPDATE ON emails BEGIN
  DELETE FROM emails_fts WHERE rowid = old.id;
  INSERT INTO emails_fts(rowid, subject, sender, recipients, body, attachments)
  VALUES (new.id, new.subject,
          coalesce(new.sender_name,'')||' '||coalesce(new.sender_addr,''),
          new.recipients_all, new.body_plain, new.attachment_names);
END;
"""

_MIGRATION_001_TEMPLATE = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS emails (
  id                INTEGER PRIMARY KEY,
  emlx_rowid        INTEGER,
  emlx_path         TEXT UNIQUE,
  emlx_mtime        REAL,
  emlx_size         INTEGER,

  account_uuid      TEXT NOT NULL,
  account_name      TEXT,
  mailbox_url       TEXT NOT NULL,
  mailbox_name      TEXT,
  mailbox_role      TEXT,

  message_id        TEXT,
  in_reply_to       TEXT,
  references_ids    TEXT,

  subject           TEXT,
  subject_norm      TEXT,
  sender_name       TEXT,
  sender_addr       TEXT,
  recipients_to     TEXT,
  recipients_cc     TEXT,
  recipients_all    TEXT,

  date_sent         INTEGER,
  date_received     INTEGER,

  snippet           TEXT,
  body_plain        TEXT,

  flag_read         INTEGER NOT NULL DEFAULT 0,
  flag_flagged      INTEGER NOT NULL DEFAULT 0,
  flag_answered     INTEGER NOT NULL DEFAULT 0,
  flag_draft        INTEGER NOT NULL DEFAULT 0,
  flag_bulk         INTEGER NOT NULL DEFAULT 0,
  attachment_count  INTEGER NOT NULL DEFAULT 0,
  attachment_names  TEXT,

  thread_id         INTEGER,
  thread_root_id    INTEGER,
  thread_position   INTEGER,

  indexed_at        REAL NOT NULL,
  embed_state       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_emails_msgid     ON emails(message_id);
CREATE INDEX IF NOT EXISTS idx_emails_inreplyto ON emails(in_reply_to);
CREATE INDEX IF NOT EXISTS idx_emails_thread    ON emails(thread_id, thread_position);
CREATE INDEX IF NOT EXISTS idx_emails_mailbox
  ON emails(account_uuid, mailbox_role, date_received DESC);
CREATE INDEX IF NOT EXISTS idx_emails_sender    ON emails(sender_addr, date_received DESC);
CREATE INDEX IF NOT EXISTS idx_emails_received  ON emails(date_received DESC);
CREATE INDEX IF NOT EXISTS idx_emails_embed     ON emails(embed_state) WHERE embed_state IN (0,1);

{{EMAILS_FTS_DDL}}

{{FTS_TRIGGERS_SQL}}

CREATE TABLE IF NOT EXISTS sync_state (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at REAL
);

CREATE TABLE IF NOT EXISTS failed_index_jobs (
  id INTEGER PRIMARY KEY,
  emlx_path TEXT NOT NULL,
  reason TEXT,
  traceback TEXT,
  attempts INTEGER NOT NULL DEFAULT 1,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_failed_path ON failed_index_jobs(emlx_path);

CREATE TABLE IF NOT EXISTS undo_journal (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  batch_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  canonical_id TEXT NOT NULL,
  account_name TEXT,
  from_mailbox TEXT,
  to_mailbox TEXT,
  prev_state TEXT,
  new_state TEXT,
  undone INTEGER NOT NULL DEFAULT 0,
  undo_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_undo_ts ON undo_journal(ts DESC);
CREATE INDEX IF NOT EXISTS idx_undo_batch ON undo_journal(batch_id);

-- account_name/mailbox_name are the JXA-addressable strings (what
-- Application("Mail") actually understands) — NOT the disk account_uuid/
-- mailbox_path our index uses internally. A disk UUID has no guaranteed
-- mapping to a JXA account name, so the write/resolution layer bridges the
-- two via email-address/name heuristics (see core/resolver.py) and caches
-- whatever JXA identity actually worked here, scoped to verify again before
-- every mutation (see CLAUDE.md invariant #1).
CREATE TABLE IF NOT EXISTS resolve_cache (
  canonical_id  TEXT NOT NULL,
  account_name  TEXT NOT NULL,
  mailbox_name  TEXT NOT NULL,
  mail_int_id   INTEGER,
  last_verified REAL NOT NULL,
  PRIMARY KEY (canonical_id, account_name, mailbox_name)
);
"""

_MIGRATION_001 = _MIGRATION_001_TEMPLATE.replace(
    "{{EMAILS_FTS_DDL}}", EMAILS_FTS_DDL
).replace("{{FTS_TRIGGERS_SQL}}", FTS_TRIGGERS_SQL)

MIGRATIONS: list[tuple[int, str]] = [
    (1, _MIGRATION_001),
]


def current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return 0
    return row[0] if row else 0


def migrate(conn: sqlite3.Connection) -> None:
    """Apply any pending migrations, in order, inside one transaction each."""
    version = current_version(conn)
    for target_version, sql in MIGRATIONS:
        if target_version <= version:
            continue
        conn.executescript(sql)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (target_version,))
        conn.commit()


def ensure_trigram_table(conn: sqlite3.Connection) -> None:
    """Optional substring-search companion index (config: index.enable_trigram).

    Self-contained (not external-content), like emails_fts — see that
    table's docstring for why.
    """
    conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS emails_trgm USING fts5(
          subject, sender, body,
          tokenize='trigram'
        );
        """
    )
    conn.commit()


def ensure_vec_table(conn: sqlite3.Connection, dimension: int) -> None:
    """Optional vector table for semantic/hybrid search. Created lazily only
    when the [semantic] extra is installed and embeddings.enabled is true.
    The dimension follows the configured embedding backend (Apple NL ~512 /
    MiniLM 384); changing backend requires a vec rebuild (see sync_state).
    """
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS emails_vec USING vec0("
        f"email_id INTEGER PRIMARY KEY, embedding FLOAT[{dimension}] distance_metric=cosine)"
    )
    conn.commit()
