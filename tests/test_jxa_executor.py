"""Real `osascript` subprocess tests for the never-hang mechanics
(CLAUDE.md invariant #4) — these don't touch Application("Mail") at all
(no Automation permission needed), so they run for real rather than mocked.
"""

from __future__ import annotations

import time

import pytest

from cobos_apple_mail_mcp.core.errors import JXAExecutionError, Timeout
from cobos_apple_mail_mcp.write.jxa_executor import run_osascript


def test_run_osascript_returns_stdout():
    out = run_osascript('JSON.stringify("hi")', timeout_sec=10)
    assert out == '"hi"'


def test_run_osascript_raises_on_script_error():
    with pytest.raises(JXAExecutionError) as exc_info:
        run_osascript('throw "boom"', timeout_sec=10)
    assert "boom" in str(exc_info.value)


def test_run_osascript_kills_on_timeout():
    start = time.monotonic()
    with pytest.raises(Timeout):
        # A tight busy-loop with no Application() call — bounded entirely by
        # our own subprocess timeout, not by Apple Events' own ~2min wait.
        run_osascript("while (true) {}", timeout_sec=1.5)
    elapsed = time.monotonic() - start
    # Must come back close to our timeout, not hang for anywhere near the
    # default ~2-minute Apple Events wait this is designed to bypass.
    assert elapsed < 10
