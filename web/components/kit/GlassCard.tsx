import type { HTMLAttributes } from "react";
import { cn } from "@/lib/cn";

interface GlassCardProps extends HTMLAttributes<HTMLDivElement> {
  /** `card` = solid panel surface; `glass` = frosted backdrop-blur surface. */
  variant?: "card" | "glass";
}

/** Surface primitive — solid card or frosted glass, both token-driven. */
export function GlassCard({ variant = "card", className, ...props }: GlassCardProps) {
  return <div className={cn(variant === "glass" ? "sb-glass" : "sb-card", className)} {...props} />;
}
