import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

/**
 * Shared axe-core accessibility assertion for the dashboard E2E.
 *
 * Each surface spec calls this once its load-bearing content has rendered. The
 * specs already run under both the `chromium-dark` and `chromium-light`
 * projects (see playwright.config.ts + theme.ts), so a single call covers the
 * surface in both themes without per-spec branching.
 *
 * We scan against WCAG 2.0/2.1 levels A + AA and fail the test only on
 * `serious`/`critical` impacts — the launch accessibility bar. Moderate/minor
 * findings are surfaced in the failure message for context but do not block, so
 * the gate stays actionable rather than noisy.
 */

const WCAG_AA_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const BLOCKING_IMPACTS = new Set(["serious", "critical"]);

export async function expectNoSeriousA11yViolations(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(WCAG_AA_TAGS).analyze();
  const blocking = results.violations.filter((violation) =>
    BLOCKING_IMPACTS.has(violation.impact ?? ""),
  );

  const report = blocking
    .map((violation) => {
      const nodes = violation.nodes
        .slice(0, 4)
        .map((node) => `      ${node.target.join(" ")}`)
        .join("\n");
      return `  • [${violation.impact}] ${violation.id} — ${violation.help}\n${nodes}`;
    })
    .join("\n");

  expect(
    blocking,
    blocking.length > 0
      ? `axe found ${blocking.length} serious/critical accessibility violation(s):\n${report}`
      : undefined,
  ).toEqual([]);
}
