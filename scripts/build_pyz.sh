#!/usr/bin/env bash
# Builds dist/apple-mail-mcp.pyz (core) and dist/apple-mail-mcp-full.pyz
# (bundles [watch]+[semantic]) via shiv. See docs/wiki/Single-file-packaging.md
# for what makes this work (importlib.resources for packaged data, lazy
# optional-dependency imports).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Prefer this project's own .venv over whatever "python3"/"shiv" happen to be
# first on $PATH -- the .pyz bundles compiled wheels (pydantic-core) tied to
# the exact Python minor version that builds it (see the warning below), and
# a system python3 can easily be a different minor version than the venv
# `pip install -e '.[dev]'` (or `uv sync --all-extras`) installed shiv into.
# This was a real footgun during development, not a hypothetical one.
if [ -x ".venv/bin/python3" ]; then
  BUILD_PYTHON=".venv/bin/python3"
  SHIV=".venv/bin/shiv"
else
  BUILD_PYTHON="$(command -v python3)"
  SHIV="shiv"
fi

if [ ! -x "$SHIV" ] && ! command -v "$SHIV" >/dev/null 2>&1; then
  echo "shiv not found; install dev deps first: uv sync --all-extras (or pip install -e '.[dev]')" >&2
  exit 1
fi

BUILD_PYTHON_VERSION="$("$BUILD_PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
BUILD_PYTHON_MINOR="$("$BUILD_PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"

mkdir -p dist

echo "Building dist/apple-mail-mcp.pyz (core) with $SHIV (Python $BUILD_PYTHON_VERSION)..."
"$SHIV" -c apple-mail-mcp -o dist/apple-mail-mcp.pyz .

echo "Building dist/apple-mail-mcp-full.pyz (core + watch + semantic)..."
"$SHIV" -c apple-mail-mcp -o dist/apple-mail-mcp-full.pyz ".[watch,semantic]"

echo "Done:"
ls -lh dist/*.pyz

cat <<EOF

IMPORTANT (verified during development, not a theoretical caveat): these
.pyz files bundle compiled wheels (pydantic-core) tied to this build's
Python ABI: $BUILD_PYTHON_VERSION ($BUILD_PYTHON).
Running a .pyz with a DIFFERENT Python minor version (e.g. built with 3.12,
run with 3.14) fails with "ModuleNotFoundError: No module named
'pydantic_core._pydantic_core'" -- this was reproduced and confirmed while
building this very script. Always run with a matching interpreter, e.g.:

  python$BUILD_PYTHON_MINOR dist/apple-mail-mcp.pyz serve

See docs/wiki/Single-file-packaging.md for the full explanation.
EOF
