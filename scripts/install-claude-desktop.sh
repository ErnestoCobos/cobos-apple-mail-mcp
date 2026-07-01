#!/usr/bin/env bash
# cobos-apple-mail-mcp — Claude Desktop installer for macOS.
#
# A focused, Claude-Desktop-only installer: it installs the CLI (uv / pipx / pip,
# whichever is available), writes the config, optionally builds the local index,
# and registers the server in Claude Desktop's claude_desktop_config.json (which
# is the same config its Cowork workspace reads). Use scripts/install.sh instead
# if you also want it registered with the Claude Code CLI.
#
# The registration step edits a file you may already have other MCP servers in,
# so it is deliberately conservative: it parses the existing JSON, aborts without
# writing if that JSON is malformed (rather than clobbering it), backs the file
# up first, writes atomically, and only touches the single "apple-mail" key —
# every other server you configured is preserved.
#
# Author: Ernesto Cobos <ernesto@cobos.io>. GPL-3.0-or-later.

set -euo pipefail

# ---- options -------------------------------------------------------------
SERVER_NAME="apple-mail"
READ_ONLY=0
WITH_ATTACHMENTS=0
ASSUME_YES=0
DO_INDEX=1
DO_INSTALL=1

usage() {
  cat <<'EOF'
Usage: bash scripts/install-claude-desktop.sh [options]

  --name NAME          Server key to register (default: apple-mail)
  --read-only          Register the server with `serve --read-only`
  --with-attachments   Install the [attachments] extra (PDF/DOCX text search)
  --no-index           Skip building the local index
  --skip-install       Assume apple-mail-mcp is already installed; just register
  -y, --yes            Non-interactive: assume "yes" to every prompt
  -h, --help           Show this help

Installs the CLI, writes ~/.cobos-apple-mail-mcp/config.toml, offers to build the
local search index (skip with --no-index), and registers the server in Claude
Desktop's config. It never sends, moves, or deletes any mail.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --name) SERVER_NAME="${2:?--name needs a value}"; shift 2 ;;
    --read-only) READ_ONLY=1; shift ;;
    --with-attachments) WITH_ATTACHMENTS=1; shift ;;
    --no-index) DO_INDEX=0; shift ;;
    --skip-install) DO_INSTALL=0; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# ---- pretty output -------------------------------------------------------
if [ -t 1 ]; then
  B="$(printf '\033[1m')"; DIM="$(printf '\033[2m')"; R="$(printf '\033[0m')"
  GRN="$(printf '\033[32m')"; YLW="$(printf '\033[33m')"; RED="$(printf '\033[31m')"; BLU="$(printf '\033[34m')"
else
  B=""; DIM=""; R=""; GRN=""; YLW=""; RED=""; BLU=""
fi
step() { printf '\n%s==>%s %s%s%s\n' "$BLU" "$R" "$B" "$1" "$R"; }
info() { printf '    %s\n' "$1"; }
ok()   { printf '    %s✓%s %s\n' "$GRN" "$R" "$1"; }
warn() { printf '    %s!%s %s\n' "$YLW" "$R" "$1"; }
die()  { printf '\n%serror:%s %s\n' "$RED" "$R" "$1" >&2; exit 1; }

# confirm PROMPT DEFAULT(y|n) -> 0 for yes. Honors --yes; on a non-tty falls
# back to DEFAULT so the script is safe to run under automation.
confirm() {
  local prompt="$1" def="${2:-n}" reply hint
  [ "$ASSUME_YES" = 1 ] && return 0
  if [ ! -t 0 ]; then
    if [ "$def" = y ]; then return 0; else return 1; fi
  fi
  hint="[y/N]"; [ "$def" = y ] && hint="[Y/n]"
  printf '    %s %s ' "$prompt" "$hint"
  read -r reply || reply=""
  reply="${reply:-$def}"
  case "$reply" in [yY]*) return 0 ;; *) return 1 ;; esac
}

# ---- 0. preflight --------------------------------------------------------
step "Checking your system"
[ "$(uname -s)" = "Darwin" ] || die "this installer is macOS-only (Apple Mail and Claude Desktop live here)."
ok "macOS detected"

PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 not found — install the Xcode Command Line Tools (xcode-select --install) or Python 3.10+."
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)'; then
  die "python3 is older than 3.10 ($("$PY" -V 2>&1)). Install a newer Python 3.10+."
fi
ok "python3 is $("$PY" -V 2>&1 | awk '{print $2}')"

# ---- 1. install the package ---------------------------------------------
SPEC="cobos-apple-mail-mcp"
[ "$WITH_ATTACHMENTS" = 1 ] && SPEC="cobos-apple-mail-mcp[attachments]"

if [ "$DO_INSTALL" = 1 ]; then
  step "Installing $SPEC"
  if command -v uv >/dev/null 2>&1; then
    info "using uv"
    uv tool install --force "$SPEC"
  elif command -v pipx >/dev/null 2>&1; then
    info "using pipx"
    pipx install --force "$SPEC"
  elif command -v pip3 >/dev/null 2>&1; then
    info "using pip3 --user"
    pip3 install --user --upgrade "$SPEC"
  else
    die "need one of: uv, pipx, or pip3 on PATH. Install uv (https://docs.astral.sh/uv/) and re-run."
  fi
else
  step "Skipping install (--skip-install)"
fi

# Resolve the installed executable to an ABSOLUTE path. Claude Desktop is a GUI
# app and doesn't inherit a login shell's PATH, so we register the full path
# rather than the bare command name — the #1 "server won't start" gotcha.
BIN=""
for cand in \
  "apple-mail-mcp" \
  "$HOME/.local/bin/apple-mail-mcp" \
  "$("$PY" -m site --user-base 2>/dev/null)/bin/apple-mail-mcp"; do
  if command -v "$cand" >/dev/null 2>&1; then BIN="$(command -v "$cand")"; break; fi
  if [ -x "$cand" ]; then BIN="$cand"; break; fi
done
if [ -z "$BIN" ]; then
  if [ "$DO_INSTALL" = 1 ]; then
    die "couldn't find apple-mail-mcp after installing — check the install output above, or add its bin dir (usually ~/.local/bin) to PATH and re-run."
  else
    die "apple-mail-mcp isn't installed (or isn't on PATH). Re-run without --skip-install to install it, or add its bin dir (usually ~/.local/bin) to PATH."
  fi
fi
ok "using: $BIN"

# ---- 2. config -----------------------------------------------------------
step "Writing config"
# init is idempotent — it won't overwrite an existing config without --force.
"$BIN" init || warn "init reported an issue (a config may already exist) — continuing."
ok "config at ~/.cobos-apple-mail-mcp/config.toml"

# ---- 3. permissions ------------------------------------------------------
step "macOS permissions (required before writing / indexing)"
info "Full Disk Access — grant it to ${B}Claude Desktop${R} (it runs the server): lets it read"
info "  ~/Library/Mail to index and search."
info "Automation → Mail — lets the server script Mail.app for sends/moves/flags."
if [ "$DO_INDEX" = 1 ]; then
  info "Your terminal also needs Full Disk Access for the index build below."
fi
info "${DIM}System Settings → Privacy & Security → Full Disk Access / Automation${R}"
if confirm "Open the Full Disk Access settings pane now?" n; then
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" 2>/dev/null || \
    warn "couldn't open System Settings automatically — open it by hand."
fi

# ---- 4. build the index --------------------------------------------------
if [ "$DO_INDEX" = 1 ]; then
  if confirm "Build the local search index now? (needs Full Disk Access; can take a few minutes)" y; then
    step "Building the index"
    if "$BIN" index build; then
      ok "index built"
      "$BIN" index status || true
    else
      warn "index build failed — most often this is Full Disk Access not yet granted."
      warn "Grant it, then run:  $BIN index build"
    fi
  else
    info "skipped — build it later with:  $BIN index build"
  fi
fi

# ---- 5. register with Claude Desktop ------------------------------------
step "Registering with Claude Desktop"
cfg="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

