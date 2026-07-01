---
covers: []
last_verified: 2026-06-30
---

# Install per client

Test the server in isolation first, with the official MCP Inspector:

```bash
npx @modelcontextprotocol/inspector apple-mail-mcp serve
```

This opens an interactive UI (and a CLI mode) to call tools/resources/prompts directly before
wiring up a client — useful for confirming permissions are granted and the index builds
correctly.

All examples below assume `apple-mail-mcp` is on `$PATH` (via pipx/uvx/pip). For the single-file
`.pyz`, replace the command/args with `"command": "python3.12", "args":
["/absolute/path/apple-mail-mcp.pyz", "serve", ...]` (substitute the Python minor version that
actually built the `.pyz` — see [Single-file packaging](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Single-file-packaging) for why a
generic `python3` can fail with a confusing `ModuleNotFoundError`). **Absolute paths are
required**, since the client launches the process from its own working directory, not yours.

## Claude Desktop

Config file: `~/Library/Application Support/Claude/claude_desktop_config.json` (Settings →
Developer → Edit Config opens it in your default editor; creates the file if missing).

```json
{
  "mcpServers": {
    "apple-mail": {
      "command": "apple-mail-mcp",
      "args": ["serve"]
    }
  }
}
```

Stdio transport only; no CLI to register — edit the JSON directly. **Restart Claude Desktop
completely** after editing (not just close the window) for changes to take effect.

## Claude Code / Cowork

```bash
claude mcp add apple-mail -- apple-mail-mcp serve
claude mcp list                  # verify
claude mcp get apple-mail        # details
```

Scopes: `local` (default, this project only), `user` (all your projects), `project` (shared with
your team, committed to `.mcp.json` in the repo root):

```bash
claude mcp add --scope project apple-mail -- apple-mail-mcp serve
```

`.mcp.json` schema (what the above writes):

```json
{
  "mcpServers": {
    "apple-mail": { "type": "stdio", "command": "apple-mail-mcp", "args": ["serve"] }
  }
}
```

First use of a project-scoped server prompts for approval. Changes take effect on the next
session start; inside an existing session, `/mcp` shows current server status.

## OpenAI Codex CLI

`~/.codex/config.toml`:

```toml
[mcp_servers.apple-mail]
command = "apple-mail-mcp"
args = ["serve"]
startup_timeout_sec = 10
tool_timeout_sec = 60
```

or via the CLI: `codex mcp add apple-mail -- apple-mail-mcp serve`. TOML, not JSON — note the
`command`/`args` are separate fields (same shape as the others, different syntax). Increase
`startup_timeout_sec` if index loading is slow on first run.

## Kimi CLI (Moonshot)

`~/.kimi/mcp.json`:

```json
{
  "mcpServers": {
    "apple-mail": { "type": "stdio", "command": "apple-mail-mcp", "args": ["serve"] }
  }
}
```

or: `kimi mcp add apple-mail -- apple-mail-mcp serve`, then verify with `kimi mcp test
apple-mail` (starts the server and lists its tools) or `kimi mcp list`. Inside the Kimi TUI,
`/mcp-config` manages servers interactively and `/mcp` shows connection status.

## Gotchas common to all of the above

- If `apple-mail-mcp` isn't resolvable on the client's `$PATH` (common with pipx installs not on
  a login shell's PATH, or any GUI-launched client), use an absolute path:
  `command: "/Users/you/.local/bin/apple-mail-mcp"` (find it with `which apple-mail-mcp`).
- Permission grants (Full Disk Access, Automation) are tied to the actual process that runs the
  server, which may be the client app itself if it launches subprocesses under its own bundle
  identity — see [Permissions & troubleshooting](https://github.com/ErnestoCobos/cobos-apple-mail-mcp/wiki/Permissions-and-troubleshooting) if writes or
  indexing fail after a config change that worked from a terminal.
- `--read-only` can be added to `args` (`["serve", "--read-only"]`) for any of the above if you
  only want read/search/triage access from that particular client.
