import { EntitiesIndex } from "@/components/entities/EntitiesIndex";

/**
 * Entities surface (/entities), mounted inside the app shell (the shell owns the
 * chrome: top bar, nav rail, source selector, theme — see (app)/layout.tsx).
 *
 * THE INDEX — the sortable table companion to the knowledge graph. Selecting a
 * row opens the shared `?entity=` drilldown sheet that the graph also drives.
 * Source-scoped and read-only.
 */
export default function EntitiesPage() {
  return <EntitiesIndex />;
}
