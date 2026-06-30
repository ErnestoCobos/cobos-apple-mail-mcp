"""Typed errors for the server. Each maps to a stable MCP-facing error code.

See CLAUDE.md invariant #1 (identity/resolution) and #2 (safety layer) — these
exceptions are how those invariants surface to callers instead of being
silently papered over.
"""

from __future__ import annotations

from typing import Any


class AppleMailMCPError(Exception):
    """Base class for all typed errors. `code` is stable and machine-readable."""

    code = "internal_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.details}


class NotFound(AppleMailMCPError):
    """No message matched the given identity / locator."""

    code = "not_found"


class MultipleMatches(AppleMailMCPError):
    """More than one physical message matched a write-target resolution.

    Per invariant #1, callers (especially write tools) must never auto-pick
    among these; they re-call with `account`/`mailbox` to disambiguate.
    """

    code = "multiple_matches"

    def __init__(self, message: str, candidates: list[dict[str, Any]], **details: Any) -> None:
        super().__init__(message, candidates=candidates, **details)


class HandleSuperseded(AppleMailMCPError):
    """An `amid:` draft handle was superseded after the draft was sent."""

    code = "handle_superseded"

    def __init__(self, message: str, new_id: str, **details: Any) -> None:
        super().__init__(message, new_id=new_id, **details)


class ReadOnlyMode(AppleMailMCPError):
    """Server is running with --read-only; this tool performs a write."""

    code = "read_only_mode"


class BatchLimitExceeded(AppleMailMCPError):
    """Requested batch size exceeds the configured cap; never silently truncated."""

    code = "batch_limit_exceeded"

    def __init__(self, message: str, limit: int, requested: int, **details: Any) -> None:
        super().__init__(message, limit=limit, requested=requested, **details)


class ConfirmationRequired(AppleMailMCPError):
    """A destructive op (permanent delete / empty trash) needs confirm=true."""

    code = "confirmation_required"

    def __init__(self, message: str, preview: dict[str, Any], **details: Any) -> None:
        super().__init__(message, preview=preview, **details)


class ConfirmationStale(AppleMailMCPError):
    """confirm=true was given but resolution now yields a different id set."""

    code = "confirmation_stale"


class MailNotRunning(AppleMailMCPError):
    """A write op needs Mail.app running and it could not be launched in time."""

    code = "mail_not_running"


class AutomationPermissionDenied(AppleMailMCPError):
    """macOS denied Apple Events automation permission to Mail.app."""

    code = "automation_permission_denied"


class FullDiskAccessDenied(AppleMailMCPError):
    """macOS denied Full Disk Access; cannot read ~/Library/Mail."""

    code = "full_disk_access_denied"


class Timeout(AppleMailMCPError):
    """A bounded operation (subprocess, broad scan, ...) exceeded its timeout.

    Per invariant #4 (never hang), every external call is bounded; this is
    the typed result of that bound being hit, not an indefinite block.
    """

    code = "timeout"


class UndoFailed(AppleMailMCPError):
    """A journaled undo could not be applied (message moved/deleted again, etc.)."""

    code = "undo_failed"


class JXAExecutionError(AppleMailMCPError):
    """osascript/JXA process failed (non-timeout failure)."""

    code = "jxa_execution_error"

    def __init__(self, message: str, stderr: str = "", **details: Any) -> None:
        super().__init__(message, stderr=stderr, **details)
