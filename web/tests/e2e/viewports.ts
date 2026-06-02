/**
 * Standard responsive breakpoints for dashboard E2E / visual regression.
 * Specs that need to assert layout at a given width call
 * `page.setViewportSize(BREAKPOINTS.tablet)`; the full per-breakpoint
 * visual-regression matrix is built on these in a later QA pass.
 */
export interface Viewport {
  width: number;
  height: number;
}

export const BREAKPOINTS = {
  mobile: { width: 375, height: 812 },
  tablet: { width: 768, height: 1024 },
  laptop: { width: 1024, height: 768 },
  desktop: { width: 1440, height: 900 },
} as const satisfies Record<string, Viewport>;

export type BreakpointName = keyof typeof BREAKPOINTS;

export const BREAKPOINT_NAMES = Object.keys(BREAKPOINTS) as BreakpointName[];
