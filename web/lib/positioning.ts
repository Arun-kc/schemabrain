// SOURCE: schemabrain/positioning.py — DO NOT EDIT WITHOUT UPDATING THE PYTHON
//
// Canonical positioning strings, mirrored from the Python single source of
// truth. The TS↔Python parity test (tests/dashboard/test_positioning_parity.py)
// fails CI if these drift. The landing page (hero, install terminal, eyebrow)
// imports these so the marketed framing can never diverge from the package.

/** Hero H1 / one-line positioning. Mirrors positioning.TAGLINE. */
export const TAGLINE =
  "The trust and intelligence layer between AI agents and your database.";

/** Primary install command (hero CTA + install terminal). Mirrors INSTALL_PRIMARY. */
export const INSTALL_PRIMARY = "uvx schemabrain init";

/** SPDX license label shown in the hero eyebrow. Mirrors LICENSE_LABEL. */
export const LICENSE_LABEL = "Apache-2.0";
