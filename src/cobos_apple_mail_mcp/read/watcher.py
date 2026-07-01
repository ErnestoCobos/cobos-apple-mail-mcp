"""Incremental indexer (`--watch`): reacts to `.emlx` filesystem changes via
`watchfiles` (debounced, Rust/fsevents-backed) and reindexes via the same
`build_index(full=False)` path used by `index build` (CLAUDE.md knowledge
map: Indexing and watch). Off the request path and self-healing — a parse
race on a half-written file dead-letters that one path and clears itself on
the next successful tick (see read/indexer.py::_index_entries); the whole
loop degrades to periodic polling when the optional `watchfiles` dependency
isn't installed (CLAUDE.md packaging notes: lazy/guarded optional imports).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from cobos_apple_mail_mcp.config import Config
from cobos_apple_mail_mcp.read.indexer import build_index
from cobos_apple_mail_mcp.storage.database import set_sync_state

logger = logging.getLogger(__name__)

DEFAULT_DEBOUNCE_MS = 500
DEFAULT_POLL_INTERVAL_SEC = 30.0
OPTIMIZE_EVERY_N_CHANGES = 2000


def has_watchfiles() -> bool:
    try:
        import watchfiles  # noqa: F401
    except ImportError:
        return False
    return True


def _only_emlx_change(change, path: str) -> bool:  # noqa: ANN001 - watchfiles.Change
    return path.endswith(".emlx")


def run_watch_loop(
    conn: sqlite3.Connection,
    mail_dir: Path,
    cfg: Config,
    *,
    max_iterations: int | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Run the watch loop in the current thread. `max_iterations` and
    `stop_event` exist for tests/embedding; a CLI/server caller leaves both
    unset and runs until interrupted.
    """
    exclude = set(cfg.index.exclude_mailboxes)
    changes_since_optimize = 0

    embed_backend = None
    if cfg.embeddings.enabled:
        from cobos_apple_mail_mcp.read.vector_search import get_backend

        embed_backend = get_backend(cfg)
        if embed_backend is None:
            logger.warning(
                "embeddings.enabled=true but backend %r is unavailable; "
                "search will keep degrading to keyword mode",
                cfg.embeddings.backend,
            )

    def tick() -> None:
        nonlocal changes_since_optimize
        try:
            result = build_index(conn, mail_dir, exclude_mailboxes=exclude, full=False)
        except Exception:  # noqa: BLE001 - a tick failure must not kill the loop
            logger.exception("watch tick failed; will retry on the next event")
            return
        changes_since_optimize += result.added + result.changed + result.deleted + result.moved
        if changes_since_optimize >= OPTIMIZE_EVERY_N_CHANGES:
            conn.execute("INSERT INTO emails_fts(emails_fts) VALUES('optimize')")
            conn.commit()
            changes_since_optimize = 0
        set_sync_state(conn, "last_watch_tick", str(time.time()))

        if embed_backend is not None:
            # Low priority: a couple of small batches per tick, never
            # blocking the indexer from reacting to new mail.
            from cobos_apple_mail_mcp.read.vector_search import embed_backfill

            try:
                embed_backfill(conn, embed_backend, max_batches=2)
            except Exception:  # noqa: BLE001 - embedding failures must not kill the watch loop
                logger.exception("embedding backfill failed this tick; will retry next tick")

        if cfg.attachments.extract_text:
            # Same low-priority discipline as embeddings — PDF/DOCX extraction
            # is slow, so a couple of small batches per tick, never blocking.
            from cobos_apple_mail_mcp.read.attachment_extract import extract_backfill

            try:
                extract_backfill(conn, cfg, max_batches=2)
            except Exception:  # noqa: BLE001 - extraction failures must not kill the watch loop
                logger.exception("attachment extraction failed this tick; will retry next tick")

    iterations = 0

    if has_watchfiles():
        import watchfiles

        for _changes in watchfiles.watch(
            str(mail_dir),
            watch_filter=_only_emlx_change,
            debounce=DEFAULT_DEBOUNCE_MS,
            step=50,
            recursive=True,
            stop_event=stop_event,
        ):
            tick()
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
    else:
        logger.warning(
            "watchfiles is not installed; degrading to polling every %ss "
            "(pip install 'cobos-apple-mail-mcp[watch]' for real-time updates)",
            DEFAULT_POLL_INTERVAL_SEC,
        )
        while stop_event is None or not stop_event.is_set():
            tick()
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            time.sleep(DEFAULT_POLL_INTERVAL_SEC)


def start_background_watch(cfg: Config) -> threading.Thread:
    """Used by `serve --watch`: runs the watch loop on a daemon thread so it
    never blocks the MCP server's stdio event loop.
    """
    from cobos_apple_mail_mcp.read.indexer import resolve_mail_dir
    from cobos_apple_mail_mcp.storage.database import connect_index

    mail_dir = resolve_mail_dir()
    if mail_dir is None:
        logger.warning("could not locate ~/Library/Mail/V*; --watch is disabled")
        return threading.Thread(target=lambda: None)

    watch_conn = connect_index(cfg.index.path)
    thread = threading.Thread(
        target=run_watch_loop,
        args=(watch_conn, mail_dir, cfg),
        name="apple-mail-mcp-watch",
        daemon=True,
    )
    thread.start()
    return thread
