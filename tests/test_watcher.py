from __future__ import annotations

import pytest

from cobos_apple_mail_mcp.config import load_config
from cobos_apple_mail_mcp.read.watcher import run_watch_loop
from cobos_apple_mail_mcp.storage.database import connect_index, get_sync_state
from tests.helpers import write_message


def test_polling_fallback_indexes_new_message(tmp_path, monkeypatch):
    import cobos_apple_mail_mcp.read.watcher as watcher_mod

    monkeypatch.setattr(watcher_mod, "has_watchfiles", lambda: False)
    monkeypatch.setattr(watcher_mod.time, "sleep", lambda _seconds: None)

    write_message(tmp_path, rowid=1, message_id="m1@example.com", subject="Hello")
    conn = connect_index(":memory:")
    cfg = load_config(environ={})

    run_watch_loop(conn, tmp_path, cfg, max_iterations=1)

    row = conn.execute("SELECT subject FROM emails WHERE message_id = 'm1@example.com'").fetchone()
    assert row is not None and row["subject"] == "Hello"
    assert get_sync_state(conn, "last_watch_tick") is not None


def test_watch_loop_runs_embedding_backfill_when_enabled(tmp_path, monkeypatch):
    pytest.importorskip("sqlite_vec")

    import cobos_apple_mail_mcp.read.watcher as watcher_mod
    from cobos_apple_mail_mcp.read.vector_search import EmbeddingBackend

    monkeypatch.setattr(watcher_mod, "has_watchfiles", lambda: False)
    monkeypatch.setattr(watcher_mod.time, "sleep", lambda _seconds: None)

    class FakeBackend(EmbeddingBackend):
        name = "fake"
        dimension = 4

        def is_available(self):
            return True

        def embed_one(self, text):
            return [0.0, 0.0, 0.0, 0.0]

    monkeypatch.setattr(
        "cobos_apple_mail_mcp.read.vector_search.get_backend", lambda cfg: FakeBackend()
    )

    write_message(tmp_path, rowid=1, message_id="m1@example.com", subject="Hello world")
    conn = connect_index(":memory:")
    cfg = load_config(
        cli_overrides={"embeddings": {"enabled": True, "backend": "apple_nl"}}, environ={}
    )

    run_watch_loop(conn, tmp_path, cfg, max_iterations=1)

    count = conn.execute("SELECT COUNT(*) AS n FROM emails_vec").fetchone()["n"]
    assert count == 1


def test_watch_tick_recovers_from_build_failure(tmp_path, monkeypatch):
    import cobos_apple_mail_mcp.read.watcher as watcher_mod

    monkeypatch.setattr(watcher_mod, "has_watchfiles", lambda: False)
    monkeypatch.setattr(watcher_mod.time, "sleep", lambda _seconds: None)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(watcher_mod, "build_index", boom)

    conn = connect_index(":memory:")
    cfg = load_config(environ={})

    # Must not raise — a tick failure is logged and the loop continues.
    run_watch_loop(conn, tmp_path, cfg, max_iterations=1)
