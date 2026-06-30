"""osascript/JXA subprocess execution — the boundary every write-layer unit
test mocks (CLAUDE.md: "Write/JXA paths mocked at the jxa_executor boundary
so unit tests need no live Mail").

Every call is bounded by a hard timeout with process-group kill on expiry
(CLAUDE.md invariant #4 — never hang): `osascript` is spawned in its own
process group via `start_new_session=True` so a runaway Apple Event (a
modal dialog stealing focus, Mail being unresponsive) gets killed outright
rather than left to time out internally on Apple Events' own ~2-minute
default wait.

Arguments are passed as a single JSON blob via stdin, never interpolated
into the script text — there is no AppleScript/shell quoting to get wrong.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from importlib import resources
from typing import Any

from cobos_apple_mail_mcp.core.errors import JXAExecutionError, MailNotRunning, Timeout

DEFAULT_TIMEOUT_SEC = 20.0
_SCRIPTS_PACKAGE = "cobos_apple_mail_mcp.write.scripts"

_mail_core_js_cache: str | None = None


def _read_script(name: str) -> str:
    """Load a packaged JXA script via importlib.resources (works whether
    installed normally or run from a shiv .pyz — see CLAUDE.md packaging
    notes)."""
    return (resources.files(_SCRIPTS_PACKAGE) / name).read_text("utf-8")


def _mail_core_js() -> str:
    global _mail_core_js_cache
    if _mail_core_js_cache is None:
        _mail_core_js_cache = _read_script("mail_core.js")
    return _mail_core_js_cache


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    finally:
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2)


def run_osascript(program: str, *, timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> str:
    """Run a JavaScript-for-Automation program via `osascript -l JavaScript`,
    returning raw stdout. Raises Timeout / MailNotRunning / JXAExecutionError.
    """
    proc = subprocess.Popen(
        ["osascript", "-l", "JavaScript", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=program, timeout=timeout_sec)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(proc)
        raise Timeout(f"osascript call exceeded {timeout_sec}s and was killed") from exc

    if proc.returncode != 0:
        stderr_text = (stderr or "").strip()
        lowered = stderr_text.lower()
        if "isn't running" in lowered or ("not running" in lowered and "mail" in lowered):
            raise MailNotRunning("Mail.app is not running", stderr=stderr_text)
        # The stderr text (the actual JS/AppleScript error) goes in the
        # message itself, not just the `stderr` detail — callers that log
        # or surface str(exc)/exc.message (e.g. server.py's ToolError
        # wrapping) must not lose the one piece of text that explains what
        # actually went wrong.
        raise JXAExecutionError(
            f"osascript failed (exit {proc.returncode}): {stderr_text}", stderr=stderr_text
        )
    return stdout.strip()


def run_jxa(
    function_name: str, args: dict[str, Any], *, timeout_sec: float = DEFAULT_TIMEOUT_SEC
) -> Any:
    """Call one function from mail_core.js with JSON args, returning the
    JSON-decoded result. The whole call is one osascript invocation: the
    library source plus an IIFE that parses args, calls the function, and
    JSON-stringifies the result — so there is exactly one process spawn per
    logical operation.
    """
    args_literal = json.dumps(json.dumps(args))  # double-encoded: a JS string literal of JSON
    call_expr = f"{function_name}(JSON.parse({args_literal}))"
    program = _mail_core_js() + f"\nJSON.stringify((function(){{ return {call_expr}; }})());\n"
    raw = run_osascript(program, timeout_sec=timeout_sec)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JXAExecutionError(f"osascript returned non-JSON output: {raw[:500]!r}") from exc


def is_mail_running(*, timeout_sec: float = 5.0) -> bool:
    result = subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to (name of processes) contains "Mail"',
        ],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    return result.stdout.strip() == "true"


def ensure_mail_running(*, timeout_sec: float = 15.0) -> bool:
    """Launch Mail.app in the background if needed and wait (bounded) until
    it responds to a JXA ping. Returns False rather than hanging forever if
    Mail never becomes scriptable within timeout_sec.
    """
    if is_mail_running():
        return True
    subprocess.run(["open", "-g", "-a", "Mail"], check=False)
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            run_jxa("ping", {}, timeout_sec=3.0)
            return True
        except (Timeout, JXAExecutionError, MailNotRunning):
            time.sleep(0.5)
    return False


class JXAExecutor:
    """The interface write/*.py and core/resolver.py depend on, rather than
    calling subprocess directly. Tests substitute a fake implementing the
    same `call`/`ensure_running` methods — the documented mock boundary.
    """

    def __init__(self, *, timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> None:
        self.timeout_sec = timeout_sec

    def call(self, function_name: str, args: dict[str, Any]) -> Any:
        return run_jxa(function_name, args, timeout_sec=self.timeout_sec)

    def ensure_running(self, *, timeout_sec: float = 15.0) -> bool:
        return ensure_mail_running(timeout_sec=timeout_sec)
