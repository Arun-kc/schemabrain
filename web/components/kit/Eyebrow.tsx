import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

interface EyebrowProps {
  children: ReactNode;
  className?: string;
}

/** Uppercase mono label — the blueprint "registration" eyebrow. */
export function Eyebrow({ children, className }: EyebrowProps) {
  return <div className={cn("sb-eyebrow", className)}>{children}</div>;
}
