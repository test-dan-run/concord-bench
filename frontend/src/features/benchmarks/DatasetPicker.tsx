// Search + toggle chips for every dataset. Datasets without labels for the current tab are dashed and
// listed as "hidden from this table".

import { useState } from "react";
import type { BenchDataset } from "../../lib/api";
import { hours } from "../../lib/format";
import * as I from "../../components/icons";
import { Button, cx } from "../../components/ui";

export function DatasetPicker({
  datasets,
  selected,
  onChange,
  taskWord,
}: {
  datasets: BenchDataset[];
  selected: number[];
  onChange: (ids: number[]) => void;
  taskWord: string;
}) {
  const [q, setQ] = useState("");
  const shown = datasets.filter((d) => !q || d.name.toLowerCase().includes(q.toLowerCase()) || d.tags.some((t) => t.includes(q.toLowerCase())));
  const hidden = datasets.filter((d) => selected.includes(d.id) && !d.has_labels);
  const toggle = (id: number) => onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]);
  return (
    <section aria-labelledby="ds-h" className="bg-panel border border-track rounded-lg px-4 py-3.5 flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <h2 id="ds-h" className="m-0 text-sm font-semibold">
          Datasets
        </h2>
        <label className="relative flex-1 min-w-[200px] max-w-[420px] flex">
          <span className="sr-only">Search datasets</span>
          <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-subtle">
            <I.Search size={15} />
          </span>
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search your datasets"
            className="w-full min-h-9 bg-canvas text-fg border border-line rounded-md pl-8 pr-2.5 text-[13px]"
          />
        </label>
        <span className="num text-xs text-muted">
          {selected.length} of {datasets.length} selected
        </span>
        <span className="flex gap-1.5 ml-auto">
          <Button size="sm" onClick={() => onChange(datasets.map((d) => d.id))}>
            Select all
          </Button>
          <Button size="sm" onClick={() => onChange([])}>
            Clear
          </Button>
        </span>
      </div>
      <div className="flex flex-wrap gap-2">
        {shown.map((d) => {
          const on = selected.includes(d.id);
          return (
            <button
              key={d.id}
              type="button"
              aria-pressed={on}
              onClick={() => toggle(d.id)}
              title={d.has_labels ? d.tags.map((t) => `#${t}`).join(" ") : `No ${taskWord} labels`}
              className={cx(
                "inline-flex items-center gap-2 min-h-9 px-3 rounded-md text-[13px] cursor-pointer",
                d.has_labels ? "border" : "border border-dashed",
                on ? "bg-active border-focus/60 text-fg" : "bg-transparent border-line text-muted hover:text-fg",
              )}
            >
              <span className={cx("inline-flex items-center justify-center size-4 rounded-sm border", on ? "bg-primary border-primary text-canvas" : "border-line")}>
                {on && <I.Check size={11} />}
              </span>
              <span>{d.name}</span>
              <span className="num text-[11px] text-subtle">{hours(d.hours)}</span>
            </button>
          );
        })}
        {shown.length === 0 && <span className="text-[13px] text-muted">{datasets.length ? `No datasets match “${q}”.` : "No datasets yet."}</span>}
      </div>
      {hidden.length > 0 && (
        <span className="text-xs text-muted">
          Hidden from this table (no {taskWord} labels): {hidden.map((d) => d.name).join(", ")}
        </span>
      )}
    </section>
  );
}
