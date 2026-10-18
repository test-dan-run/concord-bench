// Virtualised, server-side sorted / filtered table with expandable rows. Pages are fetched as the
// user scrolls (TanStack Query infinite query); rows are virtualised (TanStack Virtual), each row
// and its expansion live in their own <tbody> so variable heights are measured correctly.

import { useInfiniteQuery } from "@tanstack/react-query";
import { flexRender, getCoreRowModel, useReactTable, type ColumnDef, type SortingState } from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Page } from "../lib/api";
import { num } from "../lib/format";
import { cx, Spinner } from "./ui";

declare module "@tanstack/react-table" {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData, TValue> {
    align?: "left" | "right";
    width?: string;
    sortKey?: string;
  }
}

export interface SortableTableProps<T> {
  columns: ColumnDef<T, unknown>[];
  queryKey: unknown[];
  fetchPage: (p: { offset: number; limit: number; sort: string; order: "asc" | "desc" }) => Promise<Page<T>>;
  initialSort: { id: string; desc: boolean };
  rowKey: (row: T) => string | number;
  renderExpanded?: (row: T) => ReactNode;
  expanded?: string | number | null;
  onExpand?: (key: string | number | null) => void;
  maxHeight?: number | string;
  pageSize?: number;
  caption?: string;
  empty?: ReactNode;
}

