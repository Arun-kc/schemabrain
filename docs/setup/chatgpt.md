# SchemaBrain + ChatGPT

> **Short answer:** SchemaBrain v0.4 is **stdio-only.** ChatGPT's cloud MCP integration (Connectors) expects HTTPS / SSE transports. There is no first-class SchemaBrain → ChatGPT path that ships in this release. (Codex CLI users — see the note below; that path *does* work today.)

This page is honest about the transport gap and lays out the realistic options.

---

## Why there's no `--host chatgpt` flag

SchemaBrain runs as a local single-process MCP server over **stdio** ([`schemabrain/mcp/server.py`](../../schemabrain/mcp/server.py) — `app.run(transport="stdio")`). That's the right shape for desktop-class MCP clients (Claude Desktop, Claude Code, Cursor, Windsurf) where the host launches the server as a subprocess and pipes JSON-RPC frames.

ChatGPT's MCP integration runs in OpenAI's cloud. The client cannot exec a subprocess on your laptop; it needs an HTTPS endpoint it can reach. SchemaBrain doesn't yet expose one — adding a streaming HTTP transport with the right auth, rate-limiting, and PII-policy plumbing is on the v0.5+ roadmap, not v0.4.

We list this page because if you searched for "ChatGPT Postgres MCP" you deserve a real answer instead of a 404.

---

## Realistic options today

### Option 1 — Use a supported MCP client (recommended)

If you have any of these installed, you get the full SchemaBrain experience in minutes:

- **[Claude Desktop](claude-desktop.md)** — macOS / Windows; the most polished first-run
- **[Claude Code](claude-code.md)** — CLI on any OS where `claude` is on PATH
- **[Cursor](cursor.md)** — popular AI-first IDE
- **[Windsurf](windsurf.md)** — Codeium's AI-first IDE

All four wrap the same 12-tool MCP surface, the same PII firewall, the same audit chain. The only difference is the config file the wizard writes into.

### Option 2 — Codex CLI works today via `--host manual`

OpenAI's Codex CLI is a local stdio MCP client, much like Claude Code. You can register SchemaBrain in Codex's MCP config by hand:

```bash
schemabrain init --host manual
```

The wizard prompts for your Postgres URL (or offers the bundled demo container), then `--host manual` prints the resulting JSON entry to stdout instead of writing into a host config. Paste the entry into Codex's MCP config (consult Codex's docs for the exact path) and Codex's agent will speak stdio to SchemaBrain the same way Claude Code does.

### Option 3 — Run a stdio→HTTPS bridge yourself

Third-party bridges that wrap a stdio MCP server with an HTTPS endpoint exist in the ecosystem (e.g., community projects published as `mcp-remote`, `mcp-proxy`, etc.). We have not validated any specific bridge against SchemaBrain's PII / audit / recovery semantics, so this is a "you're on your own" path today. If you build one that works, file an issue — we will reference it here.

### Option 4 — Wait for native HTTPS transport

When SchemaBrain ships an HTTPS transport (planned for a future minor version), this page will document the OpenAI Connector / Custom GPT registration flow. Watch the [roadmap](../internal/v0.5.0_roadmap.md) or star the repo to track.

---

## What you'd get if it worked

For context — when SchemaBrain ships HTTPS transport, ChatGPT users will get exactly the same guarantees as every other supported client:

- **12 MCP tools, none of which can write.** Full list in [`/mechanism/read-only`](../mechanism/read-only.md).
- **PII-aware refusal at the `get_metric` boundary.** Details in [`/mechanism/pii-taxonomy`](../mechanism/pii-taxonomy.md).
- **Tamper-evident audit chain.** Details in [`/mechanism/audit-chain`](../mechanism/audit-chain.md).
- **Structured recovery envelopes.** Details in [`/mechanism/structured-recovery`](../mechanism/structured-recovery.md).

The firewall mechanism is transport-agnostic — only the wire format changes between stdio and HTTPS.

---

## Why we're not faking it

A "ChatGPT setup" page with screenshots and a copy-paste snippet that doesn't actually work is worse than no page at all. Two seconds with `grep transport schemabrain/mcp/server.py` and any reader can verify the gap is real. Honesty here protects the credibility of every other claim on the site — including the load-bearing safety claims in the [mechanism pages](../mechanism/).

If native ChatGPT support matters to you, open an issue on GitHub and we'll prioritize accordingly. Real demand bumps it up the roadmap; speculative work pushes other useful features back.

Full setup reference for supported clients: [`docs/setup.md`](../setup.md).
