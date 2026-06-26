---
description: Show the Live Memory's current status for this workspace (context fill, Q&A retained, cost, queue, uptime). Human-facing; not visible to the agent.
allowed-tools: ["Bash"]
---

Run the Live Memory stats command and show the formatted output to the user verbatim. Do not interpret or summarize — just display it.

!`python3 "${CLAUDE_PLUGIN_ROOT}/commands/stats.py" "${CLAUDE_PROJECT_DIR:-$PWD}"`
