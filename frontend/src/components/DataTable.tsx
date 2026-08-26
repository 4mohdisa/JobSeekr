// A generic table with sorting, filtering, pagination and an expandable row.
//
// Built once and used by Jobs, Applications and the Answer bank. Each of those
// would otherwise grow its own sort state and its own "no results" markup.

import { useMemo, useState, type ReactNode } from "react";
import { Empty, Input, cx } from "./ui";

export interface Column<T> {
  key: string;
  header: string;
  /** Cell content. Falls back to the raw value when omitted. */
  render?: (row: T) => ReactNode;
  /** Sort key. Omit to make the column unsortable. */
  sortValue?: (row: T) => string | number | null | undefined;
  className?: string;
  width?: string;
}

export interface DataTableProps<T> {
  rows: T[];
  columns: Column<T>[];
  rowKey: (row: T) => string | number;
  /** Free-text filter across these fields. Omit to hide the search box. */
  searchable?: (row: T) => string;
  searchPlaceholder?: string;
  /** Rendered under a row when it is expanded. Omit to disable expansion. */
  expanded?: (row: T) => ReactNode;
  pageSize?: number;
  empty?: ReactNode;
  toolbar?: ReactNode;
}

export function DataTable<T>({
  rows,
  columns,
  rowKey,
  searchable,
  searchPlaceholder = "Filter…",
  expanded,
  pageSize = 50,
  empty = "Nothing here yet.",
  toolbar,
}: DataTableProps<T>) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<{ key: string; desc: boolean } | null>(null);
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState<string | number | null>(null);

  const filtered = useMemo(() => {
    if (!searchable || !query.trim()) return rows;
    const needle = query.trim().toLowerCase();
    return rows.filter((row) => searchable(row).toLowerCase().includes(needle));
  }, [rows, query, searchable]);

  const sorted = useMemo(() => {
    if (!sort) return filtered;
    const column = columns.find((c) => c.key === sort.key);
    if (!column?.sortValue) return filtered;

    return [...filtered].sort((a, b) => {
      const left = column.sortValue!(a);
      const right = column.sortValue!(b);
      // Missing values sort last regardless of direction — a blank is not a
      // small number, and floating them to the top buries the real rows.
      if (left === right) return 0;
      if (left === null || left === undefined) return 1;
      if (right === null || right === undefined) return -1;
      const order = left < right ? -1 : 1;
      return sort.desc ? -order : order;
    });
  }, [filtered, sort, columns]);

  const pages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const current = Math.min(page, pages - 1);
  const visible = sorted.slice(current * pageSize, current * pageSize + pageSize);

  const toggleSort = (key: string) =>
    setSort((existing) =>
      existing?.key === key ? { key, desc: !existing.desc } : { key, desc: true },
    );

  return (
    <div>
      {(searchable || toolbar) && (
        <div className="mb-3 flex flex-wrap items-center gap-2">
          {searchable && (
            <Input
              value={query}
              placeholder={searchPlaceholder}
              onChange={(event) => {
                setQuery(event.target.value);
                setPage(0);
              }}
              className="max-w-xs"
            />
          )}
          {toolbar}
          <span className="tnum ml-auto text-xs text-ink-400">
            {sorted.length} {sorted.length === 1 ? "row" : "rows"}
          </span>
        </div>
      )}

      {sorted.length === 0 ? (
        <Empty>{empty}</Empty>
      ) : (
        <div className="overflow-x-auto rounded border border-ink-800">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-ink-800 bg-ink-850 text-left">
                {expanded && <th className="w-8" />}
                {columns.map((column) => (
                  <th
                    key={column.key}
                    style={column.width ? { width: column.width } : undefined}
                    className={cx(
                      "px-3 py-2 text-xs font-semibold tracking-wide text-ink-400 uppercase",
                      column.sortValue && "cursor-pointer select-none hover:text-ink-100",
                      column.className,
                    )}
                    onClick={column.sortValue ? () => toggleSort(column.key) : undefined}
                  >
                    {column.header}
                    {sort?.key === column.key && (
                      <span className="ml-1 text-accent">{sort.desc ? "▾" : "▴"}</span>
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visible.map((row) => {
                const key = rowKey(row);
                const isOpen = open === key;
                return (
                  <>
                    <tr
                      key={key}
                      className={cx(
                        "border-b border-ink-800 last:border-0",
                        expanded && "cursor-pointer hover:bg-ink-850",
                      )}
                      onClick={expanded ? () => setOpen(isOpen ? null : key) : undefined}
                    >
                      {expanded && (
                        <td className="px-2 py-2 text-ink-600">{isOpen ? "▾" : "▸"}</td>
                      )}
                      {columns.map((column) => (
                        <td key={column.key} className={cx("px-3 py-2", column.className)}>
                          {column.render
                            ? column.render(row)
                            : String((row as Record<string, unknown>)[column.key] ?? "")}
                        </td>
                      ))}
                    </tr>
                    {expanded && isOpen && (
                      <tr key={`${key}-detail`} className="border-b border-ink-800 bg-ink-950">
                        <td colSpan={columns.length + 1} className="px-4 py-3">
                          {expanded(row)}
                        </td>
                      </tr>
                    )}
                  </>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {pages > 1 && (
        <div className="mt-2 flex items-center justify-end gap-2 text-xs text-ink-400">
          <button
            className="rounded px-2 py-1 hover:bg-ink-800 disabled:opacity-30"
            disabled={current === 0}
            onClick={() => setPage(current - 1)}
          >
            ← Prev
          </button>
          <span className="tnum">
            {current + 1} / {pages}
          </span>
          <button
            className="rounded px-2 py-1 hover:bg-ink-800 disabled:opacity-30"
            disabled={current >= pages - 1}
            onClick={() => setPage(current + 1)}
          >
            Next →
          </button>
        </div>
      )}
    </div>
  );
}
