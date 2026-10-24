// ASR error analysis by label (EVALUATE_PLAN.md §7.4): the dataset's labels, where the model fails (slices
// whose 95% interval is wholly worse than its overall score), one label's slices as a chart, two labels at
// once, and the selected slice with its worst utterances. A slice can be saved as a Benchmarks column.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, qs, type Heatmap, type Run, type SliceFilter, type SliceReport, type SliceRow, type SliceUnits } from "../../lib/api";
import { useNormaliser } from "../../lib/config";
import { ASR_METRIC_INFO, deltaPts, deltaTone, pct } from "../../lib/format";
import { NormaliserButtons } from "../../components/NormaliserButtons";
import * as I from "../../components/icons";
import { Button, Chip, cx, Empty, ErrorNote, PageHeader, Panel, Segmented, Select, Spinner } from "../../components/ui";
import { AsrViews, Crumbs, latestRuns, RunPickers, Switcher, useDoneRuns, useSwitchLinks } from "./common";

interface Summary {
  run: Run;
  score_id: number;
  compare: { run: Run; score_id: number } | null;
}

export const PATTERN: Record<string, { label: string; title: string; cls: string }> = {
  worse: { label: "worse", title: "Reliably worse: the whole 95% interval is worse than the overall score", cls: "border-bad/50 text-bad" },
  hard: { label: "hard audio", title: "Both models are reliably worse here: the audio is hard, not just this model", cls: "border-warn/50 text-warn" },
  better: { label: "better", title: "Reliably better than the overall score", cls: "border-good/50 text-good" },
  same: { label: "no difference", title: "The interval includes the overall score", cls: "border-line text-muted" },
  too_few: { label: "too few", title: "Too few units: the interval is too wide to call", cls: "border-line text-subtle" },
  none: { label: "—", title: "", cls: "border-line text-subtle" },
};

export function PatternChip({ p }: { p: string }) {
  const x = PATTERN[p] ?? PATTERN.none;
  return (
    <span title={x.title} className={cx("inline-flex border rounded px-1.5 py-px text-[11px] whitespace-nowrap", x.cls)}>
      {x.label}
    </span>
  );
}

/** A slice's score with its 95% interval and the overall score as a line, on a shared scale. */
export function IntervalBar({ score, ci, overall, max, compare }: { score: number | null; ci: [number, number] | null; overall: number | null; max: number; compare?: number | null }) {
  const x = (v: number) => `${Math.max(0, Math.min(100, (v / (max || 1)) * 100))}%`;
  return (
    <span className="relative block h-4 min-w-[120px] flex-1" role="img" aria-label={`${pct(score)}${ci ? `, 95% interval ${pct(ci[0])} to ${pct(ci[1])}` : ""}${overall !== null ? `, overall ${pct(overall)}` : ""}`}>
      <span className="absolute inset-x-0 top-1/2 h-px bg-track" />
      {ci && <span className="absolute top-1/2 -translate-y-1/2 h-1.5 rounded bg-secondary/40" style={{ left: x(ci[0]), width: `calc(${x(ci[1])} - ${x(ci[0])})` }} />}
      {overall !== null && <span className="absolute top-0 bottom-0 border-l border-dashed border-muted" style={{ left: x(overall) }} />}
      {compare !== null && compare !== undefined && <span className="absolute top-1/2 -translate-y-1/2 size-2 rounded-full border border-warn bg-canvas" style={{ left: `calc(${x(compare)} - 4px)` }} />}
      {score !== null && <span className="absolute top-1/2 -translate-y-1/2 size-2.5 rounded-full bg-focus" style={{ left: `calc(${x(score)} - 5px)` }} />}
    </span>
  );
}

function sliceName(r: Pick<SliceRow, "key" | "value">) {
  return `${r.key} = ${String(r.value)}`;
}

export function filtersOf(r: SliceRow): SliceFilter[] {
  return r.kind === "continuous" ? [{ key: r.key, min: r.min, max: r.max }] : [{ key: r.key, value: r.value }];
}

