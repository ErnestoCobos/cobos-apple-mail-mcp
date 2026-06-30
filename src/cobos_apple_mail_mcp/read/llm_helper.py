"""Integration point for the optional on-device Foundation Models helper
(swift/foundation-models-summarizer/ — see that directory's README).

This module is NOT wired into any MCP tool yet — building the actual
`summarize_thread` tool on top of it is deferred ("build only if
requested"). What's here is the calling convention so that future tool can
be added without redesigning the boundary: a bounded subprocess call to a
separately-built Swift binary, JSON in, JSON out, graceful unavailability
when the binary hasn't been built or Foundation Models isn't supported on
this machine (macOS 15-/Apple Silicon-less systems) — the same "every
external call is a bounded subprocess with a typed degrade path" pattern
used by write/jxa_executor.py (CLAUDE.md invariant #4).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

DEFAULT_TIMEOUT_SEC = 30.0

_REPO_BUILT_BINARY = (
    Path(__file__).resolve().parents[3]
    / "swift"
    / "foundation-models-summarizer"
    / ".build"
    / "release"
    / "foundation-models-summarizer"
)


def find_binary() -> Path | None:
    """Locate a built helper binary: an `APPLE_MAIL_LLM_HELPER` env var
    (absolute path) takes precedence, then a binary on $PATH, then the
    in-repo release build location (for development)."""
    import os

    env_path = os.environ.get("APPLE_MAIL_LLM_HELPER")
    if env_path and Path(env_path).is_file():
        return Path(env_path)
    on_path = shutil.which("foundation-models-summarizer")
    if on_path:
        return Path(on_path)
    if _REPO_BUILT_BINARY.is_file():
        return _REPO_BUILT_BINARY
    return None


def is_available() -> bool:
    return find_binary() is not None


def summarize(text: str, *, timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> str:
    """Summarize `text` via the Foundation Models helper. Raises
    RuntimeError (never hangs past timeout_sec) if the helper isn't built,
    isn't supported on this machine, or the model declines/errors.
    """
    binary = find_binary()
    if binary is None:
        raise RuntimeError(
            "foundation-models-summarizer is not built; see swift/foundation-models-summarizer/"
            "README.md (this is an optional, not-built-by-default helper)"
        )

    request = json.dumps({"task": "summarize", "text": text})
    try:
        result = subprocess.run(
            [str(binary)],
            input=request,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"foundation-models-summarizer exceeded {timeout_sec}s") from exc

    try:
        response = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"foundation-models-summarizer returned non-JSON output: {result.stdout[:300]!r}"
        ) from exc

    if "error" in response:
        raise RuntimeError(f"foundation-models-summarizer: {response['error']}")
    return response["summary"]