export function SortableTable<T>({
  columns,
  queryKey,
  fetchPage,
  initialSort,
  rowKey,
  renderExpanded,
  expanded: expandedProp,
  onExpand,
  maxHeight = "70vh",
  pageSize = 200,
  caption,
  empty,
}: SortableTableProps<T>) {
  const [sorting, setSorting] = useState<SortingState>([initialSort]);
  const [expandedLocal, setExpandedLocal] = useState<string | number | null>(null);
  const expanded = expandedProp !== undefined ? expandedProp : expandedLocal;
  const setExpanded = onExpand ?? setExpandedLocal;
  const sort = sorting[0] ?? initialSort;
  const sortKey = (columns.find((c) => c.id === sort.id)?.meta?.sortKey ?? sort.id) as string;
  const order = sort.desc ? "desc" : "asc";

  const q = useInfiniteQuery({
    queryKey: [...queryKey, sortKey, order],
    queryFn: ({ pageParam }) => fetchPage({ offset: pageParam, limit: pageSize, sort: sortKey, order }),
    initialPageParam: 0,
    getNextPageParam: (last, all) => {
      const loaded = all.reduce((a, p) => a + p.rows.length, 0);
      return loaded < last.total ? loaded : undefined;
    },
  });
  const rows = useMemo(() => (q.data?.pages ?? []).flatMap((p) => p.rows), [q.data]);
  const total = q.data?.pages[0]?.total ?? 0;

  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    manualSorting: true,
    enableSortingRemoval: false,
    sortDescFirst: true,
    getCoreRowModel: getCoreRowModel(),
  });

  const scroller = useRef<HTMLDivElement>(null);
  const tRows = table.getRowModel().rows;
  // Stable callbacks: a new getItemKey every render makes the virtualizer re-measure and re-render
  // forever (no visible change, but router navigations, which React runs as transitions, never commit).
  const rowKeyRef = useRef(rowKey);
  rowKeyRef.current = rowKey;
  const getItemKey = useCallback((i: number) => (rows[i] ? rowKeyRef.current(rows[i]) : i), [rows]);
  const getScrollElement = useCallback(() => scroller.current, []);
  const estimateSize = useCallback(() => 44, []);
  const virt = useVirtualizer({
    count: tRows.length,
    getScrollElement,
    estimateSize,
    overscan: 8,
    getItemKey,
  });
  const items = virt.getVirtualItems();
  const last = items[items.length - 1];
  useEffect(() => {
    if (last && last.index >= tRows.length - 20 && q.hasNextPage && !q.isFetchingNextPage) void q.fetchNextPage();
  }, [last, tRows.length, q]);

  const pad0 = items.length ? items[0].start : 0;
  const pad1 = items.length ? virt.getTotalSize() - items[items.length - 1].end : 0;
  const ncol = columns.length;

  return (
    <div className="flex flex-col gap-2 min-w-0">
      <div ref={scroller} className="overflow-auto scroll-thin border border-track rounded-md" style={{ maxHeight }}>
        <table className="w-full border-collapse text-[13px]" style={{ minWidth: 760 }}>
          {caption && <caption className="sr-only">{caption}</caption>}
          <thead className="sticky top-0 z-10 bg-panel">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id}>
                {hg.headers.map((h) => {
                  // Sorting is server-side, so any column can sort unless it opts out; TanStack's own
                  // getCanSort() needs an accessor, which these display columns don't have.
                  const canSort = h.column.columnDef.enableSorting !== false;
                  const dir = sort.id === h.column.id ? (sort.desc ? "desc" : "asc") : false;
                  const toggle = () =>
                    setSorting([{ id: h.column.id, desc: sort.id === h.column.id ? !sort.desc : true }]);
                  const meta = h.column.columnDef.meta;
                  return (
                    <th
                      key={h.id}
                      scope="col"
                      aria-sort={dir ? (dir === "asc" ? "ascending" : "descending") : undefined}
                      style={{ width: meta?.width }}
                      className={cx("px-3 py-2 border-b border-line text-[11px] font-medium text-muted uppercase tracking-wider whitespace-nowrap", meta?.align === "right" ? "text-right" : "text-left")}
                    >
                      {canSort ? (
                        <button
                          type="button"
                          onClick={toggle}
                          className={cx("inline-flex items-center gap-1 bg-transparent border-0 p-0 cursor-pointer uppercase tracking-wider text-[11px]", dir ? "text-fg" : "text-muted hover:text-fg")}
                        >
                          {flexRender(h.column.columnDef.header, h.getContext())}
                          <span aria-hidden className="num">{dir === "asc" ? "▲" : dir === "desc" ? "▼" : "↕"}</span>
                        </button>
                      ) : (
                        flexRender(h.column.columnDef.header, h.getContext())
                      )}
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          {pad0 > 0 && (
            <tbody aria-hidden>
              <tr style={{ height: pad0 }} />
            </tbody>
          )}
          {items.map((vi) => {
            const row = tRows[vi.index];
            const key = rowKey(row.original);
            const open = expanded === key;
            return (
              <tbody key={vi.key} data-index={vi.index} ref={virt.measureElement}>
                <tr
                  className={cx("border-b border-track hover:bg-track/60", open && "bg-active/60")}
                  onClick={(e) => {
                    if (!renderExpanded) return;
                    const t = e.target as HTMLElement;
                    if (t.closest("button,a,input,select,label")) return;
                    setExpanded(open ? null : key);
                  }}
                >
                  {row.getVisibleCells().map((c) => (
                    <td key={c.id} className={cx("px-3 py-2 align-top", c.column.columnDef.meta?.align === "right" && "text-right")}>
                      {flexRender(c.column.columnDef.cell, c.getContext())}
                    </td>
                  ))}
                </tr>
                {open && renderExpanded && (
                  <tr className="bg-canvas/60 border-b border-line">
                    <td colSpan={ncol} className="p-0">
                      <Fragment>{renderExpanded(row.original)}</Fragment>
                    </td>
                  </tr>
                )}
              </tbody>
            );
          })}
          {pad1 > 0 && (
            <tbody aria-hidden>
              <tr style={{ height: pad1 }} />
            </tbody>
          )}
        </table>
        {q.isLoading && (
          <div className="p-4">
            <Spinner />
          </div>
        )}
        {!q.isLoading && rows.length === 0 && <div className="p-6 text-center text-muted text-[13px]">{empty ?? "No rows."}</div>}
      </div>
      <div className="flex items-center justify-between text-xs text-muted">
        <span className="num">
          {num(rows.length)} of {num(total)} shown{q.isFetchingNextPage ? " · loading more…" : ""}
        </span>
        {q.isError && <span className="text-bad">{String(q.error)}</span>}
      </div>
    </div>
  );
}

/** Header-free helper for an expander cell. */
export function ExpandCell({ open, label }: { open: boolean; label: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5 num text-xs whitespace-nowrap">
      <span aria-hidden className={cx("inline-block transition-transform text-muted", open && "rotate-90")}>›</span>
      {label}
    </span>
  );
}
