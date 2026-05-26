"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import {
  CATASTROPHIC_LEAK_CATEGORIES,
  type PIICategory,
  type Sensitivity,
} from "@/lib/types";
import { TrustBadge } from "./TrustBadge";

interface DrilldownProps {
  entityName: string;
  sourceId?: string;
}

/**
 * Right-rail drill-down for the currently selected entity. Lists the
 * entity's columns with their per-column PII chips. Catastrophic
 * categories render in signal-red; sensitivity is shown as a small
 * mono label next to the column name.
 *
 * The fetch is lazy — only fires when an entity is selected, so the
 * initial matrix render isn't gated on N column requests.
 */
export function Drilldown({ entityName, sourceId }: DrilldownProps) {
  const query = useQuery({
    queryKey: ["entity-columns", entityName, sourceId],
    queryFn: () => api.entityColumns(entityName, sourceId),
  });

  if (query.isPending) {
    return (
      <div className="font-mono text-xs text-(--text-muted)">
        <p className="uppercase tracking-widest">drill</p>
        <p className="mt-1 text-(--color-ink-300)">────</p>
        <p className="mt-rhythm-base">loading {entityName}…</p>
      </div>
    );
  }
  if (query.isError) {
    return (
      <p className="font-mono text-xs text-(--color-signal-red)">
        {query.error.message}
      </p>
    );
  }

  const { entity, columns } = query.data;
  const hasAnyTags = columns.some(
    (c) => c.sensitivity !== "public" || c.pii_categories.length > 0,
  );

  return (
    <div aria-live="polite">
      <div className="font-mono text-xs uppercase tracking-widest text-(--text-muted)">
        drill
      </div>
      <p className="mt-1 font-mono text-xs text-(--color-ink-300)">────</p>
      <p className="mt-rhythm-tight font-mono text-sm text-(--color-ink-900)">
        {entity.qualified_table}
      </p>
      <div className="mt-rhythm-tight flex items-center gap-2">
        <TrustBadge
          inferenceMethod={entity.inference_method}
          validationState={entity.validation_state}
          size="md"
        />
        <span className="font-mono text-xs text-(--text-muted)">
          {entity.inference_method.replace(/_/g, " ")} · {entity.validation_state}
        </span>
      </div>

      <ul className="mt-rhythm-wide space-y-rhythm-base">
        {columns.map((col) => (
          <li key={col.name} className="border-l-2 border-(--color-ink-200) pl-rhythm-tight">
            <p className="font-mono text-sm text-(--color-ink-900)">{col.name}</p>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              <SensitivityLabel sensitivity={col.sensitivity as Sensitivity} />
              {col.pii_categories.map((cat) => (
                <CategoryChip key={cat} category={cat as PIICategory} />
              ))}
            </div>
          </li>
        ))}
      </ul>

      {!hasAnyTags && (
        <p className="mt-rhythm-wide font-mono text-xs text-(--text-muted)">
          No PII tags on this entity. The classifier found no sensitive columns;
          tag manually if you want SchemaBrain to refuse on a specific column.
        </p>
      )}
    </div>
  );
}

function SensitivityLabel({ sensitivity }: { sensitivity: Sensitivity }) {
  const tone = {
    public: "text-(--text-muted)",
    internal: "text-(--color-ink-700)",
    confidential: "text-(--color-signal-amber)",
    pii: "text-(--color-signal-red)",
  }[sensitivity];
  return (
    <span className={cn("font-mono text-xs", tone)} aria-label={`sensitivity ${sensitivity}`}>
      [{sensitivity}]
    </span>
  );
}

function CategoryChip({ category }: { category: PIICategory }) {
  const isCatastrophic = (CATASTROPHIC_LEAK_CATEGORIES as readonly string[]).includes(category);
  return (
    <span
      className={cn(
        "inline-block border px-1.5 py-0.5 font-mono text-[0.7rem]",
        isCatastrophic
          ? "border-(--color-signal-red) text-(--color-signal-red)"
          : "border-(--color-ink-300) text-(--color-ink-700)",
      )}
      title={isCatastrophic ? `${category} (catastrophic-leak default)` : category}
    >
      {category}
    </span>
  );
}
