// SOURCE: schemabrain/datadict/render_common.py CATEGORY_LABELS /
// SENSITIVITY_LABELS — the canonical human labels for the closed PII
// taxonomy. Mirrored here for the data-dictionary surface and its Markdown
// export. KEEP IN SYNC: `labels.test.ts` pins key coverage to the enums,
// and the byte-for-byte golden parity test (`serialize.test.ts`) pins every
// value exercised by the demo store. Edit both sides in one commit.

import type { PIICategory, Sensitivity } from "@/lib/types";
import { isCatastrophic } from "@/lib/types";

/** Canonical human label for each of the 12 PII categories. */
export const CATEGORY_LABELS: Record<PIICategory, string> = {
  contact: "Contact",
  financial: "Financial",
  payment_card: "Payment Card",
  health: "Health",
  genetic: "Genetic",
  biometric: "Biometric",
  behavioral: "Behavioral",
  online_identifier: "Online Identifier",
  credential: "Credential",
  government_id: "Government ID",
  location: "Location",
  demographic_protected: "Protected Demographic",
};

/** Canonical label for each sensitivity level. "pii" → the acronym, not "Pii". */
export const SENSITIVITY_LABELS: Record<Sensitivity, string> = {
  public: "Public",
  internal: "Internal",
  confidential: "Confidential",
  pii: "PII",
};

/** Human label for a PII category; the raw token on an unknown value (total). */
export function categoryLabel(category: PIICategory): string {
  return CATEGORY_LABELS[category] ?? category;
}

/** Human label for a sensitivity level; the raw token on an unknown value. */
export function sensitivityLabel(sensitivity: Sensitivity): string {
  return SENSITIVITY_LABELS[sensitivity] ?? sensitivity;
}

/** True when this category is in the catastrophic-leak set (re-exported sync). */
export function isCatastrophicCategory(category: PIICategory): boolean {
  return isCatastrophic(category);
}
