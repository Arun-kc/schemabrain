import type { ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

/**
 * Vendored shadcn-style button. Owned-not-installed so contributors
 * can break the template look without fighting an upstream API.
 *
 * Variants intentionally minimal at scaffold time:
 *   - `default`: ink-on-ivory, signal-green hover ring
 *   - `quiet`:   transparent, only the underline shows on hover
 *
 * Surface designs (E-5 / E-6 / E-7) compose these primitives;
 * surface-specific affordances live in surface modules, NOT here.
 */
export type ButtonVariant = "default" | "quiet";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
}

export function Button({
  className,
  variant = "default",
  ...props
}: ButtonProps) {
  return (
    <button
      className={cn(
        "inline-flex items-center justify-center font-mono text-sm transition-all duration-fast ease-out-expo focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-(--color-signal-green) focus-visible:ring-offset-2 focus-visible:ring-offset-(--surface-base) disabled:cursor-not-allowed disabled:opacity-50",
        variant === "default" &&
          "border border-(--border-emphasis) bg-(--surface-overlay) px-rhythm-base py-rhythm-tight text-(--text-primary) hover:border-(--color-signal-green) hover:text-(--color-signal-green)",
        variant === "quiet" &&
          "px-rhythm-tight text-(--text-secondary) underline-offset-4 hover:text-(--text-primary) hover:underline",
        className,
      )}
      {...props}
    />
  );
}
