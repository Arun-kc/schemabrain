# Examples

Runnable examples and config snippets for wiring SchemaBrain into a real agent
loop or host. Each item is self-contained.

| Item | What it is | How to use it |
|---|---|---|
| [`anthropic_demo.py`](anthropic_demo.py) | A ~260-LOC, standalone Anthropic-SDK demo proving SchemaBrain plugs into **any** agent loop, not just Claude Desktop. It spawns `schemabrain serve` as a stdio subprocess, discovers the MCP tools, wires them into a Claude Haiku tool-use loop, and prints the transcript so you can see exactly which tools the agent chose. | Needs `ANTHROPIC_API_KEY` and an indexed store. `python examples/anthropic_demo.py` (see the module docstring for flags). |
| [`claude_desktop_config.example.json`](claude_desktop_config.example.json) | The MCP server entry to drop into Claude Desktop's `claude_desktop_config.json`. | `schemabrain init --host claude-desktop` writes this for you; the file is here as a reference for manual wiring. |
| [`cursor_mcp_config.example.json`](cursor_mcp_config.example.json) | The same entry shaped for Cursor's `~/.cursor/mcp.json`. | Reference for manual wiring; `schemabrain init --host cursor` writes it for you. |
| [`ecommerce/`](ecommerce/) | A complete SchemaBrain setup for the bundled e-commerce fixture — applyable entity / join / metric YAML packs. | See [`ecommerce/README.md`](ecommerce/README.md) for the apply walkthrough. |

For the full host-wiring guides see the [setup docs](../docs/setup/claude-desktop.md), and
for the end-to-end agent walkthrough see
[`docs/setup/manual.md`](../docs/setup/manual.md#3-wire-your-own-agent-anthropic-sdk).
