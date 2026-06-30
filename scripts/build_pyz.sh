#!/usr/bin/env bash
# Builds dist/apple-mail-mcp.pyz (core) and dist/apple-mail-mcp-full.pyz
# (bundles [watch]+[semantic]) via shiv. See docs/wiki/Single-file-packaging.md
# for what makes this work (importlib.resources for packaged data, lazy
# optional-dependency imports).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v shiv >/dev/null 2>&1; then
  echo "shiv not found; install dev deps first: pip install -e '.[dev]'" >&2
  exit 1
fi

BUILD_PYTHON="$(command -v python3)"
BUILD_PYTHON_VERSION="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
BUILD_PYTHON_MINOR="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"

mkdir -p dist

echo "Building dist/apple-mail-mcp.pyz (core)..."
shiv -c apple-mail-mcp -o dist/apple-mail-mcp.pyz .

echo "Building dist/apple-mail-mcp-full.pyz (core + watch + semantic)..."
shiv -c apple-mail-mcp -o dist/apple-mail-mcp-full.pyz ".[watch,semantic]"

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
