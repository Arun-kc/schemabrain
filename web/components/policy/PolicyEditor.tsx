"use client";

import { Suspense, useMemo, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Button, Icon, useToast } from "@/components/kit";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import {
  applyVerb,
  buildPreviewQuery,
  countStagedChanges,
  highlightYaml,
  initialOverridesFromPerColumn,
  type PolicyVerb,
  type StagedOverrides,
  toggleCategoryBlock,
  type YamlTokenKind,
} from "@/lib/policy";
import { useSourceId } from "@/lib/useSourceId";
import {
  CATASTROPHIC_LEAK_CATEGORIES,
  type PIICategory,
  type PolicyColumnEntry,
  type PolicyDriftState,
  type PolicyPreviewResponse,
  type PolicyResponse,
} from "@/lib/types";
import { PerColumnPane } from "./PerColumnPane";
import styles from "./policy-editor.module.css";

/**
 * PolicyEditor — the editable PII enforcement surface (/policy), matching
 * the design handoff: a per-column block/redact/allow grid beside a
 * server-rendered schemabrain.yaml panel + staged diff. Scaled to a real
 * multi-table schema — the grid (PerColumnPane) groups by table, filters, and
 * exposes a category strip; this shell owns the staged state, the preview
 * query, and read-only Apply.
 *
 * The 3-way grid is a CLIENT PROJECTION over the engine's real
 * (category block-set × column overrides) model (ADR 0008): `lib/policy.ts`
 * maps each verb to/from the staged pair, the read-only preview route renders
 * it, and Apply copies the canonical YAML + reveals the CLI command (ADR 0006).
 */
const YAML_TOKEN_CLASS: Record<YamlTokenKind, string | undefined> = {
  comment: "tkComment",
  key: "tkKey",
  value: "tkValue",
  alarm: "tkAlarm",
  punct: "tkPunct",
  plain: undefined,
};

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
  // Gate the diff/Apply on the CLIENT's staged count, not the server's
  // `changed` flag: the on-disk YAML can disagree with the store's operator
  // overrides, which would otherwise show a phantom diff on an untouched
  // editor. If the operator hasn't staged anything, there is nothing to
  // apply — regardless of server-side block-vs-store drift.
  const hasStaged = stagedCount > 0;

  const setVerb = (row: PolicyColumnEntry, verb: PolicyVerb) => {
    setShowCommand(false);
    const next = applyVerb(stagedBlock, stagedOverrides, row.qualified_column, row.categories, verb);
    setStagedBlock(next.block);
    setStagedOverrides(next.overrides);
  };
  // Category strip: block/un-block a whole category at once (category-wide is
  // the engine grain). Only touches the block set — allow overrides stay.
  const handleToggleCategory = (category: PIICategory, block: boolean) => {
    setShowCommand(false);
    setStagedBlock(toggleCategoryBlock(stagedBlock, category, block));
  };
  const handleDiscard = () => {
    setShowCommand(false);
    setStagedBlock(initialBlock);
    setStagedOverrides(initialOverrides);
  };
  const applyCommand = `schemabrain policy apply ${data.policy_path}`;
  const handleApply = () => {
    // Never copy YAML for a superseded/stale/failed staged state: require
    // staged changes AND a settled (non-fetching, non-errored) preview that
    // reflects them. Self-guarding so it holds independent of how DiffPane
    // renders the button (keepPreviousData retains stale data on error).
    if (!hasStaged || previewQuery.isFetching || previewQuery.isError || !preview) return;
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
          <Suspense fallback={<p className={styles.skeleton}>loading columns…</p>}>
            <PerColumnPane
              perColumn={data.per_column}
              stagedBlock={stagedBlock}
              stagedOverrides={stagedOverrides}
              initialBlock={initialBlock}
              initialOverrides={initialOverrides}
              onSetVerb={setVerb}
              onToggleCategory={handleToggleCategory}
            />
          </Suspense>

          <div className={styles.col}>
            <YamlPane
              preview={preview}
              stale={hasStaged && previewQuery.isFetching && !previewQuery.isError}
              error={previewQuery.isError ? previewQuery.error.message : null}
            />
            <DiffPane
              preview={preview}
              stagedCount={stagedCount}
              hasStaged={hasStaged}
              fetching={previewQuery.isFetching}
              error={previewQuery.isError ? previewQuery.error.message : null}
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

      {/* Polite live region so staging feedback reaches assistive tech. */}
      <p className={styles.srOnly} role="status" aria-live="polite">
        {previewQuery.isError
          ? "preview failed to render"
          : stagedCount === 0
            ? "no staged changes"
            : `${stagedCount} staged ${stagedCount === 1 ? "change" : "changes"}`}
      </p>
    </div>
  );
}