# If neither the app nor an existing config is present, Claude Desktop probably
# isn't installed yet — let the user defer registration instead of writing a
# config into the void.
if [ ! -e "/Applications/Claude.app" ] && [ ! -e "$cfg" ]; then
  warn "Claude Desktop not detected (no /Applications/Claude.app, no existing config)."
  warn "Get it from https://claude.ai/download, then re-run this script."
  if ! confirm "Write the config anyway?" n; then
    step "Done"
    info "Skipped registration. The CLI is installed; re-run after installing Claude Desktop,"
    info "or register by hand: https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Install-per-client"
    printf '\n'
    exit 0
  fi
fi

serve_args='["serve"]'
[ "$READ_ONLY" = 1 ] && serve_args='["serve", "--read-only"]'

# The merge is done in Python: it preserves every other server, backs up the
# file, writes atomically, and refuses to write over malformed JSON instead of
# destroying it. Run inside `if` so `set -e` is suspended and a non-zero exit
# (malformed JSON) becomes a warning rather than killing the installer.
if BIN="$BIN" CFG="$cfg" NAME="$SERVER_NAME" ARGS="$serve_args" "$PY" - <<'PY'
import fcntl, json, os, shutil, sys, tempfile, time

cfg  = os.environ["CFG"]
name = os.environ["NAME"]
cmd  = os.environ["BIN"]
args = json.loads(os.environ["ARGS"])

os.makedirs(os.path.dirname(cfg), exist_ok=True)

# Serialize concurrent installer runs across the whole read-modify-write so two
# of them can't each write back a stale copy and drop the other's server. The
# lock lives in the per-user temp dir (deterministic name) to avoid leaving a
# stray .lock next to the user's config.
lock_path = os.path.join(tempfile.gettempdir(), "cobos-apple-mail-mcp.claude-config.lock")
lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX)
try:
    data = {}
    existed = os.path.exists(cfg)
    if existed:
        # utf-8-sig tolerates a BOM an editor may have prepended.
        with open(cfg, encoding="utf-8-sig") as fh:
            raw = fh.read().strip()
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                sys.stderr.write(
                    f"    refusing to touch {cfg}: it is not valid JSON ({exc}).\n"
                    "    Fix or remove it, then re-run — nothing was changed.\n"
                )
                sys.exit(3)
            if not isinstance(data, dict):
                sys.stderr.write("    refusing to touch config: top level is not a JSON object.\n")
                sys.exit(3)

    servers = data.get("mcpServers")
    if servers is None:
        servers = data["mcpServers"] = {}
    elif not isinstance(servers, dict):
        sys.stderr.write("    refusing to touch config: \"mcpServers\" is not a JSON object.\n")
        sys.exit(3)

    action = "updated" if name in servers else "added"

    # Back up only once we know we have valid JSON and are about to write.
    if existed:
        backup = cfg + ".bak." + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(cfg, backup)
        print(f"    backed up existing config -> {os.path.basename(backup)}")

    servers[name] = {"command": cmd, "args": args}

    # Atomic write: fsync a temp file in the target's dir, then os.replace() it
    # over the config in one step. A crash mid-write leaves the original intact
    # (recoverable from the backup regardless). Follow a symlinked config to its
    # real target so we swap contents, not the symlink itself.
    target = os.path.realpath(cfg)
    payload = json.dumps(data, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target) or ".", prefix=".claude-cfg-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tf:
            tf.write(payload)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    print(f"    {action} server \"{name}\" ({len(servers)} server(s) total in config)")
finally:
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
PY
then
  ok "registered in claude_desktop_config.json"
else
  warn "Claude Desktop registration didn't complete (see the message above)."
  warn "Register it by hand: https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Install-per-client"
fi

# ---- done ----------------------------------------------------------------
step "Done"
info "Next:"
info "  1. Grant ${B}Claude Desktop${R} Full Disk Access (System Settings → Privacy & Security)."
info "  2. Fully quit Claude Desktop (Cmd-Q) and reopen it — a window close isn't enough."
info "  3. Ask it:  \"What's in my inbox that still needs a reply?\""
[ "$READ_ONLY" = 1 ] && info "  (registered read-only: search/triage only, no sends or moves.)"
printf '\n'
