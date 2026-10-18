// Shared pieces of the Label screens: the stacked progress bar, the per-dataset tab strip, the
// dataset chooser shown when a Label screen is opened without a dataset.

import { useQuery } from "@tanstack/react-query";
import { Link, NavLink } from "react-router-dom";
import { api, type ConsoleRow, type LabelProgress } from "../../lib/api";
import { hours, num } from "../../lib/format";
import { BAR_COLORS, BAR_LABELS, BAR_ORDER } from "../../lib/label";
import { cx, Empty, ErrorNote, PageHeader, Panel, Spinner } from "../../components/ui";

export function useConsole() {
  return useQuery({
    queryKey: ["label-console"],
    queryFn: () => api.get<ConsoleRow[]>("/label/console"),
    refetchInterval: (q) => (q.state.data?.some((r) => r.progress.running) ? 2000 : false),
  });
}

/** Stacked bar in audio time: confirmed / auto-accepted / needs review / rejected / running / not labelled. */
export function StatusBar({ p, className }: { p: LabelProgress; className?: string }) {
  const total = p.seconds || 1;
  const parts = BAR_ORDER.filter((k) => p.bar[k] > 0);
  const label = BAR_ORDER.map((k) => `${BAR_LABELS[k]} ${Math.round((100 * p.bar[k]) / total)}%`).join(", ");
  return (
    <span role="img" aria-label={label} title={label} className={cx("flex h-2 w-full rounded overflow-hidden bg-track", className)}>
      {parts.map((k) => (
        <span key={k} className={cx("h-full", k === "running" && "animate-pulse")} style={{ width: `${(100 * p.bar[k]) / total}%`, background: BAR_COLORS[k] }} />
      ))}
    </span>
  );
}

export function BarLegend({ p }: { p?: LabelProgress }) {
  return (
    <span className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted">
      {BAR_ORDER.map((k) => (
        <span key={k} className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-sm" style={{ background: BAR_COLORS[k] }} />
          {BAR_LABELS[k]}
          {p && p.seconds > 0 && <span className="num text-subtle">{Math.round((100 * p.bar[k]) / p.seconds)}%</span>}
        </span>
      ))}
    </span>
  );
}

/** Pseudo-label · Annotate · Live evaluation for one dataset. */
export function LabelTabs({ datasetId, current }: { datasetId: number; current: "pseudo" | "annotate" | "live" }) {
  const items: [typeof current, string, string][] = [
    ["pseudo", "Pseudo-label", `/label/pseudo/${datasetId}`],
    ["annotate", "Annotate", `/label/annotate/${datasetId}`],
    ["live", "Live evaluation", `/label/live/${datasetId}`],
  ];
  return (
    <nav aria-label="Label screens" className="flex gap-1 border-b border-track">
      {items.map(([k, label, to]) => (
        <NavLink
          key={k}
          to={to}
          aria-current={k === current ? "page" : undefined}
          className={cx("px-3 pb-2.5 pt-1 text-[13px] border-b-2 no-underline", k === current ? "border-primary !text-fg font-medium" : "border-transparent !text-muted hover:!text-fg")}
        >
          {label}
        </NavLink>
      ))}
    </nav>
  );
}

export function Crumbs({ name }: { name?: string }) {
  return (
    <>
      <Link to="/label" className="!text-muted hover:!text-fg">Label</Link>
      {name && <> · {name}</>}
    </>
  );
}

/** A Label screen opened from the nav, with no dataset yet: pick one. */
export function DatasetChooser({ title, to, subtitle }: { title: string; to: (id: number) => string; subtitle: string }) {
  const q = useConsole();
  return (
    <>
      <PageHeader crumbs={<Crumbs />} title={title} subtitle={subtitle} />
      <Panel title="Choose a dataset">
        {q.isLoading && <Spinner />}
        <ErrorNote error={q.error} />
        {q.data && q.data.length === 0 && <Empty>No datasets yet. <Link to="/datasets/upload">Upload one</Link>.</Empty>}
        <ul className="m-0 p-0 list-none flex flex-col">
          {(q.data ?? []).map((r) => (
            <li key={r.id} className="border-b border-track last:border-0">
              <Link to={to(r.id)} className="grid grid-cols-[minmax(0,1fr)_minmax(120px,240px)] items-center gap-4 px-2 py-2.5 no-underline !text-fg hover:bg-track rounded">
                <span className="flex flex-col min-w-0">
                  <span className="truncate">{r.name}</span>
                  <span className="text-xs text-muted">
                    {r.label_source} · <span className="num">{hours(r.hours)}</span> · <span className="num">{num(r.progress.segments.review)}</span> to review
                  </span>
                </span>
                <StatusBar p={r.progress} />
              </Link>
            </li>
          ))}
        </ul>
      </Panel>
    </>
  );
}
