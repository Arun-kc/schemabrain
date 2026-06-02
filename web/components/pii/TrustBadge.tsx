import { cn } from "@/lib/cn";
import type { InferenceMethod, ValidationState } from "@/lib/types";

/**
 * 2D trust glyph used inline across PII Viz cells, Refusal cards, and
 * Audit row tooltips. The inner dot encodes inference_method (origin)
 * and the outer ring encodes validation_state (rigor).
 *
 * v2 — Boardroom Brief redesign:
 *   - Three sizes: sm (14px) inline matrix cells, md (24px) card
 *     headers, lg (40px) featured surface headers.
 *   - Always paired with a 2-char mono tag (`ma`, `fk`, `ll`, `db`,
 *     `ql`) to disambiguate the dot color, since 5 methods × 3
 *     states cannot be reliably encoded by glyph alone at small sizes.
 *   - The tag renders to the right of the glyph in a `Trust` wrapper
 *     that callers compose via <TrustBadge ... /> + adjacent text, or
 *     via the convenience <TrustBadgeWithTag /> wrapper below.
 */

const INFERENCE_BG: Record<InferenceMethod, string> = {
  manually_authored: "bg-(--ink)",
  fk_constraint: "bg-(--green)",
  llm_suggested: "bg-(--amber)",
  dbt_import: "bg-(--ink-2)",
  observed_in_query_log: "bg-(--ink-3)",
};

const VALIDATION_RING: Record<ValidationState, string> = {
  draft: "ring-1 ring-(--hair)",
  applied: "ring-2 ring-(--ink-3)",
  confirmed: "ring-[3px] ring-(--green)",
};

const METHOD_TAG: Record<InferenceMethod, string> = {
  manually_authored: "ma",
  fk_constraint: "fk",
  llm_suggested: "ll",
  dbt_import: "db",
  observed_in_query_log: "ql",
};

interface TrustBadgeProps {
  inferenceMethod: InferenceMethod;
  validationState: ValidationState;
  size?: "sm" | "md" | "lg";
  /** When true, renders the 2-char mono method tag adjacent to the glyph. */
  showTag?: boolean;
  className?: string;
}

export function TrustBadge({
  inferenceMethod,
  validationState,
  size = "sm",
  showTag = false,
  className,
}: TrustBadgeProps) {
  const dot = size === "lg" ? "h-3 w-3" : size === "md" ? "h-2 w-2" : "h-1.5 w-1.5";
  const wrap =
    size === "lg" ? "h-[40px] w-[40px]" : size === "md" ? "h-6 w-6" : "h-[14px] w-[14px]";
  const tagSize = size === "lg" ? "text-sm" : size === "md" ? "text-xs" : "text-[0.6875rem]";
  const label = `inference: ${inferenceMethod.replace(/_/g, " ")}, validation: ${validationState}`;
  return (
    <span
      className={cn("inline-flex items-center gap-[0.4em]", className)}
      role="img"
      aria-label={label}
      title={label}
    >
      <span
        className={cn(
          "inline-flex shrink-0 items-center justify-center rounded-full",
          wrap,
          VALIDATION_RING[validationState],
        )}
      >
        <span
          aria-hidden="true"
          className={cn("rounded-full", dot, INFERENCE_BG[inferenceMethod])}
        />
      </span>
      {showTag && (
        <span
          aria-hidden="true"
          className={cn(
            "font-mono uppercase tracking-wider text-(--ink-2)",
            tagSize,
          )}
        >
          {METHOD_TAG[inferenceMethod]}
        </span>
      )}
    </span>
  );
}
