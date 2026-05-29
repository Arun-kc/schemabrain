# Firewall Bypass Corpus

Each test file here reproduces ONE firewall bypass surfaced by the
internal firewall audit. Tests assert the SECURE behaviour, so they
FAIL on `main` today and will PASS once the corresponding remediation
lands — at which point they become regression guards.

## Running

```bash
# Full corpus (requires Docker container `sb-demo-pg` running locally
# on 127.0.0.1:5433 for the integration tests; pure-unit tests run
# without it).
uv run pytest tests/firewall/ -m firewall_bypass

# One attack
uv run pytest tests/firewall/test_fw_002_semantic_search_leak.py -v

# Skip the integration tests if Postgres isn't running
uv run pytest tests/firewall/ -m "firewall_bypass and not integration"

# Stress + perf gates (separately — these are the runnable harness, not
# bypass repros). Defaults: concurrency=50, duration=10s, mix tool.
uv run python tests/firewall/stress_mcp_server.py --help

# Standalone fuzz harness (522 cases across 12 MCP tools, ~30s)
uv run python tests/firewall/fuzz_mcp_tools.py
```

The `firewall_bypass` marker is registered in `pyproject.toml`. Tests
that hit a real Postgres ALSO carry `integration` so the default
`-m 'not integration'` exclusion (for contributors without Docker)
skips them automatically.

## Attack matrix

### Bypass repros — confirmed via live verification

| Test file | Finding ID | Confirmed | Severity | Notes |
|---|---|---|---|---|
| `test_fw_001_quoted_ident_ansi_escape.py` | FW-001 | CONFIRMED | HIGH | ANSI escape + 5 other control-char payloads accepted via double-quoted ident path |
| `test_fw_002_semantic_search_leak.py` | FW-002 / IF-1 | CONFIRMED | **CRIT** | `find_relevant_tables` leaks raw catastrophic-leak column names that `describe_table` redacts |
| `test_fw_003_fk_metadata_leak.py` | FW-003 / IF-2 | CONFIRMED | **CRIT** | Outgoing-FK `target_columns` leaks the redacted column name |
| `test_fw_005_aggregate_pii_leak.py` | FW-005 | CONFIRMED | HIGH | `MAX(email)` returns a raw row value through the metric envelope (live Postgres) |
| `test_fw_009_probe_oracle.py` | FW-009 | CONFIRMED | MED | `describe_entity.pii_categories` leaks the category family of a redacted column; second-order oracle: the redacted placeholder name itself encodes the category |
| `test_pii_001_i18n_classifier_bypass.py` | PII-001 | CONFIRMED | HIGH | 7 non-English column names (`correo_electronico`, `numero_seguridad_social`, `cpf`, `cnpj`, `kreditkartennummer`, `sozialversicherungsnummer`, `telefonnummer`) classify as `('public', frozenset())` |
| `test_pii_013_auth_secrets_classifier_bypass.py` | PII-013 | CONFIRMED | **CRIT** | 12 auth-secret column names (`pin`, `pin_code`, `recovery_code`, `ssh_key`, `kms_key`, `encryption_key`, `signing_key`, `aws_access_key`, …) all classify as `public` and bypass catastrophic-leak block |
| `test_sf_002_propagation_empty_fail_open.py` | SF-002 | CONFIRMED | HIGH | `propagate([])` is bit-identical to confirmed-clean data — empty tag input fail-opens |
| `test_sf_005_build_server_default_allow.py` | SF-005 | CONFIRMED | MED | `build_server(...)` API default `pii_block=frozenset()` = zero policy for library consumers |

### Fuzz-discovered findings — net-new

| Test file | ID | Severity | Notes |
|---|---|---|---|
| `test_fuzz_FZ_GM_007_limit_zero_bypasses_envelope.py` | FZ-GM-007 | HIGH | `get_metric(limit=0)` raises `FastMCPToolError` (transport-level) instead of returning the Charter v1.2 `ToolResponse` envelope with typed `ErrorKind`. Asymmetric vs `find_relevant_tables` / `suggest_joins` which gracefully refuse. Uniformity break in the envelope contract |

### Performance / DoS pins

| Test file | Finding ID | Verdict | Notes |
|---|---|---|---|
| `test_perf_audit_chain_race.py` | FW-008 | **NOT exploitable** (positive) | 123k cumulative concurrent writes, zero chain mismatches. `AuditWriter._lock` + commit-before-publish closes the race |
| `test_perf_statement_timeout_enforcement.py` | IF-3, FW-010 | IF-3 CONFIRMED; FW-010 NOT exploitable | No default timeout on `serve`; URL-smuggled `options=` correctly stripped; view-embedded `pg_sleep` correctly aborts under outer timeout |
| `test_perf_max_rows_truncation.py` | SF-003 | CONFIRMED | Truncation fires correctly but is SILENT — no envelope signal. Eager materialisation: 10M-row source query peaks at 10M dicts before clip |

### New DoS condition discovered during stress

NullPool + Postgres `max_connections=100` produces raw `FATAL: too many
clients already` at 120 concurrent workers (12% failure rate, no
graceful degradation, no MCP-layer rate limit, no `degraded`-envelope
fallback). Documented in `STRESS_REPORT.md` §"Connection-pool
exhaustion".

### Skipped / out-of-scope for this corpus

| Sub-test | Reason |
|---|---|
| `test_fw_003 :: test_describe_column_incoming_fk_does_not_leak_…` | Incoming-FK back-reference index didn't populate for the synthetic fixture; the leak surface exists for that direction too but a different fixture is needed. The outgoing-FK direction (same file) catches the bypass class |
| Tier 3 attacks (FW-006, FW-007, FW-010, FW-011, FW-012, FW-013) | Lower priority; deferred from this corpus |

## Per-test shape conventions

Every bypass repro follows the same skeleton so a remediator
can flip assertions mechanically:

1. **Module docstring** quotes the finding verbatim and spells
   out the SECURE behaviour the fix must produce.
2. **`pytestmark = [pytest.mark.firewall_bypass]`** (plus
   `pytest.mark.integration` when a real DB is required).
3. **Fixture-based setup**: tests do NOT share state. Each one seeds a
   fresh `SQLiteStore` from `conftest.fixture('store')`. Postgres tests
   get a fresh schema from `make_schema(name)`, dropped CASCADE on
   teardown.
4. **Assertion shape**: `assert <secure_behaviour>, "BYPASS: …"`. The
   assertion FAILS today; once the fix lands, the same line is the
   regression guard. The failure message names the bypass so
   `pytest --tb=line` is enough to triage.

## When the fix ships

For each bypass:

1. Land the remediation.
2. Run the corresponding test — it should now PASS without any test
   changes (that's the design — the test asserts the SECURE behaviour,
   not the current one).
3. If the assertion shape needs to change (e.g. a new
   `redacted=True` placeholder for FW-002 needs an explicit allowlist
   in the test), update the test BEFORE merging the fix so the
   regression guard stays pin-tight.
4. Move the test out of `tests/firewall/` and into the appropriate
   `tests/test_mcp_*.py` regression file. `tests/firewall/` is the
   stress-test scratch surface; long-lived regression coverage belongs
   alongside the tool it locks down.

## Source documents

- **Stress report**: `tests/firewall/STRESS_REPORT.md`
- **Threat model (baseline)**: `docs/threat-model.md`
- **Fuzz harness**: `tests/firewall/fuzz_mcp_tools.py`
- **Stress harness**: `tests/firewall/stress_mcp_server.py`