export function AsrLabels() {
  const { runId } = useParams();
  const [params, setParams] = useSearchParams();
  const [normaliser] = useNormaliser();
  const compare = params.get("compare") ?? undefined;
  const [metric, setMetric] = useState("wer");
  const [label, setLabel] = useState<string | null>(null);
  const [bins, setBins] = useState<"even" | "quantile" | "custom">("even");
  const [edges, setEdges] = useState("");
  const [picked, setPicked] = useState<{ name: string; filters: SliceFilter[] } | null>(null);
  const s = useQuery({
    queryKey: ["asr-summary", runId, normaliser, compare],
    queryFn: () => api.get<Summary>(`/errors/asr/summary${qs({ run_id: runId, normaliser, compare_run_id: compare })}`),
  });
  const scoreId = s.data?.score_id;
  const cmpId = s.data?.compare?.score_id;
  const all = useQuery({
    queryKey: ["slices", scoreId, cmpId, metric],
    queryFn: () => api.get<SliceReport>(`/errors/slices${qs({ score_id: scoreId, compare_score_id: cmpId, metric })}`),
    enabled: !!scoreId,
  });
  const usable = (all.data?.labels ?? []).filter((l) => l.kind !== "unused");
  const cur = label ?? all.data?.finder?.[0]?.key ?? usable[0]?.key ?? null;
  const one = useQuery({
    queryKey: ["slices", scoreId, cmpId, metric, cur, bins, edges],
    queryFn: () => api.get<SliceReport>(`/errors/slices${qs({ score_id: scoreId, compare_score_id: cmpId, metric, label: cur, bins, edges: bins === "custom" ? edges : undefined })}`),
    enabled: !!scoreId && !!cur,
  });
  const links = useSwitchLinks(s.data?.run.dataset_id, "");
  const others = latestRuns(useDoneRuns("asr").data ?? []).filter((r) => s.data && r.dataset_id === s.data.run.dataset_id && r.id !== s.data.run.id);
  const set = (k: string, v: string | undefined) => {
    const p = new URLSearchParams(params);
    if (v) p.set(k, v);
    else p.delete(k);
    setParams(p, { replace: true });
  };
  if (s.isLoading) return <Spinner />;
  if (s.error || !s.data) return <ErrorNote error={s.error} />;
  const d = s.data;
  const info = ASR_METRIC_INFO[metric];
  const overall = all.data?.overall.value ?? null;
  const cmpName = d.compare?.run.label;

  return (
    <>
      <PageHeader
        crumbs={<Crumbs parts={[{ to: "/benchmarks/asr", label: "Benchmarks" }, "ASR", d.run.dataset]} />}
        title={d.run.label}
        actions={
          <RunPickers
            task="asr"
            run={d.run}
            path={(r) => `/errors/asr/${r.id}/labels${qs({ normaliser })}`}
            extra={
              <>
                <Select label="Metric" value={metric} onChange={(e) => setMetric(e.target.value)}>
                  {(["wer", "cer", "mer", "wil", "wip"] as const).map((k) => (
                    <option key={k} value={k}>
                      {ASR_METRIC_INFO[k].label}
                    </option>
                  ))}
                </Select>
                <Select label="Compare with" value={compare ?? ""} onChange={(e) => set("compare", e.target.value || undefined)}>
                  <option value="">—</option>
                  {others.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.label}
                    </option>
                  ))}
                </Select>
                <NormaliserButtons />
              </>
            }
          />
        }
      />
      <Switcher current="asr" links={links} />
      <AsrViews runId={d.run.id} current="label" />
      {all.isLoading && <Spinner />}
      <ErrorNote error={all.error} />
      {all.data && (
        <>
          <Panel id="lab-h" title="Labels on this dataset" subtitle={<>From the attributes that came with the dataset's references, plus speaker and utterance length. Pick one to see how errors change across its values. <Link to={`/datasets/${d.run.dataset_id}`}>See the dataset's labels</Link></>}>
            <div className="flex flex-col gap-3">
              {(["discrete", "continuous"] as const).map((kind) => {
                const ls = usable.filter((l) => l.kind === kind);
                if (!ls.length) return null;
                return (
                  <div key={kind} className="flex flex-col gap-1.5">
                    <span className="text-[11px] text-muted uppercase tracking-wider">{kind === "discrete" ? "Discrete" : "Continuous"}</span>
                    <div className="flex flex-wrap gap-2">
                      {ls.map((l) => {
                        const on = l.key === cur;
                        return (
                          <button key={l.key} type="button" aria-pressed={on} onClick={() => { setLabel(l.key); setBins("even"); }} className={cx("flex flex-col items-start gap-0.5 min-h-11 px-3 py-1.5 rounded-md border cursor-pointer text-left", on ? "border-focus bg-active text-fg" : "border-line bg-transparent text-muted hover:text-fg")}>
                            <span className="flex items-center gap-1.5 text-[13px]">
                              <span className="num">{l.key}</span>
                              {(l.weak ?? 0) > 0 && <span title="Values where this model is reliably worse than its overall score" className="text-[10px] text-bad border border-bad/50 rounded px-1">{l.weak} weak</span>}
                              {l.derived && <span className="text-[10px] text-subtle">derived</span>}
                            </span>
                            <span className="num text-[11px] text-subtle">
                              {l.values ?? "?"} {kind === "discrete" ? "values" : "bins"} · {Math.round((l.have / Math.max(1, l.of)) * 100)}% labelled
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
              {usable.length === 0 && <Empty>No labels to slice by on this dataset.</Empty>}
            </div>
          </Panel>
          <Panel id="ff-h" title={`Where ${d.run.label} fails`} subtitle={`${info.label} by slice, worst first. The bar is the slice's 95% interval; the dashed line is the overall ${pct(overall)}. Slices with fewer than ${all.data.min_units} utterances are too few to call.`}>
            <SliceTable rows={all.data.finder ?? []} overall={overall} low={all.data.low_better} cmpName={cmpName} onPick={(r) => { setLabel(r.key); setPicked({ name: sliceName(r), filters: filtersOf(r) }); }} />
          </Panel>
          {cur && (
            <Panel
              id="ch-h"
              title={`${info.label} by ${cur}`}
              subtitle={one.data ? `${one.data.rows?.length ?? 0} slices · ${one.data.unlabelled ? `${one.data.unlabelled} utterances without a value are left out` : "every utterance has a value"}` : undefined}
              actions={
                <span className="flex flex-wrap gap-3 text-[11px] text-muted">
                  <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-full bg-focus" />{d.run.label}</span>
                  {cmpName && <span className="flex items-center gap-1.5"><span className="size-2 rounded-full border border-warn" />{cmpName}</span>}
                  <span className="flex items-center gap-1.5"><span className="h-3 border-l border-dashed border-muted" />Overall</span>
                  <span className="flex items-center gap-1.5"><span className="h-1.5 w-4 rounded bg-secondary/40" />95% interval</span>
                </span>
              }
            >
              {usable.find((l) => l.key === cur)?.kind === "continuous" && (
                <div className="flex flex-wrap items-center gap-3">
                  <Segmented label="Bins" value={bins} onChange={setBins} options={[{ value: "even", label: "Even width" }, { value: "quantile", label: "Quantiles (equal counts)" }, { value: "custom", label: "Custom edges" }]} />
                  {bins === "custom" && (
                    <label className="flex items-center gap-2 text-xs text-muted">
                      Edges
                      <input value={edges} onChange={(e) => setEdges(e.target.value)} placeholder="e.g. 5, 10, 20" className="num min-h-8 w-40 bg-canvas border border-line rounded-md px-2 text-[13px] text-fg" />
                    </label>
                  )}
                </div>
              )}
              {one.isLoading ? <Spinner /> : <SliceChart rows={one.data?.rows ?? []} overall={overall} picked={picked?.name} onPick={(r) => setPicked({ name: sliceName(r), filters: filtersOf(r) })} />}
            </Panel>
          )}
          <div className="grid gap-5 xl:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
            <TwoLabels scoreId={d.score_id} labels={usable.map((l) => l.key)} metric={metric} low={info.low} onPick={(name, filters) => setPicked({ name, filters })} />
            <SelectedSlice scoreId={d.score_id} datasetId={d.run.dataset_id} metric={metric} picked={picked} runId={d.run.id} />
          </div>
        </>
      )}
    </>
  );
}

export function SliceTable({ rows, overall, low, cmpName, onPick, unit = "Utts" }: { rows: SliceRow[]; overall: number | null; low: boolean; cmpName?: string; onPick?: (r: SliceRow) => void; unit?: string }) {
  if (!rows.length) return <span className="text-[13px] text-muted">Nothing to show: no labels with enough values.</span>;
  const max = Math.max(0.01, ...rows.map((r) => r.ci?.[1] ?? r.score ?? 0), overall ?? 0) * 1.05;
  return (
    <div className="overflow-x-auto scroll-thin">
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
            <th scope="col" className="font-medium px-3 py-2 border-b border-line">Slice</th>
            <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">{unit}</th>
            <th scope="col" className="font-medium px-3 py-2 border-b border-line">Score · 95% interval · overall ┆</th>
            <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">vs overall</th>
            {cmpName && <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">{cmpName} here</th>}
            <th scope="col" className="font-medium px-3 py-2 border-b border-line">Pattern</th>
            {onPick && <th scope="col" className="px-3 py-2 border-b border-line"><span className="sr-only">Actions</span></th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            return (
              <tr key={`${r.key}-${String(r.value)}`} className="border-b border-track">
                <td className="px-3 py-2">
                  <span className="flex flex-col">
                    <span className="num text-[11px] text-subtle">{r.key}</span>
                    <span className="num">{String(r.value)}</span>
                  </span>
                </td>
                <td className="px-3 py-2 text-right num">{r.n}</td>
                <td className="px-3 py-2">
                  <span className="flex items-center gap-3">
                    <span className="num w-14 text-right">{pct(r.score)}</span>
                    <IntervalBar score={r.score} ci={r.ci} overall={overall} max={max} compare={r.compare?.score} />
                  </span>
                </td>
                <td className={cx("px-3 py-2 text-right num text-xs", deltaTone(r.delta, low))}>{deltaPts(r.delta)}</td>
                {cmpName && <td className="px-3 py-2 text-right num text-xs text-muted">{pct(r.compare?.score)}</td>}
                <td className="px-3 py-2"><PatternChip p={r.pattern} /></td>
                {onPick && (
                  <td className="px-3 py-2 text-right">
                    <Button size="sm" onClick={() => onPick(r)} aria-label={`Inspect ${sliceName(r)}`}>
                      Inspect <I.Arrow size={12} />
                    </Button>
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function SliceChart({ rows, overall, picked, onPick }: { rows: SliceRow[]; overall: number | null; picked?: string; onPick: (r: SliceRow) => void }) {
  if (!rows.length) return <span className="text-[13px] text-muted">No values to show.</span>;
  const max = Math.max(0.01, ...rows.map((r) => r.ci?.[1] ?? r.score ?? 0), overall ?? 0) * 1.05;
  const nmax = Math.max(1, ...rows.map((r) => r.n));
  return (
    <div className="flex flex-col gap-1">
      {rows.map((r) => {
        const on = picked === sliceName(r);
        return (
          <button key={String(r.value)} type="button" aria-pressed={on} onClick={() => onPick(r)} className={cx("grid grid-cols-[minmax(110px,180px)_minmax(0,1fr)_120px] items-center gap-3 px-2 py-1.5 rounded-md cursor-pointer border text-left", on ? "border-focus bg-active/50" : "border-transparent bg-transparent hover:bg-track")}>
            <span className="flex flex-col min-w-0">
              <span className="flex items-center gap-1.5 text-[13px] num truncate">
                {String(r.value)} {(r.pattern === "worse" || r.pattern === "hard") && <PatternChip p={r.pattern} />}
              </span>
              <span className="flex items-center gap-1.5">
                <span className="h-1 rounded bg-line" style={{ width: `${(r.n / nmax) * 60}px` }} />
                <span className="num text-[11px] text-subtle">n {r.n}</span>
              </span>
            </span>
            <IntervalBar score={r.score} ci={r.ci} overall={overall} max={max} compare={r.compare?.score} />
            <span className="num text-xs text-right">
              {pct(r.score)} {r.ci && <span className="text-subtle">± {((r.ci[1] - r.ci[0]) * 50).toFixed(1)}</span>}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function TwoLabels({ scoreId, labels, metric, low, onPick }: { scoreId: number; labels: string[]; metric: string; low: boolean; onPick: (name: string, f: SliceFilter[]) => void }) {
  const [row, setRow] = useState<string | null>(null);
  const [col, setCol] = useState<string | null>(null);
  const r = row ?? labels[0];
  const c = col ?? labels.find((l) => l !== r) ?? null;
  const q = useQuery({ queryKey: ["heat", scoreId, r, c, metric], queryFn: () => api.get<Heatmap>(`/errors/slices/heatmap${qs({ score_id: scoreId, row: r, col: c, metric })}`), enabled: !!r && !!c && r !== c });
  const h = q.data;
  return (
    <Panel id="hm-h" title="Two labels at once" subtitle="Failures often hide where two labels meet.">
      {labels.length < 2 ? (
        <span className="text-[13px] text-muted">Needs at least two labels.</span>
      ) : (
        <>
          <div className="flex flex-wrap gap-3">
            <Select label="Rows" value={r ?? ""} onChange={(e) => setRow(e.target.value)}>
              {labels.map((l) => (
                <option key={l} value={l}>{l}</option>
              ))}
            </Select>
            <Select label="Columns" value={c ?? ""} onChange={(e) => setCol(e.target.value)}>
              {labels.filter((l) => l !== r).map((l) => (
                <option key={l} value={l}>{l}</option>
              ))}
            </Select>
          </div>
          {q.isLoading && <Spinner />}
          {h && (
            <div className="overflow-x-auto scroll-thin">
              <table className="border-collapse text-[12px]">
                <thead>
                  <tr>
                    <th scope="col" className="text-left text-[11px] text-muted font-medium px-2 py-1">{r} ╲ {c}</th>
                    {h.cols.map((x) => (
                      <th key={String(x)} scope="col" className="num text-[11px] text-muted font-medium px-2 py-1">{String(x)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {h.rows.map((row_, ri) => (
                    <tr key={String(row_.label)}>
                      <th scope="row" className="text-left num text-[12px] font-normal px-2 py-1">{String(row_.label)}</th>
                      {row_.cells.map((cell, i) => {
                        const d = cell.score !== null && h.overall !== null ? cell.score - h.overall : null;
                        const bad = d !== null && (low ? d > 0 : d < 0);
                        const rel = d === null ? 0 : Math.min(1, Math.abs(d) / 0.1);
                        const bg = !cell.colour || d === null ? "rgba(30,41,59,0.5)" : `${bad ? "rgba(244,63,94," : "rgba(16,185,129,"}${(0.12 + rel * 0.5).toFixed(2)})`;
                        return (
                          <td key={i} className="p-0.5">
                            <button
                              type="button"
                              disabled={!cell.n}
                              aria-label={`${r} ${String(row_.label)}, ${c} ${String(h.cols[i])}: ${pct(cell.score)}, ${cell.n} utterances`}
                              onClick={() => onPick(`${r} = ${String(row_.label)}, ${c} = ${String(h.cols[i])}`, [h.row_filters[ri], h.col_filters[i]])}
                              className="w-[78px] flex flex-col items-center rounded px-1.5 py-1 border-0 cursor-pointer disabled:cursor-default"
                              style={{ background: bg }}
                            >
                              <span className="num text-[12px]">{pct(cell.score)}</span>
                              <span className="num text-[10px] text-subtle">n {cell.n}</span>
                            </button>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <span className="text-[11px] text-subtle">Green is better than the overall {pct(h?.overall)}, red worse. Cells with fewer than {h?.min_units ?? 10} utterances are not coloured.</span>
        </>
      )}
    </Panel>
  );
}

function SelectedSlice({ scoreId, datasetId, metric, picked, runId }: { scoreId: number; datasetId: number; metric: string; picked: { name: string; filters: SliceFilter[] } | null; runId: number }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["slice-units", scoreId, metric, picked?.name],
    queryFn: () => api.get<SliceUnits>(`/errors/slices/units${qs({ score_id: scoreId, metric, filters: JSON.stringify(picked!.filters) })}`),
    enabled: !!picked,
  });
  const save = useMutation({
    mutationFn: () => api.post(`/datasets/${datasetId}/slices`, { name: picked!.name, filters: picked!.filters }),
    onSuccess: () => void qc.invalidateQueries(),
  });
  if (!picked)
    return (
      <Panel title="Selected slice">
        <span className="text-[13px] text-muted">Pick a slice in the table, the chart or the heatmap.</span>
      </Panel>
    );
  const u = q.data;
  return (
    <Panel id="sl-h" title={picked.name} subtitle={u ? `${u.n} of ${u.of} utterances` : undefined}>
      {q.isLoading && <Spinner />}
      {u && (
        <>
          <div className="grid grid-cols-3 gap-2">
            <div className="bg-canvas border border-track rounded-md px-3 py-2 flex flex-col">
              <span className="text-[11px] text-muted">{ASR_METRIC_INFO[metric]?.label ?? metric} here</span>
              <span className="num text-[16px]">{pct(u.value)}</span>
            </div>
            <div className="bg-canvas border border-track rounded-md px-3 py-2 flex flex-col">
              <span className="text-[11px] text-muted">95% interval</span>
              <span className="num text-[13px]">{u.ci ? `${pct(u.ci[0])} – ${pct(u.ci[1])}` : "—"}</span>
            </div>
            <div className="bg-canvas border border-track rounded-md px-3 py-2 flex flex-col">
              <span className="text-[11px] text-muted">Overall</span>
              <span className="num text-[16px]">{pct(u.overall)}</span>
            </div>
          </div>
          <span className="text-[11px] text-muted uppercase tracking-wider">Worst utterances here</span>
          {u.worst.map((w) => (
            <div key={w.id} className="flex flex-col gap-0.5 border-b border-track last:border-0 pb-1.5">
              <span className="flex flex-wrap items-center gap-1.5">
                <span className="num text-xs">{w.key}</span>
                {Object.entries(w.labels).slice(0, 3).map(([k, v]) => (
                  <Chip key={k}>{String(v)}</Chip>
                ))}
                <span className="num text-xs text-bad ml-auto">{ASR_METRIC_INFO.wer.label} {pct(w.score)}</span>
              </span>
              <span className="num text-[12px] text-muted">ref <span className="text-fg">{w.ref}</span></span>
              <span className="num text-[12px] text-muted">hyp <span className="text-fg">{w.hyp}</span></span>
            </div>
          ))}
          {u.worst.length === 0 && <span className="text-[13px] text-muted">No utterances.</span>}
          <span className="flex flex-wrap items-center gap-3">
            <Link to={`/errors/asr/${runId}`} className="text-[13px]">Open the utterances</Link>
            <Button size="sm" variant="primary" disabled={save.isPending || save.isSuccess} onClick={() => save.mutate()}>
              <I.Plus size={13} /> {save.isSuccess ? "Saved" : "Save as a slice"}
            </Button>
          </span>
          <span className="text-[11px] text-subtle">A saved slice shows up as an extra column on Benchmarks, so every new model is scored on it too. It belongs to this dataset.</span>
          <ErrorNote error={save.error} />
        </>
      )}
    </Panel>
  );
}
