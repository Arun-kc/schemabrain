import clsx, { type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Compose conditional class names while resolving Tailwind merge
 * conflicts (e.g. `px-2` vs `px-rhythm-base` — the later wins).
 *
 * The shadcn convention; vendored here to avoid pulling shadcn's
 * full util package for a one-liner.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
