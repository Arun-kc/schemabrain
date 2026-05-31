---
title: "Docker install"
description: "Published Docker image with the embedding model baked in — no first-run download, runs as a non-root uid."
---

# Docker install

The published Docker image bundles the runtime, all dependencies, and the local embedding model (`BAAI/bge-small-en-v1.5`, baked at build time so the first `serve` does not download). The image is published to GitHub Container Registry at `ghcr.io/arun-kc/schemabrain` for `linux/amd64` and `linux/arm64`.

The [activation wizard](/setup) is still the recommended path for most users — start there if you haven't yet. This page is for operators who prefer running SchemaBrain as a container.

<Note>
The published image is **headless**: it serves the MCP firewall (`index` / `serve`) and does not bundle the web dashboard. For the dashboard, install natively with `pip install schemabrain[ui]` and run `schemabrain dashboard` — it binds to `localhost` only and reads the same store.
</Note>

## 1. Quick index against a Postgres source

```bash
# Persist the SQLite store and the events log to ~/.schemabrain on the
# host. The container writes there as a non-root user (uid 1000); the
# directory must be writable by that uid. Either chown the directory
# to uid 1000 (`sudo chown 1000:1000 ~/.schemabrain`) or run the
# container with `--user $(id -u)` to match your own uid.
mkdir -p ~/.schemabrain

# Export the URL first so the password never lands in shell history or
# in argv. Same discipline as the non-Docker setup. `-e DATABASE_URL`
# without a value passes the env var through to the container.
export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/dbname"

# Run `schemabrain index` with the connection URL passed via env var.
# `-i` is not needed for index (no stdio); it IS needed for serve.
docker run --rm \
    -e DATABASE_URL \
    -e ANTHROPIC_API_KEY \
    -v ~/.schemabrain:/data \
    ghcr.io/arun-kc/schemabrain:latest \
    index --url-env DATABASE_URL --store-path /data/store.db
```

## 2. Claude Desktop config (containerised serve)

The `serve` subcommand needs `-i` (interactive stdio) so Claude Desktop can talk to it. Drop the following into your `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS path; the file lives next to other Claude config on Linux / Windows):

```json
{
  "mcpServers": {
    "schemabrain": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "DATABASE_URL",
        "-v", "/Users/YOU/.schemabrain:/data",
        "ghcr.io/arun-kc/schemabrain:latest",
        "serve", "--url-env", "DATABASE_URL", "--store-path", "/data/store.db"
      ],
      "env": {
        "DATABASE_URL": "postgresql+psycopg://user:pass@host:5432/dbname"
      }
    }
  }
}
```

Notes:

- Replace `/Users/YOU/.schemabrain` with your real home directory. Claude Desktop does not expand `~` inside config arguments.
- The `env` block carries the password into the container as a Docker-side environment variable. The URL never lands in argv on the host or in argv inside the container; `--url-env DATABASE_URL` reads it from the in-container env. This is the same discipline as the non-Docker setup.
- The mounted store volume (`-v .../.schemabrain:/data`) persists across container restarts. Without it, every `serve` rebuilds the in-memory retriever from zero and you lose `mine-queries` history.
- The image runs as uid 1000. If you indexed natively before switching to Docker, run `chown -R 1000:1000 ~/.schemabrain` once so the container can read the store.

## 3. Tags

| Tag | Meaning |
|---|---|
| `:latest` | Latest published release (PyPI publish + Docker push together) |
| `:0.4.0` | A specific version |
| `:0.4` | The latest patch in the 0.4 minor line |

For production-style pinning, use a specific patch (`:0.4.0`) rather than `:latest`.

## What's next

- [Wire your MCP host](/setup#4-wire-your-host) — per-host setup pages (~60 seconds each).
- [Manual flow](/setup/manual) — for explicit control over each step, plus logs, troubleshooting, and an SQL-validation ladder.
- [First 5 queries](/first-5-queries) — exercises each load-bearing firewall property.
