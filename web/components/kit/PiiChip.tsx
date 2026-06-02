import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * Visual variants for the PII / status chip. `payment` and `auth` are the
 * catastrophic (alarm-coloured) variants and read distinctly from the
 * lower-sensitivity `contact` and the neutral states.
 */
export type PiiChipKind =
  | "neutral"
  | "contact"
  | "payment"
  | "auth"
  | "none"
  | "green"
  | "cyan";

interface PiiChipProps {
  kind?: PiiChipKind;
  /** Render the leading status dot (inherits the chip's colour). */
  dot?: boolean;
  children: ReactNode;
  className?: string;
}

export function PiiChip({ kind = "neutral", dot = false, children, className }: PiiChipProps) {
  return (
    <span className={cn("sb-chip", kind, className)}>
      {dot && <span className="d" style={{ background: "currentColor" }} aria-hidden="true" />}
      {children}
    </span>
  );
}

/**
 * Map a PII category to its chip variant. Catastrophic categories (auth,
 * payment) map to their alarm-coloured variants; `contact` to the amber
 * contact variant; everything else to the muted `none`.
 */
export function piiCategoryToChipKind(category: string): PiiChipKind {
  if (category === "auth") return "auth";
  if (category === "payment") return "payment";
  if (category === "contact") return "contact";
  return "none";
}
