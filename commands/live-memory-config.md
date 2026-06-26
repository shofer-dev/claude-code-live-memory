---
description: View or change the Live Memory's model/provider (writes config.json + hot-reloads the server). Human-facing; not visible to the agent.
allowed-tools: ["Bash"]
argument-hint: "[show | set provider=openai base_url=https://api.deepseek.com model=deepseek-chat api_key=sk-…]"
---

Run the Live Memory config command with the user's arguments and show the output verbatim. Do not interpret — just display it.

!`python3 "${CLAUDE_PLUGIN_ROOT}/commands/config.py" $ARGUMENTS`
