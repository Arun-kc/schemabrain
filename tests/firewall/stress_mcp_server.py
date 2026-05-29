"""Stress harness — drive the MCP server under concurrent load.

Usage::

    uv run python tests/firewall/stress_mcp_server.py \\
        --store /tmp/sb-stress-store.db \\
        --concurrency 50 \\
        --duration 10 \\
        --tool mix

Tool options: `find_relevant_tables`, `describe_table`, `list_entities`,
or `mix` (round-robin across the three).

The harness writes nothing under `schemabrain/`. It fans calls out across
a `ThreadPoolExecutor`, records per-call wall-clock + status, then dumps
a structured summary that downstream tests (and the human reading
`STRESS_REPORT.md`) can scrape.

Bounded runtime: each scenario stops at `--duration` seconds or when a
60-second wall budget passes since the burst began — whichever fires
first. Configurable via `--max-wall`.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# Resolve the repo root so the harness runs from any CWD without
# requiring an editable install.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ruff: noqa: E402  --  sys.path mutation must precede schemabrain imports
from tests.firewall._harness import (
    HarnessHandle,
    build_test_server,
    call_tool_sync,
    count_audit_rows,
    verify_chain,
)

_TOOL_ARGS = {
    "find_relevant_tables": {"query": "orders", "limit": 5},
    "describe_table": {"qualified_name": "public.orders"},
    "list_entities": {},
}


def _pick_args(tool: str, idx: int) -> tuple[str, dict[str, Any]]:
    if tool == "mix":
        rotation = ("find_relevant_tables", "describe_table", "list_entities")
        picked = rotation[idx % len(rotation)]
        return picked, _TOOL_ARGS[picked]
    return tool, _TOOL_ARGS[tool]


def _one_call(handle: HarnessHandle, tool: str, idx: int) -> tuple[float, str, str]:
    name, args = _pick_args(tool, idx)
    t0 = time.perf_counter()
    try:
        env = call_tool_sync(handle.app, name, args)
        status = env.get("status", "?")
    except Exception as exc:
        status = f"raised:{type(exc).__name__}"
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return elapsed_ms, status, name


def run_burst(
    handle: HarnessHandle,
    *,
    concurrency: int,
    duration_s: float,
    tool: str,
    max_wall_s: float = 60.0,
) -> dict[str, Any]:
    """Saturate `concurrency` workers with calls for `duration_s` seconds.

    Stops on the earlier of (a) duration elapsed, (b) `max_wall_s`
    elapsed, (c) the dispatch queue draining naturally (which means the
    workers blocked and didn't recover — recorded as a hang).
    """
    stop = threading.Event()
    submitted = 0
    completed: list[tuple[float, str, str]] = []
    lock = threading.Lock()
    t_start = time.perf_counter()

    def worker(idx: int) -> tuple[float, str, str]:
        return _one_call(handle, tool, idx)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = []
        next_idx = 0

        def maybe_top_up() -> None:
            nonlocal next_idx, submitted
            # Keep a pipeline of ~4x concurrency in flight so workers
            # never go idle.
            target_inflight = concurrency * 4
            inflight = sum(1 for f in futures if not f.done())
            while inflight < target_inflight and not stop.is_set():
                futures.append(ex.submit(worker, next_idx))
                next_idx += 1
                submitted += 1
                inflight += 1

        maybe_top_up()
        # Poll-loop: pop completed futures, top up, check deadlines.
        deadline = t_start + duration_s
        hard_deadline = t_start + max_wall_s
        while futures and not stop.is_set():
            now = time.perf_counter()
            if now >= deadline or now >= hard_deadline:
                stop.set()
                break
            done_this_pass: list[int] = []
            for i, fut in enumerate(futures):
                if fut.done():
                    done_this_pass.append(i)
            # process completed in reverse so del-by-index is safe
            for i in reversed(done_this_pass):
                fut = futures.pop(i)
                try:
                    elapsed_ms, status, name = fut.result()
                except Exception as exc:
                    elapsed_ms, status, name = 0.0, f"crash:{type(exc).__name__}", "?"
                with lock:
                    completed.append((elapsed_ms, status, name))
            maybe_top_up()
            time.sleep(0.001)  # ~1ms poll loop
        # cancel pending futures so the executor shuts down promptly.
        for fut in futures:
            if not fut.done():
                fut.cancel()

    wall_s = time.perf_counter() - t_start
    latencies = sorted(ms for ms, _, _ in completed)
    statuses: dict[str, int] = {}
    per_tool: dict[str, int] = {}
    for _, status, name in completed:
        statuses[status] = statuses.get(status, 0) + 1
        per_tool[name] = per_tool.get(name, 0) + 1

    summary: dict[str, Any] = {
        "submitted": submitted,
        "completed": len(completed),
        "wall_s": round(wall_s, 3),
        "throughput_rps": round(len(completed) / wall_s, 1) if wall_s > 0 else 0.0,
        "p50_ms": round(statistics.median(latencies), 2) if latencies else None,
        "p95_ms": round(latencies[int(len(latencies) * 0.95)], 2) if latencies else None,
        "p99_ms": round(latencies[int(len(latencies) * 0.99)], 2) if latencies else None,
        "max_ms": round(latencies[-1], 2) if latencies else None,
        "status_counts": statuses,
        "per_tool_counts": per_tool,
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCP-server stress harness")
    parser.add_argument(
        "--store",
        default="/tmp/sb-stress-store.db",
        help="SQLite store path (must be pre-indexed)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=50,
        help="ThreadPoolExecutor worker count (default 50)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="Burst duration in seconds (default 10)",
    )
    parser.add_argument(
        "--max-wall",
        type=float,
        default=60.0,
        help="Hard wall-clock limit (default 60s)",
    )
    parser.add_argument(
        "--tool",
        default="mix",
        choices=("mix", "find_relevant_tables", "describe_table", "list_entities"),
    )
    parser.add_argument(
        "--source-id",
        default="src1",
        help="source_connection_id captured at index time",
    )
    args = parser.parse_args(argv)

    store_path = Path(args.store)
    if not store_path.exists():
        print(f"ERROR: store {store_path} not found. Index first.", file=sys.stderr)
        return 2

    audit_before = count_audit_rows(store_path)
    handle = build_test_server(
        store_path=store_path,
        source_connection_id=args.source_id,
    )

    summary = run_burst(
        handle,
        concurrency=args.concurrency,
        duration_s=args.duration,
        tool=args.tool,
        max_wall_s=args.max_wall,
    )

    audit_after = count_audit_rows(store_path)
    mismatches = verify_chain(store_path)
    summary["audit_rows_before"] = audit_before
    summary["audit_rows_after"] = audit_after
    summary["audit_rows_written"] = audit_after - audit_before
    summary["chain_intact"] = len(mismatches) == 0
    summary["chain_mismatches"] = [
        {"row_id": m.row_id, "expected": m.expected_hex, "actual": m.actual_hex}
        for m in mismatches[:5]  # cap dump size
    ]

    handle.audit_writer.close()
    handle.store.close()

    print(json.dumps(summary, indent=2))
    # Exit non-zero if the chain broke OR if no calls completed
    # (likely hang).
    if not summary["chain_intact"]:
        return 1
    if summary["completed"] == 0:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
