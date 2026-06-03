"use client";

import { useMemo, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Button, Icon, useToast } from "@/components/kit";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import {
  buildPreviewQuery,
  columnIsFloor,
  countStagedChanges,
  initialOverridesFromPerColumn,
  isColumnOverrideChanged,
  isMarkedSafe,
  type StagedOverrides,
  toggleBlockCategory,
  toggleMarkSafe,
} from "@/lib/policy";
import { useSourceId } from "@/lib/useSourceId";
import {
  isCatastrophic,
  type PIICategory,
  PII_CATEGORIES,
  type PolicyCategoryRollup,
  type PolicyColumnEntry,
  type PolicyDriftState,
  type PolicyPreviewResponse,
  type PolicyResponse,
} from "@/lib/types";
import styles from "./policy-editor.module.css";

/**
 * PolicyEditor — the editable PII enforcement surface (/policy).
 *
 * Two honest levers reconciled from the handoff's per-column grid
 * (ADR 0007): a category block panel (writes `block:`) and a per-column
 * override list (writes `column_overrides:` via "mark safe"). The
 * catastrophic floor is locked in both. The YAML panel + diff are
 * rendered server-side through GET /api/pii/policy/preview, and Apply
 * copies that canonical YAML + reveals the CLI command rather than
 * writing (ADR 0006) — the sidecar stays read-only.
 */
export function PolicyEditor({ sourceId: sourceIdProp }: { sourceId?: string }) {
  const { sourceId: resolvedSourceId, status: sourceStatus } = useSourceId();
  const sourceId = sourceIdProp ?? resolvedSourceId ?? undefined;

  const policyQuery = useQuery({
    queryKey: ["pii-policy", sourceId],
    queryFn: () => api.piiPolicy(sourceId),
    enabled: sourceIdProp !== undefined || sourceStatus !== "loading",
  });

  if (policyQuery.isPending) {
    return (
      <div className={styles.page}>
        <PageHead />
        <p className={styles.skeleton}>loading policy…</p>
      </div>
    );
  }
  if (policyQuery.isError) {
    return <PolicyEditorError message={policyQuery.error.message} />;
  }

  // Re-key on the baseline so a real policy change (operator ran
  // `policy apply` + restarted serve) remounts with a fresh staged
  // baseline, while a background refetch of identical content does not
  // clobber in-progress staging.
  return (
    <PolicyEditorContent key={baselineSignature(policyQuery.data)} data={policyQuery.data} />
  );
}

function baselineSignature(data: PolicyResponse): string {
  const { block, override } = buildPreviewQuery(
    new Set(data.active_block),
    initialOverridesFromPerColumn(data.per_column),
  );
  return JSON.stringify({ block, override });
}

function prettyCategory(category: string): string {
  return category.replace(/_/g, " ");
}

