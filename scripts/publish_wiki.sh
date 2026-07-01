#!/usr/bin/env bash
# Syncs docs/wiki/*.md (the version-controlled source of truth) to the
# project's GitHub wiki repo. See docs/wiki/Home.md and CLAUDE.md's
# "Docs-as-source-of-truth & maintenance" section.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

REPO_URL="${WIKI_REPO_URL:-}"
if [ -z "$REPO_URL" ]; then
  ORIGIN_URL="$(git config --get remote.origin.url || true)"
  if [ -z "$ORIGIN_URL" ]; then
    echo "Could not determine the repo URL (no git remote 'origin' and WIKI_REPO_URL unset)." >&2
    exit 1
  fi
  # Derive the .wiki.git URL from the main repo's remote, for both
  # git@github.com:owner/repo.git and https://github.com/owner/repo.git forms.
  REPO_URL="${ORIGIN_URL%.git}.wiki.git"
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "Cloning $REPO_URL ..."
git clone --depth 1 "$REPO_URL" "$WORKDIR"

echo "Syncing docs/wiki/ -> wiki repo ..."
# Mirror docs/wiki/*.md into the wiki checkout, removing pages that no
# longer exist in docs/wiki/ but never touching the wiki repo's own .git.
rsync -a --delete --exclude='.git' docs/wiki/ "$WORKDIR/"

# GitHub's wiki renderer (unlike GitHub Pages/Jekyll) does NOT strip YAML
# frontmatter -- verified live: the `covers`/`last_verified` block rendered
# as garbled literal text (and a bogus heading) at the top of every page.
# That block only exists for scripts/check_docs_sync.py's drift guard, so
# docs/wiki/ (the source of truth) keeps it, but the actually-published
# pages should not show it.
python3 - "$WORKDIR" <<'PYEOF'
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
frontmatter = re.compile(r"\A---\n.*?\n---\n\n?", re.DOTALL)
for f in root.glob("*.md"):
    text = f.read_text()
    stripped = frontmatter.sub("", text, count=1)
    if stripped != text:
        f.write_text(stripped)
PYEOF

cd "$WORKDIR"
git add -A
if git diff --cached --quiet; then
  echo "No changes to publish."
  exit 0
fi

git -c user.name="Ernesto Cobos" -c user.email="ernesto@cobos.io" \
  commit -m "Sync wiki from docs/wiki/"
git push origin HEAD

echo "Wiki published."
