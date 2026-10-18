import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { ConsoleRow, NextAction } from "../../lib/api";
import { capLabel, hours, num } from "../../lib/format";
import * as I from "../../components/icons";
import { Chip, Empty, ErrorNote, LinkButton, PageHeader, Panel, Spinner, Toggle } from "../../components/ui";
import { BarLegend, StatusBar, useConsole } from "./common";

type Filter = "all" | "review" | "running" | "unlabelled" | "complete";

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "review", label: "Needs review" },
  { value: "running", label: "Running" },
  { value: "unlabelled", label: "Not labelled" },
  { value: "complete", label: "Complete" },
];

function matches(r: ConsoleRow, f: Filter): boolean {
  const s = r.progress.segments;
  if (f === "review") return s.review + s.rejected + s.auto > 0;
  if (f === "running") return r.progress.running;
  if (f === "unlabelled") return r.progress.files.unlabelled > 0;
  if (f === "complete") return r.next.kind === "live";
  return true;
}

export function nextHref(id: number, n: NextAction): string {
  if (n.kind === "review") return `/label/annotate/${id}${n.status ? `?status=${n.status}` : ""}`;
  if (n.kind === "live") return `/label/live/${id}`;
  return `/label/pseudo/${id}`;
}

export function ReviewConsole() {
  const q = useConsole();
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const rows = useMemo(() => {
    const t = search.trim().toLowerCase();
    return (q.data ?? []).filter((r) => matches(r, filter)).filter((r) => {
      if (!t) return true;
      if (t.startsWith("#")) return r.tags.some((g) => g.toLowerCase().includes(t.slice(1)));
      return r.name.toLowerCase().includes(t) || r.tags.some((g) => g.toLowerCase().includes(t));
    });
  }, [q.data, filter, search]);
  return (
    <>
      <PageHeader
        crumbs="Label"
        title="Review console"
        subtitle="Labelling progress per dataset, in audio time. Auto-accepted labels never become benchmark references until you confirm them."
        actions={
          <>
            <LinkButton to="/label/sync">
              <I.Refresh size={14} /> Import / export progress
            </LinkButton>
            <LinkButton to="/datasets/upload" variant="primary">
              <I.Upload size={14} /> Upload dataset
            </LinkButton>
          </>
        }
      />
      <Panel>
        <div className="flex flex-wrap items-center gap-3">
          <Toggle label="Status filter" options={FILTERS} value={filter} onChange={(v) => setFilter(v as Filter)} />
          <label className="flex items-center gap-2 bg-canvas border border-line rounded-md px-2 min-h-[34px] text-xs text-muted">
            <I.Search size={13} />
            <input
              aria-label="Search datasets by name or #tag"
              placeholder="Name or #tag"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="bg-transparent border-0 outline-none text-fg text-[13px] w-44"
            />
          </label>
          <span className="ml-auto">
            <BarLegend />
          </span>
        </div>
        {q.isLoading && <Spinner />}
        <ErrorNote error={q.error} />
        {q.data && q.data.length === 0 && (
          <Empty>
            No datasets yet. <Link to="/datasets/upload">Upload audio</Link>, with or without labels.
          </Empty>
        )}
        {q.data && q.data.length > 0 && rows.length === 0 && <Empty>No dataset matches.</Empty>}
        {rows.length > 0 && (
          <div className="overflow-x-auto scroll-thin">
            <table className="w-full border-collapse text-[13px]" style={{ minWidth: 860 }}>
              <thead>
                <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Dataset</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Label source</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Tasks</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line w-[34%]">Progress</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Next</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const s = r.progress.segments;
                  return (
                    <tr key={r.id} className="border-b border-track hover:bg-track/50 align-top">
                      <td className="px-3 py-2.5">
                        <span className="flex flex-col gap-1">
                          <Link to={`/datasets/${r.id}`} className="!text-fg hover:!text-focus">{r.name}</Link>
                          <span className="flex flex-wrap gap-1">
                            <span className="text-xs text-muted num">{hours(r.hours)}</span>
                            {r.tags.map((t) => <Chip key={t}>#{t}</Chip>)}
                          </span>
                        </span>
                      </td>
                      <td className="px-3 py-2.5 text-muted">{r.label_source}</td>
                      <td className="px-3 py-2.5">
                        <span className="flex gap-1">
                          {r.tasks.length ? r.tasks.map((t) => <Chip key={t}>{capLabel(t)}</Chip>) : <span className="text-subtle">—</span>}
                        </span>
                      </td>
                      <td className="px-3 py-2.5">
                        <span className="flex flex-col gap-1.5">
                          <StatusBar p={r.progress} />
                          <span className="text-[11px] text-muted num">
                            {num(s.confirmed)} confirmed · {num(s.auto)} auto · {num(s.review)} to review · {num(s.rejected)} rejected
                            {r.progress.files.unlabelled > 0 && ` · ${num(r.progress.files.unlabelled)} files not labelled`}
                          </span>
                        </span>
                      </td>
                      <td className="px-3 py-2.5">
                        <LinkButton to={nextHref(r.id, r.next)} variant={r.next.kind === "review" || r.next.kind === "pseudo" ? "primary" : "outline"}>
                          {r.next.label}
                        </LinkButton>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}
