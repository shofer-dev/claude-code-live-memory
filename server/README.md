# live-memory — MCP server

mcp-name: io.github.shofer-dev/live-memory

A cheap, persistent, **read-only** codebase memory for Claude Code. A separate large-context model runs
as a long-lived MCP server and *accumulates* knowledge of your repository across sessions; your agent
asks it via one tool, `ask_live_memory`, instead of re-reading files. It learns passively from the files
your agent reads/edits and stays current as the repo changes. Read-only and path-jailed; zero-config on a
Claude subscription (Haiku), or point it at any OpenAI-compatible / local model.

This package is the **server** behind the [live-memory Claude Code plugin](https://github.com/shofer-dev/claude-code-live-memory).

## Install & run

```bash
pip install shofer-live-memory
live-memory-server          # serves http://127.0.0.1:7711/mcp
```

Then point an MCP client at `http://127.0.0.1:7711/mcp` (the Claude Code plugin wires this for you).

- **Full docs / README:** https://github.com/shofer-dev/claude-code-live-memory
- **Design:** https://github.com/shofer-dev/claude-code-live-memory/blob/main/DESIGN.md
- **Privacy policy:** https://github.com/shofer-dev/claude-code-live-memory/blob/main/PRIVACY.md

Apache-2.0.
