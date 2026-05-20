"""End-to-end demo: Claude Haiku answers a database question via Schema Brain MCP.

A standalone Anthropic-SDK demo that proves Schema Brain plugs into
any agent loop, not just Claude Desktop:

  1. Spawn `schemabrain serve` as a stdio subprocess.
  2. Use the official `mcp` SDK to discover tools and proxy calls.
  3. Wire those tools into a Claude Haiku conversation via the
     Anthropic SDK's standard `tools` parameter + tool-use loop.
  4. Print the conversation transcript so a reader can see exactly
     which tools the agent chose to call and why.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/db"
    python examples/anthropic_demo.py \\
        --url-env DATABASE_URL \\
        --store-path /path/to/schemabrain.db \\
        --question "Where do we store customer order totals?"

Cost: ~$0.005-0.02 per run on Haiku 4.5 depending on how many tool
turns the agent takes. Bounded by --max-turns (default 8). Aborts
cleanly with a clear message if the cap trips.

Why Haiku 4.5 not Sonnet? Demo. Haiku is 4x cheaper, plenty smart for
this task, and surfaces any subtle "the tool returned text the model
can't parse" issues that a bigger model might paper over.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_DEFAULT_QUESTION = (
    "Which tables in this database describe customer orders, and what "
    "are the most important columns on each?"
)
_DEFAULT_MAX_TURNS = 8
_HAIKU_MODEL = "claude-haiku-4-5"
# Per-message output cap. 1024 was too tight — verbose answers (multiple
# SQL skeletons, long explanations) hit the cap mid-response and stopped
# with `stop_reason="max_tokens"`. 4096 leaves headroom for typical demo
# answers without being excessive; Haiku 4.5 supports up to 8192.
_MAX_OUTPUT_TOKENS = 4096
_SYSTEM_PROMPT = (
    "You are a senior data engineer answering questions about an "
    "unfamiliar database. The user has indexed the database with "
    "Schema Brain. Use the provided MCP tools to discover relevant "
    "tables (`find_relevant_tables`) and inspect them in detail "
    "(`describe_table`) before answering. When you have enough to "
    "answer concisely, do so without further tool calls."
)


def _mcp_tool_to_anthropic_tool(tool: Any) -> dict[str, Any]:
    """Convert an MCP `Tool` into the Anthropic `tools=[...]` shape.

    The MCP SDK's tool already carries a JSON schema (`inputSchema`)
    that matches what Anthropic's tool-use API expects under
    `input_schema`, so the conversion is literally a rename.
    """
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.inputSchema,
    }


def _content_to_text(content: list[Any]) -> str:
    """Flatten an MCP `CallToolResult.content` block list into a single
    string the LLM can consume. MCP returns rich content (text, images,
    embedded resources); for v0 every Schema Brain tool returns text
    blocks only, so we just concatenate them.
    """
    parts: list[str] = []
    for block in content:
        if hasattr(block, "text"):
            parts.append(block.text)
        else:
            parts.append(json.dumps({"unsupported_block_type": str(type(block))}))
    return "\n".join(parts)


async def _run_agent_loop(
    *,
    url_env: str,
    store_path: str,
    question: str,
    max_turns: int,
    api_key: str,
) -> int:
    """Drive a tool-use conversation between Haiku and Schema Brain MCP.

    Returns the process exit code (0 on success, 1 on max-turns trip,
    2 on a tool failure).

    ``url_env`` is the name of the environment variable that holds the
    database URL — matches the rest of the CLI surface (`schemabrain
    serve --url-env DATABASE_URL`). Passing the URL via env keeps it
    out of `ps` output, shell history, and process argv.
    """
    server_params = StdioServerParameters(
        command="uv",
        args=[
            "run",
            "schemabrain",
            "serve",
            "--url-env",
            url_env,
            "--store-path",
            store_path,
        ],
        env={**os.environ},
    )

    client = anthropic.Anthropic(api_key=api_key)

    async with (
        stdio_client(server_params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools_resp = await session.list_tools()
        anthropic_tools = [_mcp_tool_to_anthropic_tool(t) for t in tools_resp.tools]
        print(f"[discovered tools] {[t['name'] for t in anthropic_tools]}\n")

        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        print(f"[user] {question}\n")

        for turn in range(1, max_turns + 1):
            resp = client.messages.create(
                model=_HAIKU_MODEL,
                max_tokens=_MAX_OUTPUT_TOKENS,
                system=_SYSTEM_PROMPT,
                tools=anthropic_tools,
                messages=messages,
            )

            # Append assistant turn to conversation.
            messages.append({"role": "assistant", "content": resp.content})

            # Print any text the assistant produced this turn.
            text_blocks = [b for b in resp.content if b.type == "text"]
            for tb in text_blocks:
                print(f"[assistant turn {turn}] {tb.text}\n")

            # Process tool_use blocks, if any.
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                # No more tool calls — Claude is done.
                print(f"[done] stopped at turn {turn}, stop_reason={resp.stop_reason!r}\n")
                return 0

            tool_results: list[dict[str, Any]] = []
            for tu in tool_uses:
                args_repr = json.dumps(tu.input)
                print(f"[tool call] {tu.name}({args_repr})")
                try:
                    result = await session.call_tool(tu.name, tu.input)
                    result_text = _content_to_text(result.content)
                    is_error = bool(result.isError)
                except Exception as e:
                    result_text = f"tool invocation failed: {type(e).__name__}: {e}"
                    is_error = True
                print(f"[tool result] is_error={is_error}, text={result_text[:240]}...\n")
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result_text,
                        "is_error": is_error,
                    }
                )

            # Append tool results so the next turn sees them.
            messages.append({"role": "user", "content": tool_results})

        print(f"[abort] hit max-turns cap of {max_turns}; agent did not converge", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="anthropic_demo",
        description="Drive Schema Brain MCP from Claude Haiku via the Anthropic SDK.",
    )
    parser.add_argument(
        "--url-env",
        dest="url_env",
        required=True,
        help="Name of the environment variable that holds the database URL "
        "(e.g. DATABASE_URL). The same variable Schema Brain's CLI reads "
        "via --url-env. Keeps the URL out of argv / ps output / shell "
        "history. Standard Postgres URL formats accepted; bare "
        "postgresql:// is silently normalised to postgresql+psycopg://.",
    )
    parser.add_argument(
        "--store-path",
        required=True,
        help="Path to the SchemaSqlite store created by `schemabrain index`.",
    )
    parser.add_argument(
        "--question",
        default=_DEFAULT_QUESTION,
        help="The question to ask the agent. Defaults to a sample about orders.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=_DEFAULT_MAX_TURNS,
        help=f"Hard cap on tool-use turns (default: {_DEFAULT_MAX_TURNS}).",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("error: ANTHROPIC_API_KEY not set in environment", file=sys.stderr)
        return 2

    # Validate the named env var resolves to a non-empty URL before we
    # spawn a subprocess that would fail with a less obvious error.
    db_url = os.environ.get(args.url_env)
    if not db_url:
        print(
            f"error: env var {args.url_env!r} (passed via --url-env) is not "
            f"set or is empty. Export it first, e.g. "
            f"`export {args.url_env}=postgresql://user:pass@host:5432/db`.",
            file=sys.stderr,
        )
        return 2

    return asyncio.run(
        _run_agent_loop(
            url_env=args.url_env,
            store_path=args.store_path,
            question=args.question,
            max_turns=args.max_turns,
            api_key=api_key,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
