import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Icon, type IconName } from "./Icon";

export type SbButtonVariant = "default" | "primary" | "ghost";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: SbButtonVariant;
  /** Optional leading icon. */
  icon?: IconName;
  children?: ReactNode;
}

/**
 * Mono-label button. Three variants — default (hairline
 * + green hover), primary (green fill + glow), ghost (transparent). Defaults
 * `type="button"` so it never submits a form by accident.
 */
export function Button({
  variant = "default",
  icon,
  className,
  children,
  type = "button",
  ...props
}: ButtonProps) {
  return (
    <button
      type={type}
      className={cn("sb-btn", variant !== "default" && variant, className)}
      {...props}
    >
      {icon && <Icon name={icon} size={15} />}
      {children}
    </button>
  );
}
