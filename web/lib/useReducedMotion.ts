"use client";

import { useEffect, useState } from "react";

const QUERY = "(prefers-reduced-motion: reduce)";

/**
 * True when the user has asked the OS to minimise motion.
 *
 * SSR-safe: returns `false` (motion allowed) for the first render — a static
 * export has no `window` and must hydrate to a stable value — then corrects to
 * the real preference in a mount effect and stays subscribed to changes.
 *
 * Motion-gated code should therefore *start* its animation from an effect that
 * depends on this value (never during render), so a reduced-motion user never
 * sees a frame of motion before the correction lands. The graph canvas uses it
 * to decide whether the parked force loop runs at all (reduced motion → render
 * the settled final positions, never start the rAF loop).
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const mq = window.matchMedia(QUERY);
    const update = () => setReduced(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);

  return reduced;
}