function PageHead() {
  return (
    <header className={styles.pageHead}>
      <h1>Policy</h1>
      <p>
        The enforcement policy SchemaBrain compiles every query against. Tag a column to tighten
        it, clear to loosen — the catastrophic-floor columns (credential, payment card, government
        ID) are locked and can&apos;t be unblocked. Blocking acts on the column&apos;s category, so
        it can cover sibling columns; applying copies the canonical{" "}
        <code className={styles.inlineCode}>schemabrain.yaml</code> for you to commit and run.
      </p>
    </header>
  );
}

/* ─────────── server-rendered, syntax-highlighted YAML panel ─────────── */

function YamlPane({
  preview,
  stale,
  error,
}: {
  preview?: PolicyPreviewResponse;
  stale?: boolean;
  error?: string | null;
}) {
  const lines = useMemo(
    () => (preview ? highlightYaml(preview.staged_yaml) : null),
    [preview],
  );
  return (
    <section className={styles.pane} aria-label="generated policy yaml">
      <div className={styles.paneHead}>
        <Icon name="file-code" size={14} /> schemabrain.yaml
      </div>
      <pre className={cn(styles.yaml, stale && styles.stale)} aria-busy={stale || undefined}>
        {error
          ? `couldn't render preview — ${error}`
          : lines === null
          ? "rendering…"
          : lines.map((tokens, lineIndex) => (
              // YAML lines are positional + immutable per preview.
              <div key={lineIndex} className={styles.yamlLine}>
                {tokens.map((token, tokenIndex) => {
                  const cls = YAML_TOKEN_CLASS[token.kind];
                  return (
                    <span key={tokenIndex} className={cls ? styles[cls] : undefined}>
                      {token.text}
                    </span>
                  );
                })}
              </div>
            ))}
      </pre>
      <p className={styles.yamlNote}>
        <span className={styles.tkAlarm}>+ always-on floor</span> ·{" "}
        {CATASTROPHIC_LEAK_CATEGORIES.join(" · ")} — enforced on every read even when absent from{" "}
        <code className={styles.inlineCode}>block:</code>; these can&apos;t be unblocked.
      </p>
      {preview?.current_parse_error && (
        <p className={styles.yamlNote}>
          <span className={styles.tkAlarm}>current pii_policy.yaml doesn&apos;t parse</span> ·
          diffing against the catastrophic-floor default. {preview.current_parse_error}
        </p>
      )}
    </section>
  );
}

/* ─────────── staged diff + read-only apply (ADR 0006) ─────────── */

function DiffPane({
  preview,
  stagedCount,
  hasStaged,
  fetching,
  error,
  onApply,
  onDiscard,
  showCommand,
  applyCommand,
  policyPath,
  onCopyCommand,
}: {
  preview?: PolicyPreviewResponse;
  stagedCount: number;
  hasStaged: boolean;
  fetching: boolean;
  error?: string | null;
  onApply: () => void;
  onDiscard: () => void;
  showCommand: boolean;
  applyCommand: string;
  policyPath: string;
  onCopyCommand: () => void;
}) {
  // Gate on the client's staged count (ADR 0008 / phantom-diff fix), not the
  // server `changed` flag. Apply is disabled until the preview settles so we
  // never copy YAML for a superseded staged state. The dim is for in-flight
  // refetches only — never while the error branch is showing.
  const stale = hasStaged && fetching && !error;
  return (
    <section className={styles.pane} aria-label="staged changes">
      <div className={styles.paneHead}>
        <Icon name="git-pull-request" size={14} /> Pending changes
        <span className={styles.paneCount}>{stagedCount} staged</span>
      </div>
      <div className={cn(styles.diff, stale && styles.stale)} aria-busy={stale || undefined}>
        {error ? (
          <div className={styles.diffEmpty}>couldn&apos;t render preview — {error}</div>
        ) : !hasStaged || !preview ? (
          <div className={styles.diffEmpty}>
            No staged changes. Tag a column to preview a diff before applying.
          </div>
        ) : (
          <>
            {preview.diff_lines.map((line, index) => (
              <div key={index} className={cn(styles.diffRow, styles[line.kind])}>
                <span className={styles.diffSign}>
                  {line.kind === "add" ? "+" : line.kind === "remove" ? "−" : ""}
                </span>
                {line.text || " "}
              </div>
            ))}
            <div className={styles.diffFoot}>
              <Button
                variant="primary"
                icon="check"
                onClick={onApply}
                disabled={stale}
                aria-label={stale ? "Apply policy (rendering…)" : "Apply policy"}
              >
                Apply policy
              </Button>
              <Button onClick={onDiscard}>Discard</Button>
            </div>
          </>
        )}
        {showCommand && hasStaged && !error && (
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
  const isNoSource = message.includes("409") || message.toLowerCase().includes("no source");
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
