import type { ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/cn";
import { Icon, type IconName } from "./Icon";

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  icon: IconName;
  /** Required accessible name — an icon-only control must be labelled. */
  label: string;
  size?: number;
}

/** Square icon-only button. `label` is mandatory for accessibility. */
export function IconButton({
  icon,
  label,
  size = 16,
  className,
  type = "button",
  ...props
}: IconButtonProps) {
  return (
    <button
      type={type}
      className={cn("sb-iconbtn", className)}
      aria-label={label}
      title={label}
      {...props}
    >
      <Icon name={icon} size={size} />
    </button>
  );
}