function PolicyEditorContent({ data }: { data: PolicyResponse }) {
  const { copyToClipboard } = useToast();

  const initialBlock = useMemo(() => new Set(data.active_block), [data.active_block]);
  const initialOverrides = useMemo(
    () => initialOverridesFromPerColumn(data.per_column),
    [data.per_column],
  );

  const [stagedBlock, setStagedBlock] = useState<ReadonlySet<PIICategory>>(initialBlock);
  const [stagedOverrides, setStagedOverrides] = useState<StagedOverrides>(initialOverrides);
  const [showCommand, setShowCommand] = useState(false);

  const previewParams = useMemo(
    () => buildPreviewQuery(stagedBlock, stagedOverrides),
    [stagedBlock, stagedOverrides],
  );
  const previewQuery = useQuery({
    queryKey: ["pii-policy-preview", previewParams.block, previewParams.override],
    queryFn: () => api.piiPolicyPreview(previewParams),
    placeholderData: keepPreviousData,
  });
  const preview = previewQuery.data;

  const stagedCount = countStagedChanges(
    initialBlock,
    stagedBlock,
    initialOverrides,
    stagedOverrides,
  );

  const handleToggleBlock = (category: PIICategory) => {
    setShowCommand(false);
    setStagedBlock((prev) => toggleBlockCategory(prev, category));
  };
  const handleMarkSafe = (qualifiedColumn: string, safe: boolean) => {
    setShowCommand(false);
    setStagedOverrides((prev) => toggleMarkSafe(prev, qualifiedColumn, safe));
  };
  const handleDiscard = () => {
    setShowCommand(false);
    setStagedBlock(initialBlock);
    setStagedOverrides(initialOverrides);
  };
  const applyCommand = `schemabrain policy apply ${data.policy_path}`;
  const handleApply = () => {
    if (!preview?.changed) return;
    copyToClipboard(preview.staged_yaml, "policy YAML");
    setShowCommand(true);
  };

  return (
    <div className={styles.page}>
      <PageHead />
      <div className={styles.body}>
        {data.yaml_parse_error && <ParseErrorBanner error={data.yaml_parse_error} />}
        {data.policy_drift.detected && (
          <DriftBanner
            drift={data.policy_drift}
            onCopyRestart={() => copyToClipboard("schemabrain serve", "restart command")}
          />
        )}

        <div className={styles.grid}>
          <div className={styles.col}>
            <CategoryBlockPanel
              stagedBlock={stagedBlock}
              rollup={data.category_rollup}
              onToggle={handleToggleBlock}
            />
            <OverrideListPanel
              perColumn={data.per_column}
              stagedOverrides={stagedOverrides}
              initialOverrides={initialOverrides}
              onMarkSafe={handleMarkSafe}
            />
          </div>

          <div className={styles.col}>
            <YamlPanel preview={preview} />
            <DiffPanel
              preview={preview}
              stagedCount={stagedCount}
              onApply={handleApply}
              onDiscard={handleDiscard}
              showCommand={showCommand}
              applyCommand={applyCommand}
              policyPath={data.policy_path}
              onCopyCommand={() => copyToClipboard(applyCommand, "apply command")}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

function PageHead() {
  return (
    <header className={styles.pageHead}>
      <h1>Policy</h1>
      <p>
        The enforcement policy SchemaBrain compiles every query against. Block a category to
        refuse it everywhere, or mark a column safe to clear a false positive — the catastrophic
        floor (credential, payment card, government ID) is always on and can&apos;t be unlocked.
        Applying copies the canonical{" "}
        <code className={styles.inlineCode}>schemabrain.yaml</code> for you to commit and run.
      </p>
    </header>
  );
}

/* ─────────── category block panel (writes `block:`) ─────────── */

function CategoryBlockPanel({
  stagedBlock,
  rollup,
  onToggle,
}: {
  stagedBlock: ReadonlySet<PIICategory>;
  rollup: readonly PolicyCategoryRollup[];
  onToggle: (category: PIICategory) => void;
}) {
  const countByCategory = useMemo(() => {
    const map = new Map<string, number>();
    for (const row of rollup) map.set(row.category, row.column_count);
    return map;
  }, [rollup]);
  const blockedCount = PII_CATEGORIES.filter(
    (category) => isCatastrophic(category) || stagedBlock.has(category),
  ).length;

  return (
    <section className={styles.pane} aria-label="category block set">
      <div className={styles.paneHead}>
        <Icon name="sliders-horizontal" size={14} /> Block categories
        <span className={styles.paneCount}>{blockedCount} blocked</span>
      </div>
      {PII_CATEGORIES.map((category) => {
        const floor = isCatastrophic(category);
        const on = stagedBlock.has(category);
        const count = countByCategory.get(category) ?? 0;
        return (
          <div
            key={category}
            className={cn(styles.catRow, floor ? styles.floor : on && styles.on)}
          >
            <div className={styles.catId}>
              <div className={styles.catName}>
                {floor && (
                  <Icon name="lock" size={12} className={styles.floorLock} label="locked floor" />
                )}
                {prettyCategory(category)}
              </div>
              <div className={styles.catMeta}>
                {count === 0
                  ? "no tagged columns"
                  : `${count} ${count === 1 ? "column" : "columns"}`}
              </div>
            </div>
            {floor ? (
              <span className={styles.floorTag}>
                <Icon name="lock" size={11} /> floor · locked
              </span>
            ) : (
              <button
                type="button"
                className={cn(styles.toggle, on && styles.on)}
                aria-pressed={on}
                aria-label={`${on ? "unblock" : "block"} ${prettyCategory(category)}`}
                onClick={() => onToggle(category)}
              >
                {on ? "blocked" : "block"}
              </button>
            )}
          </div>
        );
      })}
    </section>
  );
}

/* ─────────── per-column override list (writes `column_overrides:`) ─────────── */

function OverrideListPanel({
  perColumn,
  stagedOverrides,
  initialOverrides,
  onMarkSafe,
}: {
  perColumn: readonly PolicyColumnEntry[];
  stagedOverrides: StagedOverrides;
  initialOverrides: StagedOverrides;
  onMarkSafe: (qualifiedColumn: string, safe: boolean) => void;
}) {
  const editable = useMemo(
    () => perColumn.filter((row) => !columnIsFloor(row.categories)),
    [perColumn],
  );
  const floorCount = perColumn.length - editable.length;

  return (
    <section className={styles.pane} aria-label="column overrides">
      <div className={styles.paneHead}>
        <Icon name="shield" size={14} /> Column overrides
        <span className={styles.paneCount}>{editable.length} editable</span>
      </div>
      {editable.length === 0 ? (
        <p className={styles.empty}>
          {perColumn.length === 0
            ? "no columns have been tagged on this source yet"
            : "every tagged column is on the catastrophic floor — nothing to override"}
        </p>
      ) : (
        editable.map((row) => (
          <OverrideRow
            key={row.qualified_column}
            row={row}
            marked={isMarkedSafe(row.qualified_column, stagedOverrides)}
            pending={isColumnOverrideChanged(
              initialOverrides,
              stagedOverrides,
              row.qualified_column,
            )}
            onMarkSafe={onMarkSafe}
          />
        ))
      )}
      {floorCount > 0 && (
        <p className={styles.empty}>
          {floorCount} catastrophic-floor{" "}
          {floorCount === 1 ? "column is" : "columns are"} always protected — see Block categories.
        </p>
      )}
    </section>
  );
}

function OverrideRow({
  row,
  marked,
  pending,
  onMarkSafe,
}: {
  row: PolicyColumnEntry;
  marked: boolean;
  pending: boolean;
  onMarkSafe: (qualifiedColumn: string, safe: boolean) => void;
}) {
  return (
    <div className={cn(styles.rule, pending && styles.pending)}>
      <div className={styles.ruleId}>
        <div className={styles.ruleTable}>{row.qualified_table}</div>
        <div className={styles.ruleName}>{row.column_name}</div>
        <div className={styles.ruleCats}>
          {row.categories.length === 0
            ? "asserted safe · no categories"
            : row.categories.map(prettyCategory).join(" · ")}
        </div>
      </div>
      <div className={styles.seg} role="group" aria-label={`override ${row.qualified_column}`}>
        <button
          type="button"
          className={cn(!marked && styles.onUse)}
          aria-pressed={!marked}
          onClick={() => onMarkSafe(row.qualified_column, false)}
        >
          use classifier
        </button>
        <button
          type="button"
          className={cn(marked && styles.onSafe)}
          aria-pressed={marked}
          onClick={() => onMarkSafe(row.qualified_column, true)}
        >
          mark safe
        </button>
      </div>
    </div>
  );
}

/* ─────────── server-rendered YAML panel ─────────── */

function YamlPanel({ preview }: { preview?: PolicyPreviewResponse }) {
  return (
    <section className={styles.pane} aria-label="generated policy yaml">
      <div className={styles.paneHead}>
        <Icon name="file-code" size={14} /> schemabrain.yaml
      </div>
      <pre className={styles.yaml}>{preview ? preview.staged_yaml : "rendering…"}</pre>
      {preview?.current_parse_error && (
        <p className={styles.yamlNote}>
          <span className={styles.alarm}>current pii_policy.yaml doesn&apos;t parse</span> ·
          diffing against the catastrophic-floor default. {preview.current_parse_error}
        </p>
      )}
    </section>
  );
}

/* ─────────── staged diff + read-only apply (ADR 0006) ─────────── */

function DiffPanel({
  preview,
  stagedCount,
  onApply,
  onDiscard,
  showCommand,
  applyCommand,
  policyPath,
  onCopyCommand,
}: {
  preview?: PolicyPreviewResponse;
  stagedCount: number;
  onApply: () => void;
  onDiscard: () => void;
  showCommand: boolean;
  applyCommand: string;
  policyPath: string;
  onCopyCommand: () => void;
}) {
  const changed = preview?.changed ?? false;
  return (
    <section className={styles.pane} aria-label="staged changes">
      <div className={styles.paneHead}>
        <Icon name="git-pull-request" size={14} /> Pending changes
        <span className={styles.paneCount}>{stagedCount} staged</span>
      </div>
      <div className={styles.diff}>
        {!changed || !preview ? (
          <div className={styles.diffEmpty}>
            No staged changes. Block a category or mark a column safe to preview a diff.
          </div>
        ) : (
          <>
            {preview.diff_lines.map((line, index) => (
              <div
                // diff lines are positional + immutable for a given preview;
                // index is a stable key here.
                key={index}
                className={cn(styles.diffRow, styles[line.kind])}
              >
                <span className={styles.diffSign}>
                  {line.kind === "add" ? "+" : line.kind === "remove" ? "−" : ""}
                </span>
                {line.text || " "}
              </div>
            ))}
            <div className={styles.diffFoot}>
              <Button variant="primary" icon="check" onClick={onApply}>
                Apply policy
              </Button>
              <Button onClick={onDiscard}>Discard</Button>
            </div>
          </>
        )}
        {showCommand && changed && (
          <div className={styles.command}>
            <span className={styles.commandLabel}>Run to apply</span>
            <div className={styles.commandRow}>
              <code className={styles.commandCode}>{applyCommand}</code>
              <Button icon="copy" onClick={onCopyCommand} aria-label="copy apply command">
                copy
              </Button>
            </div>
            <p className={styles.commandHint}>
              The YAML is on your clipboard — paste it into{" "}
              <code className={styles.inlineCode}>{policyPath}</code>, run the command, then restart{" "}
              <code className={styles.inlineCode}>schemabrain serve</code> for the firewall to
              enforce it.
            </p>
          </div>
        )}
      </div>
    </section>
  );
}

/* ─────────── banners + error ─────────── */

function DriftBanner({
  drift,
  onCopyRestart,
}: {
  drift: PolicyDriftState;
  onCopyRestart: () => void;
}) {
  return (
    <div className={styles.banner} role="alert">
      <span className={styles.bannerIcon}>
        <Icon name="git-compare" size={18} />
      </span>
      <div className={styles.bannerText}>
        <b>pii_policy.yaml</b> changed since <b>schemabrain serve</b> started
        <div className={styles.bannerSub}>
          serve resolved policy at {drift.recorded_at ?? "unknown"}; the file was last edited at{" "}
          {drift.current_mtime ?? "missing"}. The running firewall still enforces the older policy
          — restart serve to pick up the edit.
        </div>
      </div>
      <div className={styles.bannerActions}>
        <Button icon="rotate-cw" onClick={onCopyRestart}>
          Copy restart
        </Button>
      </div>
    </div>
  );
}

function ParseErrorBanner({ error }: { error: string }) {
  return (
    <div className={styles.banner} role="alert">
      <span className={styles.bannerIcon}>
        <Icon name="git-compare" size={18} />
      </span>
      <div className={styles.bannerText}>
        <b>pii_policy.yaml</b> failed to parse
        <div className={styles.bannerSub}>falling back to the catastrophic floor · {error}</div>
      </div>
    </div>
  );
}

function PolicyEditorError({ message }: { message: string }) {
  const isNoSource =
    message.includes("409") || message.toLowerCase().includes("no source");
  return (
    <div className={styles.page}>
      <PageHead />
      <div className={styles.errorCard}>
        {isNoSource
          ? "The policy editor needs an indexed source. Run `schemabrain init` then `schemabrain index`."
          : message}
      </div>
    </div>
  );
}
