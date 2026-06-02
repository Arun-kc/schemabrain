import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface DataTableColumn<Row> {
  /** Stable key for the column (React key + cell key). */
  key: string;
  header: ReactNode;
  cell: (row: Row) => ReactNode;
  align?: "left" | "right" | "center";
}

interface DataTableProps<Row> {
  columns: DataTableColumn<Row>[];
  rows: readonly Row[];
  /** Stable key per row — never fall back to array index for real data. */
  getRowKey: (row: Row, index: number) => string;
  className?: string;
  caption?: ReactNode;
  emptyMessage?: ReactNode;
}

/** Generic, typed data table over the `.sb-table` surface. */
export function DataTable<Row>({
  columns,
  rows,
  getRowKey,
  className,
  caption,
  emptyMessage = "No rows",
}: DataTableProps<Row>) {
  return (
    <table className={cn("sb-table", className)}>
      {caption && (
        <caption className="sb-eyebrow" style={{ textAlign: "left", paddingBottom: "var(--s2)" }}>
          {caption}
        </caption>
      )}
      <thead>
        <tr>
          {columns.map((col) => (
            <th key={col.key} style={{ textAlign: col.align ?? "left" }}>
              {col.header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <tr>
            <td colSpan={columns.length} style={{ color: "var(--ink-3)" }}>
              {emptyMessage}
            </td>
          </tr>
        ) : (
          rows.map((row, index) => (
            <tr key={getRowKey(row, index)}>
              {columns.map((col) => (
                <td key={col.key} style={{ textAlign: col.align ?? "left" }}>
                  {col.cell(row)}
                </td>
              ))}
            </tr>
          ))
        )}
      </tbody>
    </table>
  );
}
