#!/usr/bin/env python3
"""Docs-sync drift guard (CLAUDE.md: "Docs-as-source-of-truth & maintenance
technique"). Wired into pre-commit and CI.

Two checks, both non-fatal by default (--strict makes drift a failure,
useful for CI; pre-commit stays informational to avoid blocking on false
positives from unrelated changes):

1. Structural: every Wiki page's `covers:` front-matter globs resolve to at
   least one real file — catches a page describing a module that was since
   renamed or deleted.
2. Drift: a file matching a page's `covers:` globs changed (staged, or in
   the given --base..HEAD range) without that page itself changing in the
   same diff — the "Definition of Done" CLAUDE.md describes.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML is required: pip install -e '.[dev]'", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parents[1]
WIKI_DIR = REPO_ROOT / "docs" / "wiki"


def _parse_front_matter(path: Path) -> dict:
    text = path.read_text("utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    return yaml.safe_load(text[3:end]) or {}


def _git_changed_files(base: str | None) -> set[str]:
    if base:
        cmd = ["git", "diff", "--name-only", f"{base}...HEAD"]
    else:
        cmd = ["git", "diff", "--cached", "--name-only"]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        return set()
    changed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if not base and not changed:
        # Nothing staged (e.g. running standalone, not as a pre-commit hook)
        # -- fall back to unstaged working-tree changes so the check is
        # still useful when run manually.
        result = subprocess.run(
            ["git", "diff", "--name-only"], cwd=REPO_ROOT, capture_output=True, text=True
        )
        changed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", default=None, help="git ref to diff against (CI on a PR); defaults to staged/working changes"
    )
    parser.add_argument("--strict", action="store_true", help="exit non-zero if drift is found")
    args = parser.parse_args()

    if not WIKI_DIR.is_dir():
        print(f"no such directory: {WIKI_DIR}", file=sys.stderr)
        return 2

    changed_files = _git_changed_files(args.base)
    warnings: list[str] = []

    for page in sorted(WIKI_DIR.glob("*.md")):
        meta = _parse_front_matter(page)
        covers = meta.get("covers") or []
        page_rel = str(page.relative_to(REPO_ROOT))

        resolved_any = False
        for pattern in covers:
            matches = list(REPO_ROOT.glob(pattern))
            if matches:
                resolved_any = True
            else:
                warnings.append(f"{page.name}: covers pattern matches no files: {pattern!r}")

        if covers and not resolved_any:
            continue  # already warned above for every pattern

        covered_changed = [
            f
            for f in changed_files
            if any(Path(f).match(pattern) or f.startswith(pattern.rstrip("*")) for pattern in covers)
        ]
        if covered_changed and page_rel not in changed_files:
            warnings.append(
                f"{page.name}: {', '.join(covered_changed)} changed but this page did not "
                "(Definition of Done: update the mapped Wiki page in the same change)"
            )

    if warnings:
        print("docs-sync warnings:")
        for w in warnings:
            print(f"  - {w}")
        if args.strict:
            return 1
    else:
        print("docs-sync: no drift detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
