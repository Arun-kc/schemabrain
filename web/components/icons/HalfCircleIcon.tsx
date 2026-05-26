import { cn } from "@/lib/cn";

interface HalfCircleIconProps {
  className?: string;
}

/**
 * A crisp, vector-sharp half-filled circle icon.
 * Used across SchemaBrain dashboard surfaces to denote catastrophic column exposure,
 * completely avoiding cross-platform browser unicode rendering bugs.
 */
export function HalfCircleIcon({ className }: HalfCircleIconProps) {
  return (
    <svg
      className={cn("inline-block shrink-0", className)}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      aria-hidden="true"
    >
      {/* Circle outline with crisp stroke width */}
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="2.5" />
      {/* Filled left half */}
      <path d="M12 2a10 10 0 000 20V2z" fill="currentColor" />
    </svg>
  );
}
