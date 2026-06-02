import { cn } from "@/lib/cn";

interface ConfidenceMeterProps {
  /** Confidence in the 0–1 range; clamped before rendering. */
  value: number;
  className?: string;
}

function clamp01(value: number): number {
  if (Number.isNaN(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

/** Inline track + percentage label for a 0–1 confidence score. */
export function ConfidenceMeter({ value, className }: ConfidenceMeterProps) {
  const pct = Math.round(clamp01(value) * 100);
  return (
    <span
      className={cn("sb-meter", className)}
      role="meter"
      aria-valuenow={pct}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={`Confidence ${pct}%`}
    >
      <span className="track">
        <span className="fill" style={{ width: `${pct}%` }} />
      </span>
      {pct}%
    </span>
  );
}
