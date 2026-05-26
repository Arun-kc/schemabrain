import { cn } from "@/lib/cn";
import type { InferenceMethod, ValidationState } from "@/lib/types";

/**
 * 2D trust glyph used inline across PII Viz cells, Refusal cards, and
 * Audit row tooltips. The inner dot encodes inference_method (origin)
 * and the outer ring encodes validation_state (rigor).
 *
 * Per RFC §5.4:
 *   - manually_authored      → ink (operator hand-typed)
 *   - fk_constraint          → signal-green (FK-derived; trustworthy)
 *   - llm_suggested          → signal-amber (LLM-guessed; verify)
 *   - dbt_import             → ink-700 (imported from dbt)
 *   - observed_in_query_log  → ink-500 (mined from query log)
 *
 * Ring thickness:
 *   - draft     = 1px outline
 *   - applied   = 2px outline
 *   - confirmed = 3px solid signal-green ring
 */

const INFERENCE_BG: Record<InferenceMethod, string> = {
  manually_authored: "bg-(--color-ink-900)",
  fk_constraint: "bg-(--color-signal-green)",
  llm_suggested: "bg-(--color-signal-amber)",
  dbt_import: "bg-(--color-ink-700)",
  observed_in_query_log: "bg-(--color-ink-500)",
};

const VALIDATION_RING: Record<ValidationState, string> = {
  draft: "ring-1 ring-(--color-ink-300)",
  applied: "ring-2 ring-(--color-ink-500)",
  confirmed: "ring-[3px] ring-(--color-signal-green)",
};

interface TrustBadgeProps {
  inferenceMethod: InferenceMethod;
  validationState: ValidationState;
  size?: "sm" | "md" | "lg";
  className?: string;
}

export function TrustBadge({
  inferenceMethod,
  validationState,
  size = "sm",
  className,
}: TrustBadgeProps) {
  const dot = size === "lg" ? "h-2.5 w-2.5" : size === "md" ? "h-2 w-2" : "h-1.5 w-1.5";
  const wrap = size === "lg" ? "h-7 w-7" : size === "md" ? "h-5 w-5" : "h-4 w-4";
  const label = `inference: ${inferenceMethod.replace(/_/g, " ")}, validation: ${validationState}`;
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-full",
        wrap,
        VALIDATION_RING[validationState],
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn("rounded-full", dot, INFERENCE_BG[inferenceMethod])}
      />
    </span>
  );
}
