"""Filesystem path validation shared by every tool that writes user-supplied
output paths (export_emails, save_email_attachment, compose attachments).

Confines writes to the user's home directory and blocks a denylist of
sensitive subdirectories — the same pattern used by patrickfreyer's
save_email_attachment/export_emails, kept here as one reusable guard
instead of duplicated per tool.
"""

from __future__ import annotations

from pathlib import Path

from cobos_apple_mail_mcp.core.errors import AppleMailMCPError

_SENSITIVE_DIR_NAMES = {
    ".ssh",
    ".gnupg",
    ".aws",
    ".config",
    ".kube",
    ".docker",
    "Library",
}


class InvalidOutputPath(AppleMailMCPError):
    code = "invalid_output_path"


def validate_output_path(raw_path: str, *, must_be_under_home: bool = True) -> Path:
    """Resolve and validate a user-supplied output path. Raises
    InvalidOutputPath rather than allowing a write outside the home
    directory or into an obviously sensitive location.
    """
    path = Path(raw_path).expanduser().resolve()
    home = Path.home().resolve()

    if must_be_under_home:
        try:
            path.relative_to(home)
        except ValueError as exc:
            raise InvalidOutputPath(f"path must be under the home directory: {raw_path!r}") from exc

    relative_parts = path.relative_to(home).parts if must_be_under_home else path.parts
    if any(part in _SENSITIVE_DIR_NAMES for part in relative_parts):
        raise InvalidOutputPath(f"refusing to write into a sensitive directory: {raw_path!r}")

    return path


def validate_attachment_path(raw_path: str) -> Path:
    """Resolve and validate a file the user wants to ATTACH (read, not
    write) — readable from anywhere on disk, just not a sensitive
    directory, and must actually exist.
    """
    path = Path(raw_path).expanduser().resolve()
    if any(part in _SENSITIVE_DIR_NAMES for part in path.parts):
        raise InvalidOutputPath(f"refusing to attach from a sensitive directory: {raw_path!r}")
    if not path.is_file():
        raise InvalidOutputPath(f"attachment file not found: {raw_path!r}")
    return path
