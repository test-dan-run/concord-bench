// The ASR and diarisation leaderboards (EVALUATE_PLAN.md §4, §6): every model, and every model reading an
// enhancer's copy of the datasets ("model + enhancer" rows), scored per dataset. Columns sort best or worst
// first; ASR shows any metric across datasets, or every metric on one dataset; hardware filters to where
// models ran; saved slices are extra columns after their dataset.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, qs, type AsrMetric, type BenchCell, type BenchTable, type Enhancer, type Hardware } from "../../lib/api";
import { ERROR_COLORS } from "../../lib/colors";
import { ago, ASR_METRIC_INFO, deltaPts, duration, hwLabel, num, pct, rtf, speed } from "../../lib/format";
import * as I from "../../components/icons";
import { Button, cx, Empty, EnhChip, ErrorNote, Fact, Panel, Segmented, Spinner, StatusDot } from "../../components/ui";
import { DatasetPicker } from "./DatasetPicker";

const SEL_KEY = "concord.benchmarks.datasets";
const ONE_METRICS = ["wer", "cer", "mer", "wil", "wip", "s", "d", "i"] as const;
type OneKey = (typeof ONE_METRICS)[number];

function loadSel(): number[] | null {
  try {
    const v = localStorage.getItem(SEL_KEY);
    return v ? (JSON.parse(v) as number[]) : null;
  } catch {
    return null;
  }
}

function saveSel(ids: number[]) {
  try {
    localStorage.setItem(SEL_KEY, JSON.stringify(ids));
  } catch {
    /* private mode */
  }
}

interface Variant {
  key: string;
  modelId: number;
  name: string;
  catalogId: string;
  sub: string;
  enh: Enhancer | null;
}

interface Col {
  key: string;
  label: string;
  sub: string;
  title?: string;
  low: boolean;
  count?: boolean;
  /** The value of a row in this column, and its change (vs the previous run, or vs original audio). */
  get: (v: Variant) => { value: number | null; delta: number | null; deltaVsOriginal: boolean; cell: BenchCell | null };
  datasetId: number;
}

type Sort = { key: string; dir: "best" | "worst" };

export function Leaderboard({ tab, normaliser, collar, skipOverlap }: { tab: "asr" | "diar"; normaliser: string; collar: number; skipOverlap: boolean }) {
  const qc = useQueryClient();
  const [metric, setMetric] = useState<AsrMetric>("wer");
  const q = useQuery({
    queryKey: ["bench", tab, normaliser, collar, metric, skipOverlap],
    queryFn: () => api.get<BenchTable>(`/benchmarks/${tab}${qs({ normaliser, collar, metric: tab === "asr" ? metric : undefined, skip_overlap: tab === "diar" && skipOverlap ? true : undefined })}`),
    refetchInterval: (query) =>
      query.state.data?.cells.some((c) => c.status === "queued" || c.status === "waiting" || c.status === "running" || c.rerunning || c.pending_score_job) ||
      query.state.data?.copies.some((c) => c.status === "building")
        ? 3000
        : false,
  });
  const [selected, setSelected] = useState<number[] | null>(loadSel);
  const [view, setView] = useState<"across" | "one">("across");
  const [oneDs, setOneDs] = useState<number | null>(null);
  const [hw, setHw] = useState<"all" | "gpu" | "cpu" | "hosted">("all");
  const [mode, setMode] = useState<"off" | "best" | "all">("best");
  const [arrange, setArrange] = useState<"group" | "rank">("group");
  const [enhOff, setEnhOff] = useState<number[]>([]);
  const [sort, setSort] = useState<Sort | null>(null);
  const [inspect, setInspect] = useState<{ v: string; d: number } | null>(null);
  const data = q.data;
  const sel = useMemo(() => selected ?? data?.datasets.filter((d) => d.has_labels).map((d) => d.id) ?? [], [selected, data]);
  useEffect(() => {
    if (selected) saveSel(selected);
  }, [selected]);

  const runCell = useMutation({
    mutationFn: ({ v, d }: { v: Variant; d: number }) =>
      api.post("/runs", { task: tab === "asr" ? "asr" : "seg", model_id: v.modelId, dataset_id: d, enh_model_id: v.enh?.id }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["bench"] }),
  });

  if (q.isLoading) return <Spinner />;
  if (q.error || !data) return <ErrorNote error={q.error} />;
  const isAsr = tab === "asr";
  const taskWord = isAsr ? "transcript" : "speaker";
  const labelled = data.datasets.filter((d) => d.has_labels);
  const cols0 = data.datasets.filter((d) => sel.includes(d.id) && d.has_labels);
  const one = labelled.find((d) => d.id === oneDs) ?? cols0[0] ?? labelled[0];
  const across = !isAsr || view === "across";

  const cellOf = (m: number, d: number, e: number | null) => data.cells.find((c) => c.model_id === m && c.dataset_id === d && c.enh_model_id === e) ?? null;
  const modelInfo = (id: number) => data.models.find((m) => m.id === id)!;
  const deviceOf = (m: number): string | undefined => {
    const info = modelInfo(m);
    for (const c of data.cells) if (c.model_id === m && c.hardware?.[info.catalog_id]?.device) return c.hardware[info.catalog_id].device;
    return undefined;
  };
  const shownModels = data.models.filter((m) => hw === "all" || deviceOf(m.id) === hw);
  const hiddenByHw = data.models.filter((m) => !shownModels.includes(m));
  const devices = [...new Set(shownModels.map((m) => deviceOf(m.id)).filter(Boolean))];
  const enhOn = data.enhancers.filter((e) => !enhOff.includes(e.id));
  const variantsOf = (m: (typeof data.models)[number]): Variant[] => {
    const sub = m.kind === "vad" ? "speech only" : m.kind === "diar" ? "speakers" : m.timing_level ? `${m.timing_level} times` : "";
    const base: Variant = { key: `${m.id}`, modelId: m.id, name: m.display_name, catalogId: m.catalog_id, sub, enh: null };
    if (mode === "off") return [base];
    const kids = enhOn.filter((e) => data.cells.some((c) => c.model_id === m.id && c.enh_model_id === e.id)).map((e) => ({ ...base, key: `${m.id}+${e.id}`, enh: e }));
    return [base, ...kids];
  };

  // columns
  const sliceCols = (d: (typeof data.datasets)[number]): Col[] =>
    data.slices
      .filter((s) => s.dataset_id === d.id)
      .map((s) => ({
        key: `s${s.id}`,
        label: s.name,
        sub: `slice of ${d.name}`,
        title: s.filters.map((f) => ("value" in f ? `${f.key} = ${f.value}` : `${f.key} ${f.min ?? ""}–${f.max ?? ""}`)).join(", "),
        low: isAsr ? ASR_METRIC_INFO[metric].low : true,
        datasetId: d.id,
        get: (v) => {
          const c = cellOf(v.modelId, d.id, v.enh?.id ?? null);
          return { value: c?.slices?.[String(s.id)]?.value ?? null, delta: null, deltaVsOriginal: false, cell: c };
        },
      }));
  const datasetCol = (d: (typeof data.datasets)[number]): Col => ({
    key: `d${d.id}`,
    label: d.name,
    sub: isAsr ? `${d.files ?? 0} files · ${d.hours ? `${(d.hours * 60).toFixed(1)} min` : "—"}` : `${d.files ?? 0} files · ${d.hours ? `${(d.hours * 60).toFixed(1)} min` : "—"}`,
    low: isAsr ? ASR_METRIC_INFO[metric].low : true,
    datasetId: d.id,
    get: (v) => {
      const c = cellOf(v.modelId, d.id, v.enh?.id ?? null);
      const vs = v.enh ? (c?.vs_original ?? null) : (c?.delta ?? null);
      return { value: c?.value ?? null, delta: vs, deltaVsOriginal: !!v.enh, cell: c };
    },
  });
  const oneCol = (k: OneKey): Col => ({
    key: k,
    label: k.length === 1 ? k.toUpperCase() : ASR_METRIC_INFO[k].label,
    sub: k.length === 1 ? "count" : ASR_METRIC_INFO[k].low ? "↓ lower" : "↑ higher",
    title: k.length === 1 ? { s: "Substitutions", d: "Deletions", i: "Insertions" }[k as "s"] : ASR_METRIC_INFO[k].name,
    low: k === "wip" ? false : true,
    count: k.length === 1,
    datasetId: one?.id ?? 0,
    get: (v) => {
      const c = one ? cellOf(v.modelId, one.id, v.enh?.id ?? null) : null;
      const val = (c?.metrics?.[k] as number | null | undefined) ?? null;
      const base = v.enh && one ? ((cellOf(v.modelId, one.id, null)?.metrics?.[k] as number | null | undefined) ?? null) : null;
      return { value: val, delta: v.enh && val !== null && base !== null ? val - base : null, deltaVsOriginal: !!v.enh, cell: c };
    },
  });
  const cols: Col[] = across ? cols0.flatMap((d) => [datasetCol(d), ...sliceCols(d)]) : ONE_METRICS.map(oneCol);
  const dsCols = cols.filter((c) => c.key.startsWith("d"));
  const mean = (v: Variant): number | null => {
    if (!across) return null;
    const vals = dsCols.map((c) => c.get(v).value);
    return vals.length && vals.every((x) => x !== null) ? (vals as number[]).reduce((a, b) => a + b, 0) / vals.length : null;
  };
  const rtfOf = (v: Variant): number | null => {
    const ids = across ? dsCols.map((c) => c.datasetId) : one ? [one.id] : [];
    const xs = ids.map((d) => cellOf(v.modelId, d, v.enh?.id ?? null)).filter((c): c is BenchCell => !!c && c.rtf !== null && c.rtf !== undefined);
    if (!xs.length) return null;
    return xs.reduce((a, c) => a + (c.rtf ?? 0) + (c.rtf_by_stage?.enh ?? 0), 0) / xs.length;
  };
  const lowMean = isAsr ? ASR_METRIC_INFO[metric].low : true;
  const sortKey = sort?.key ?? (across ? "mean" : "wer");
  const sortDir = sort?.dir ?? "best";
  const keyLow = sortKey === "rtf" ? true : sortKey === "mean" ? lowMean : (cols.find((c) => c.key === sortKey)?.low ?? true);
  const valueFor = (v: Variant): number | null => (sortKey === "rtf" ? rtfOf(v) : sortKey === "mean" ? mean(v) : (cols.find((c) => c.key === sortKey)?.get(v).value ?? null));
  const cmp = (a: number | null, b: number | null) => {
    if (a === null && b === null) return 0;
    if (a === null) return 1;
    if (b === null) return -1;
    const best = keyLow ? a - b : b - a;
    return sortDir === "best" ? best : -best;
  };
  // order: groups of a model and its enhanced variants, or every row ranked
  const groups = shownModels.map((m) => {
    const vs = variantsOf(m);
    let kids = vs.slice(1).sort((a, b) => cmp(valueFor(a), valueFor(b)));
    if (mode === "best") kids = kids.slice(0, 1).filter((k) => valueFor(k) !== null);
    return { plain: vs[0], kids };
  });
  let rows: { v: Variant; child: boolean }[];
  if (mode === "off" || arrange === "rank") {
    rows = groups.flatMap((g) => [g.plain, ...g.kids]).sort((a, b) => cmp(valueFor(a), valueFor(b))).map((v) => ({ v, child: false }));
  } else {
    const scored = groups.map((g) => ({ g, best: [g.plain, ...g.kids].map(valueFor).filter((x) => x !== null).sort((a, b) => cmp(a, b))[0] ?? null }));
    scored.sort((a, b) => cmp(a.best, b.best));
    rows = scored.flatMap(({ g }) => [{ v: g.plain, child: false }, ...g.kids.map((v) => ({ v, child: true }))]);
  }
  const bestOf = (c: Col) => {
    const vals = rows.map((r) => c.get(r.v).value).filter((x): x is number => x !== null);
    return vals.length > 1 ? (c.low ? Math.min(...vals) : Math.max(...vals)) : null;
  };
  const bests = Object.fromEntries(cols.map((c) => [c.key, bestOf(c)]));
  const bestVariant = (m: number) => {
    const g = groups.find((x) => x.plain.modelId === m);
    if (!g) return null;
    return [g.plain, ...g.kids].filter((x) => valueFor(x) !== null).sort((a, b) => cmp(valueFor(a), valueFor(b)))[0]?.key ?? null;
  };
  const setSortKey = (key: string) => setSort(sortKey === key ? { key, dir: sortDir === "best" ? "worst" : "best" } : { key, dir: "best" });

  // inspector target
  const firstWithValue = rows.flatMap((r) => cols.map((c) => ({ r, c }))).find(({ r, c }) => c.get(r.v).value !== null);
  const insp = inspect ?? (firstWithValue ? { v: firstWithValue.r.v.key, d: firstWithValue.c.datasetId } : null);
  const iv = insp ? rows.find((r) => r.v.key === insp.v)?.v ?? null : null;
  const ic = iv && insp ? cellOf(iv.modelId, insp.d, iv.enh?.id ?? null) : null;
  const ids = data.datasets.find((d) => d.id === insp?.d);
  const copyState = (d: number, e: number | null) => (e ? data.copies.find((c) => c.dataset_id === d && c.enh_model_id === e) : undefined);
  const def = isAsr ? ASR_METRIC_INFO[across ? metric : "wer"] : null;
  const title = isAsr ? (across ? ASR_METRIC_INFO[metric].name : `All metrics · ${one?.name ?? ""}`) : "Diarisation error rate";
  const subtitle = isAsr
    ? `${ASR_METRIC_INFO[across ? metric : "wer"].low ? "Lower" : "Higher"} is better · normaliser ${data.normaliser}`
    : `Lower is better · collar ±${data.collar} s${data.skip_overlap ? " · overlapped speech not scored" : ""} · speech-only models show VAD error (missed + false alarm)`;

  return (
    <>
      {across && <DatasetPicker datasets={data.datasets} selected={sel} onChange={setSelected} taskWord={taskWord} />}
      <section aria-label="Leaderboard controls" className="bg-panel border border-track rounded-lg px-4 py-3.5 flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
          {isAsr && <Segmented label="View" value={view} onChange={setView} options={[{ value: "across", label: "Across datasets" }, { value: "one", label: "All metrics, one dataset" }]} />}
          {isAsr && across && (
            <Segmented label="Metric" value={metric} onChange={setMetric} options={(["wer", "cer", "mer", "wil", "wip"] as const).map((k) => ({ value: k, label: ASR_METRIC_INFO[k].label, title: ASR_METRIC_INFO[k].name }))} />
          )}
          {isAsr && !across && <Segmented label="Dataset" value={one?.id ?? 0} onChange={setOneDs} options={labelled.map((d) => ({ value: d.id, label: d.name }))} />}
          <Segmented label="Hardware" value={hw} onChange={setHw} options={[{ value: "all", label: "All hardware" }, { value: "gpu", label: "GPU" }, { value: "cpu", label: "CPU" }, { value: "hosted", label: "Hosted" }]} />
        </div>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
          <Segmented label="Enhancement" value={mode} onChange={setMode} options={[{ value: "off", label: "Off" }, { value: "best", label: "Best per model" }, { value: "all", label: "All variants" }]} />
          {mode !== "off" && (
            <>
              <Segmented label="Arrange" value={arrange} onChange={setArrange} options={[{ value: "group", label: "Group by model" }, { value: "rank", label: "Rank all" }]} />
              <span className="flex flex-wrap gap-1.5">
                {data.enhancers.map((e) => {
                  const on = !enhOff.includes(e.id);
                  return (
                    <button
                      key={e.id}
                      type="button"
                      aria-pressed={on}
                      onClick={() => setEnhOff(on ? [...enhOff, e.id] : enhOff.filter((x) => x !== e.id))}
                      className={cx("min-h-7 px-2.5 rounded-md text-xs cursor-pointer border", on ? "border-enh bg-enh/10 text-enh-soft" : "border-line bg-transparent text-muted")}
                    >
                      {e.display_name}
                    </button>
                  );
                })}
                {data.enhancers.length === 0 && <span className="text-xs text-subtle">No runs on enhanced audio yet. Tick enhancers in Run evaluation.</span>}
              </span>
            </>
          )}
        </div>
        <span className="flex items-start gap-2 text-xs text-muted">
          <I.Info size={14} />
          {mode === "off"
            ? "Original audio only. Turn enhancement on to add rows where each model reads an enhanced copy of the dataset."
            : "A row named model + enhancer is that model reading the enhancer's copy of each dataset. Under each score: the change against the previous run for plain models, against original audio for enhanced rows."}
        </span>
        {def && (
          <span className="flex items-start gap-2 text-xs text-muted">
            <I.Info size={14} />
            <span>
              <span className="text-fg">{def.name}</span> · <span className="num">{def.formula}</span> · {def.text}
            </span>
          </span>
        )}
      </section>
      <div className="flex flex-col xl:flex-row gap-5 items-start">
        <Panel
          className="flex-1 w-full"
          title={title}
          subtitle={
            <>
              {subtitle}
              <br />
              Sorted by {sortKey === "rtf" ? "RTF" : sortKey === "mean" ? "mean" : (cols.find((c) => c.key === sortKey)?.label ?? sortKey)}, {sortDir === "best" ? "best first" : "worst first"}
              {mode !== "off" && arrange === "group" ? " · groups ranked by their best row" : ""} · click a column to sort
            </>
          }
          actions={
            <span className="flex gap-3 text-[11px] text-muted">
              <span className="flex items-center gap-1.5">
                <span className="size-2.5 rounded-sm bg-good/30 border border-good" />
                Best in column
              </span>
              <span className="flex items-center gap-1.5">
                <span className="size-2.5 rounded-sm border border-focus" />
                Inspecting
              </span>
            </span>
          }
        >
          {devices.length > 1 && (
            <div role="status" className="flex flex-wrap items-center gap-2 border border-warn/50 bg-warn/10 rounded-md px-3 py-2 text-xs">
              <I.Info size={14} />
              <span className="flex-1">RTF was measured on {devices.length} kinds of hardware ({devices.join(", ")}). Accuracy compares fairly; speed only compares within one kind.</span>
              <Button size="sm" onClick={() => setHw(devices[0] as "gpu")}>Show {devices[0]} only</Button>
            </div>
          )}
          {!data.models.length ? (
            <Empty>
              No {isAsr ? "ASR" : "VAD / diarisation"} model has run yet. Pick models from the catalog with Run evaluation, or see <Link to="/models">Models</Link>.
            </Empty>
          ) : !cols.length ? (
            <Empty>Select at least one dataset with {taskWord} labels to see scores.</Empty>
          ) : (
            <div className="overflow-x-auto scroll-thin">
              <table className="w-full border-collapse text-[13px]">
                <thead>
                  <tr>
                    <th scope="col" className="text-left px-3 py-2 border-b border-line text-[11px] font-medium text-muted uppercase tracking-wider">
                      {mode === "off" ? "Model" : "Model + enhancement"}
                    </th>
                    {cols.map((c) => (
                      <SortHead key={c.key} label={c.label} sub={c.sub} title={c.title} on={sortKey === c.key} dir={sortDir} onClick={() => setSortKey(c.key)} slice={c.key.startsWith("s")} />
                    ))}
                    {across && <SortHead label="Mean" sub="selected datasets" on={sortKey === "mean"} dir={sortDir} onClick={() => setSortKey("mean")} title="Mean across the selected datasets; blank if one is missing" />}
                    <SortHead label="RTF" sub={mode !== "off" ? "enhancer + model" : "↓ lower"} on={sortKey === "rtf"} dir={sortDir} onClick={() => setSortKey("rtf")} />
                    {isAsr && (
                      <th scope="col" className="text-left px-3 py-2 border-b border-line text-[11px] font-medium text-muted uppercase tracking-wider">
                        Ran on
                      </th>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {rows.map(({ v, child }) => {
                    const r = rtfOf(v);
                    const mv = mean(v);
                    const hwc = data.cells.find((c) => c.model_id === v.modelId && c.hardware?.[v.catalogId]);
                    const h = hwc?.hardware?.[v.catalogId];
                    return (
                      <tr key={v.key} className={cx(child ? "border-b border-track/60" : "border-b border-track")}>
                        <th scope="row" className={cx("text-left px-3 py-2.5 font-normal", child && "pl-6")}>
                          <span className="flex items-center gap-2">
                            {child && <span aria-hidden className="text-subtle">└</span>}
                            <span className="flex flex-col gap-0.5">
                              <span className="flex flex-wrap items-center gap-1.5">
                                {!child && <span>{v.name}</span>}
                                {v.enh && <EnhChip title={v.enh.generative ? "Generative: can shift timing or invent speech" : undefined}>+ {v.enh.display_name}{v.enh.generative ? " · generative" : ""}</EnhChip>}
                                {mode === "all" && v.enh && bestVariant(v.modelId) === v.key && <span className="text-[10px] text-good">best for this model</span>}
                              </span>
                              <span className="text-[11px] text-subtle num">{v.enh ? (child ? `reads the ${v.enh.display_name} copy` : `${v.sub} · ${v.enh.display_name} copy`) : `${v.sub}${mode !== "off" ? " · original audio" : ""}`}</span>
                            </span>
                          </span>
                        </th>
                        {cols.map((c) => {
                          const got = c.get(v);
                          const isInsp = insp?.v === v.key && insp?.d === c.datasetId && (across ? c.key.startsWith("d") : true);
                          const copy = copyState(c.datasetId, v.enh?.id ?? null);
                          return (
                            <td key={c.key} className="px-1.5 py-1.5 text-right">
                              <CellView
                                got={got}
                                count={c.count}
                                low={c.low}
                                best={got.value !== null && bests[c.key] !== null && got.value === bests[c.key]}
                                inspecting={isInsp}
                                metric={isAsr ? (across ? ASR_METRIC_INFO[metric].label : c.label) : "DER"}
                                aria={`${v.name}${v.enh ? ` + ${v.enh.display_name}` : ""} on ${across ? c.label : one?.name}: ${across ? (got.cell?.metric ?? c.label) : c.label} ${c.count ? got.value : pct(got.value)}`}
                                onInspect={() => setInspect({ v: v.key, d: c.datasetId })}
                                onRun={() => runCell.mutate({ v, d: c.datasetId })}
                                running={runCell.isPending && runCell.variables?.v.key === v.key && runCell.variables?.d === c.datasetId}
                                runLabel={`Run ${v.name}${v.enh ? ` + ${v.enh.display_name}` : ""} on ${data.datasets.find((d) => d.id === c.datasetId)?.name}`}
                                building={copy?.status === "building" && got.value === null ? copy.progress : null}
                                slice={c.key.startsWith("s")}
                              />
                            </td>
                          );
                        })}
                        {across && (
                          <td className="px-3 py-2 text-right">
                            <span className="num">{pct(mv)}</span>
                          </td>
                        )}
                        <td className="px-3 py-2 text-right">
                          <span className="flex flex-col items-end">
                            <span className="num text-xs">{rtf(r)}</span>
                            {isAsr && r !== null && <span className="num text-[10px] text-subtle">{speed(r)}</span>}
                          </span>
                        </td>
                        {isAsr && (
                          <td className="px-3 py-2">
                            <span title={h ? [h.gpu_name, h.vram_gb ? `${h.vram_gb} GB declared VRAM` : null, h.cpu_model].filter(Boolean).join(" · ") : undefined} className={cx("inline-flex border rounded px-2 py-0.5 text-[11px] whitespace-nowrap", devices.length > 1 && h?.device !== devices[0] ? "border-warn/50 text-warn" : "border-line text-muted")}>
                              {hwLabel(h)}
                            </span>
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {hiddenByHw.length > 0 && <span className="text-xs text-subtle">{hiddenByHw.length} model{hiddenByHw.length === 1 ? "" : "s"} hidden because they ran on other hardware: {hiddenByHw.map((m) => m.display_name).join(", ")}</span>}
          <ErrorNote error={runCell.error} />
        </Panel>
        <aside aria-label="Selected result" className="w-full xl:w-[360px] shrink-0 flex flex-col gap-4">
          {ic && iv && ids && ic.value !== null ? (
            <>
              <Inspector tab={tab} cell={ic} variant={iv} dataset={ids.name} normaliser={normaliser} collar={collar} plain={iv.enh ? cellOf(iv.modelId, ids.id, null) : null} />
              {data.enhancers.length > 0 && (
                <EffectGrid
                  data={data}
                  modelId={iv.modelId}
                  name={iv.name}
                  datasets={across ? cols0 : one ? [one] : []}
                  metricLabel={isAsr ? ASR_METRIC_INFO[metric].label : "DER"}
                  low={lowMean}
                  selected={{ e: iv.enh?.id ?? null, d: ids.id }}
                  onPick={(e, d) => {
                    if (mode === "off") setMode("all");
                    else if (mode === "best") setMode("all");
                    setEnhOff(enhOff.filter((x) => x !== e));
                    setInspect({ v: `${iv.modelId}+${e}`, d });
                  }}
                />
              )}
              <Compute cell={ic} variant={iv} enhancers={data.enhancers} />
            </>
          ) : (
            <Panel title="Inspecting">
              <span className="text-[13px] text-muted">Click a score to see its breakdown.</span>
            </Panel>
          )}
          <Panel title="Recent evaluation runs">
            {data.recent_runs.length === 0 && <span className="text-[13px] text-muted">No runs yet.</span>}
            {data.recent_runs.map((r) => (
              <div key={r.id} className="flex items-start gap-2.5 text-[13px]">
                <span className="mt-1.5">
                  <StatusDot state={r.status} />
                </span>
                <span className="flex flex-col">
                  <span>
                    {r.label} · {r.dataset}
                  </span>
                  <span className="text-[11px] text-subtle num">
                    {r.status} · {ago(r.finished_at)}
                  </span>
                </span>
              </div>
            ))}
          </Panel>
        </aside>
      </div>
    </>
  );
}

export function SortHead({ label, sub, title, on, dir, onClick, slice, align = "right" }: { label: string; sub: string; title?: string; on: boolean; dir: "best" | "worst"; onClick: () => void; slice?: boolean; align?: "left" | "right" }) {
  return (
    <th scope="col" aria-sort={on ? (dir === "best" ? "ascending" : "descending") : "none"} className={cx("px-1.5 py-1.5 border-b border-line align-bottom", align === "right" ? "text-right" : "text-left")}>
      <button
        type="button"
        onClick={onClick}
        title={on ? (dir === "best" ? "Best first. Click for worst first." : "Worst first. Click for best first.") : (title ?? `Sort by ${label}`)}
        className={cx("w-full inline-flex flex-col gap-0.5 rounded-md px-2 py-1 cursor-pointer border-0", align === "right" ? "items-end" : "items-start", on ? "bg-focus/10 text-fg" : "bg-transparent text-[#CBD5E1] hover:bg-track")}
      >
        <span className="flex items-center gap-1 text-[12px] font-medium">
          <span className={cx("text-[11px]", on ? "text-focus" : "text-[#475569]")}>{on ? (dir === "best" ? "↑" : "↓") : "↕"}</span>
          {slice && <span className="text-[10px] text-secondary">slice</span>}
          {label}
        </span>
        <span className="num text-[11px] text-subtle font-normal">{sub}</span>
      </button>
    </th>
  );
}

function CellView({
  got,
  count,
  low,
  best,
  inspecting,
  metric,
  aria,
  onInspect,
  onRun,
  running,
  runLabel,
  building,
  slice,
}: {
  got: ReturnType<Col["get"]>;
  count?: boolean;
  low: boolean;
  best: boolean;
  inspecting: boolean;
  metric: string;
  aria: string;
  onInspect: () => void;
  onRun: () => void;
  running: boolean;
  runLabel: string;
  building: number | null;
  slice?: boolean;
}) {
  const c = got.cell;
  if (got.value === null || got.value === undefined) {
    if (slice) return <span className="text-subtle px-2">—</span>;
    const busy = c?.status === "queued" || c?.status === "waiting" || c?.status === "running" || running;
    return (
      <span className="inline-flex items-center justify-end gap-2 min-h-9 px-2">
        {c?.status === "failed" || c?.status === "blocked" ? (
          <span className="text-[11px] text-bad" title={c.error ?? undefined}>
            {c.status}
          </span>
        ) : (
          <span className="text-subtle">—</span>
        )}
        {building !== null ? (
          <span className="text-[11px] text-enh-soft">building copy · {Math.round(building * 100)}%</span>
        ) : busy ? (
          <span className="text-[11px] text-muted inline-flex items-center gap-1.5">
            <StatusDot state={c?.status ?? "queued"} />
            {c?.status === "waiting" ? "waiting for compute" : (c?.status ?? "queued")}
          </span>
        ) : (
          <Button size="sm" onClick={onRun} aria-label={runLabel}>
            Run
          </Button>
        )}
      </span>
    );
  }
  const d = got.delta;
  const better = d !== null && Math.abs(d) > 1e-9 ? (low ? d < 0 : d > 0) : null;
  const fmt = count ? `${d !== null && d > 0 ? "+" : d !== null && d < 0 ? "−" : "±"}${num(Math.abs(d ?? 0))}` : got.deltaVsOriginal ? deltaPts(d) : `${better ? "↓" : better === false ? "↑" : ""}${deltaPts(d).replace(/^[+−]/, "")}`;
  return (
    <button
      type="button"
      onClick={onInspect}
      aria-pressed={inspecting}
      aria-label={aria}
      className={cx(
        "w-full inline-flex flex-col items-end gap-0.5 rounded-md px-2.5 py-1.5 cursor-pointer border",
        best ? "bg-good/15 border-good/50" : "bg-transparent border-transparent hover:bg-track",
        inspecting && "!border-focus",
      )}
    >
      <span className="flex items-baseline gap-1.5">
        <span className="num text-[14px] text-fg">{count ? num(got.value) : pct(got.value)}</span>
        {d !== null && <span className={cx("num text-[11px]", better === null ? "text-muted" : better ? "text-good" : "text-bad")}>{fmt}</span>}
      </span>
      {c?.metric && c.metric !== metric && !count && <span className="text-[10px] text-subtle">{c.metric}</span>}
      {c?.rerunning && <span className="text-[10px] text-primary">re-running…</span>}
    </button>
  );
}

const PART_COLORS: Record<string, string> = {
  h: "var(--color-line)",
  s: "var(--color-sub)",
  d: "var(--color-del)",
  i: "var(--color-ins)",
  missed: ERROR_COLORS.missed,
  false_alarm: ERROR_COLORS.false_alarm,
  confusion: ERROR_COLORS.confusion,
  vad_missed: ERROR_COLORS.missed,
  vad_false_alarm: ERROR_COLORS.false_alarm,
};

function Inspector({ tab, cell, variant, dataset, normaliser, collar, plain }: { tab: string; cell: BenchCell; variant: Variant; dataset: string; normaliser: string; collar: number; plain: BenchCell | null }) {
  const href = tab === "asr" ? `/errors/asr/${cell.run_id}${qs({ normaliser })}` : `/errors/diar/${cell.run_id}${qs({ collar })}`;
  const m = cell.metrics;
  const b = cell.breakdown;
  const parts =
    tab === "asr" && m
      ? [
          { key: "h", label: "Hits", count: m.h ?? 0 },
          { key: "s", label: "Substitutions", count: m.s ?? 0 },
          { key: "d", label: "Deletions", count: m.d ?? 0 },
          { key: "i", label: "Insertions", count: m.i ?? 0 },
        ]
      : (b?.parts ?? []).map((p) => ({ key: p.key, label: p.label, count: p.count ?? p.seconds ?? 0, seconds: p.seconds, pct: p.pct }));
  const total = parts.reduce((a, p) => a + (p.count ?? 0), 0) || 1;
  return (
    <Panel>
      <div className="flex flex-col gap-1">
        <span className="text-[11px] text-muted uppercase tracking-wider">Inspecting</span>
        <span className="font-medium flex flex-wrap items-center gap-1.5">
          {variant.name}
          {variant.enh && <EnhChip>+ {variant.enh.display_name}</EnhChip>}
        </span>
        <span className="text-xs text-muted">{dataset}</span>
        {variant.enh && plain?.value !== null && plain?.value !== undefined && cell.value !== null && (
          <span className={cx("num text-xs", (cell.value ?? 0) < plain.value ? "text-good" : (cell.value ?? 0) > plain.value ? "text-bad" : "text-muted")}>
            {cell.metric} {pct(plain.value)} on original audio → {pct(cell.value)} with {variant.enh.display_name} ({deltaPts((cell.value ?? 0) - plain.value)} pts)
          </span>
        )}
      </div>
      {tab === "asr" && m ? (
        <div className="grid grid-cols-3 gap-2">
          {(["wer", "cer", "mer", "wil", "wip"] as const).map((k) => (
            <div key={k} title={ASR_METRIC_INFO[k].name} className="bg-canvas border border-track rounded-md px-3 py-2 flex flex-col gap-0.5">
              <span className="text-[11px] text-muted">{ASR_METRIC_INFO[k].label}</span>
              <span className="num text-[16px]">{pct(m[k])}</span>
              <span className="text-[10px] text-subtle">{ASR_METRIC_INFO[k].low ? "lower is better" : "higher is better"}</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="flex items-baseline gap-2">
          <span className="num text-[30px] font-medium">{pct(cell.value)}</span>
          <span className="text-xs text-muted">{cell.metric}</span>
        </div>
      )}
      <div className="flex h-2 rounded overflow-hidden bg-track" role="img" aria-label={parts.map((p) => `${p.label} ${p.count}`).join(", ")}>
        {parts.map((p) => (
          <span key={p.key} style={{ width: `${((p.count ?? 0) / total) * 100}%`, background: PART_COLORS[p.key] }} />
        ))}
      </div>
      <div className="flex flex-col gap-1.5">
        {parts.map((p) => (
          <div key={p.key} className="flex items-center justify-between text-[13px]">
            <span className="flex items-center gap-2 text-muted">
              <span className="size-2.5 rounded-sm" style={{ background: PART_COLORS[p.key] }} />
              {p.label}
            </span>
            <span className="num">
              {"seconds" in p && p.seconds !== undefined ? `${(p.seconds ?? 0).toFixed(1)} s` : num(p.count)}
              {"pct" in p && p.pct !== undefined ? <span className="text-subtle"> {pct(p.pct)}</span> : null}
            </span>
          </div>
        ))}
        {tab === "asr" && m && (
          <span className="text-[11px] text-subtle">
            Bar shows every aligned word: hits, then each error type. N = {num(m.n)} reference words, P = {num(m.p)} hypothesis words.
          </span>
        )}
      </div>
      {tab !== "asr" && (
        <div className="flex flex-col">
          {Object.entries(b?.facts ?? {}).map(([k, v]) => (
            <Fact key={k} k={k} v={typeof v === "number" && v <= 1 && (k.includes("JER") || k.includes("right")) ? pct(v) : v === null ? "—" : typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(1)) : v} />
          ))}
        </div>
      )}
      {cell.run_id && (
        <span className="flex flex-wrap gap-4">
          <Link to={href} className="inline-flex items-center gap-1.5 text-[13px]">
            Open error analysis <I.Arrow size={14} />
          </Link>
          {tab === "asr" && (
            <Link to={`/errors/asr/${cell.run_id}/labels${qs({ normaliser })}`} className="inline-flex items-center gap-1.5 text-[13px]">
              Break down by label <I.Arrow size={14} />
            </Link>
          )}
        </span>
      )}
    </Panel>
  );
}

function EffectGrid({
  data,
  modelId,
  name,
  datasets,
  metricLabel,
  low,
  selected,
  onPick,
}: {
  data: BenchTable;
  modelId: number;
  name: string;
  datasets: { id: number; name: string }[];
  metricLabel: string;
  low: boolean;
  selected: { e: number | null; d: number };
  onPick: (e: number, d: number) => void;
}) {
  const val = (e: number | null, d: number) => data.cells.find((c) => c.model_id === modelId && c.dataset_id === d && c.enh_model_id === e)?.value ?? null;
  const helps: string[] = [];
  const hurts: string[] = [];
  const same: string[] = [];
  for (const d of datasets) {
    const base = val(null, d.id);
    if (base === null) continue;
    let best: { e: Enhancer; v: number } | null = null;
    for (const e of data.enhancers) {
      const v = val(e.id, d.id);
      if (v !== null && (!best || (low ? v < best.v : v > best.v))) best = { e, v };
    }
    if (!best) continue;
    const dd = best.v - base;
    if (low ? dd < -1e-9 : dd > 1e-9) helps.push(`${d.name} (${best.e.display_name}, ${deltaPts(dd)})`);
    else if (Math.abs(dd) < 1e-9) same.push(d.name);
    else hurts.push(d.name);
  }
  return (
    <Panel title="Enhancement effect" subtitle={`${name} · change in ${metricLabel} against original audio · click a cell to inspect it`}>
      <div className="overflow-x-auto scroll-thin">
        <div className="grid gap-1" style={{ gridTemplateColumns: `minmax(110px, 1.2fr) repeat(${datasets.length}, minmax(64px, 1fr))`, minWidth: 110 + datasets.length * 70 }}>
          <span />
          {datasets.map((d) => (
            <span key={d.id} className="text-[11px] text-muted text-right truncate px-1" title={d.name}>
              {d.name}
            </span>
          ))}
          {data.enhancers.map((e) => (
            <EffectRow key={e.id} e={e} datasets={datasets} val={val} low={low} selected={selected} onPick={onPick} />
          ))}
        </div>
      </div>
      {(helps.length > 0 || hurts.length > 0 || same.length > 0) && (
        <span className="text-xs text-muted leading-relaxed">
          {helps.length ? `Helps on ${helps.join("; ")}. ` : "No enhancer helps this model on these datasets. "}
          {hurts.length ? `Every enhancer makes ${hurts.join(", ")} worse, so leave enhancement off there. ` : ""}
          {same.length ? `No change on ${same.join(", ")}.` : ""}
        </span>
      )}
      <Link to={`/errors/enh`} className="inline-flex items-center gap-1.5 text-[13px]">
        Enhancement error analysis <I.Arrow size={14} />
      </Link>
    </Panel>
  );
}

function EffectRow({ e, datasets, val, low, selected, onPick }: { e: Enhancer; datasets: { id: number; name: string }[]; val: (e: number | null, d: number) => number | null; low: boolean; selected: { e: number | null; d: number }; onPick: (e: number, d: number) => void }) {
  return (
    <>
      <span className="text-xs truncate self-center" title={e.display_name}>
        {e.display_name}
      </span>
      {datasets.map((d) => {
        const v = val(e.id, d.id);
        const b = val(null, d.id);
        if (v === null || b === null)
          return (
            <span key={d.id} className="text-[11px] text-subtle text-right px-2 py-1.5 rounded bg-track/40">
              {v === null && b !== null ? "…" : "—"}
            </span>
          );
        const dd = v - b;
        const good = low ? dd < 0 : dd > 0;
        const rel = Math.min(1, Math.abs(dd) / 0.02);
        const bg = Math.abs(dd) < 1e-9 ? "rgba(30,41,59,0.6)" : `${good ? "rgba(16,185,129," : "rgba(244,63,94,"}${(0.12 + rel * 0.5).toFixed(2)})`;
        const sel = selected.e === e.id && selected.d === d.id;
        return (
          <button key={d.id} type="button" onClick={() => onPick(e.id, d.id)} aria-label={`${e.display_name} on ${d.name}: ${deltaPts(dd)}`} className={cx("num text-[12px] text-right px-2 py-1.5 rounded cursor-pointer border", sel ? "border-focus" : "border-transparent")} style={{ background: bg }}>
            {deltaPts(dd)}
          </button>
        );
      })}
    </>
  );
}

function hwFacts(h: Hardware | undefined): [string, string][] {
  if (!h) return [];
  const out: [string, string][] = [];
  if (h.device === "hosted") return [["Where", h.hosted_at ? `hosted at ${h.hosted_at}` : "hosted"]];
  if (h.device === "gpu") out.push(["GPU", `${h.gpu_count ?? 1} × ${h.gpu_name ?? "GPU"}${h.gpu_total_gb ? ` (${h.gpu_total_gb} GB)` : ""}`]);
  else out.push(["Device", "CPU"]);
  if (h.peak_vram_gb !== undefined) out.push(["Peak VRAM", `${h.peak_vram_gb.toFixed(1)} GB`]);
  else if (h.vram_gb !== undefined) out.push(["VRAM", `${h.vram_gb} GB declared`]);
  if (h.precision) out.push(["Precision", h.precision]);
  if (h.batch_size) out.push(["Batch size", String(h.batch_size)]);
  if (h.cpu_model) out.push(["CPU", `${h.cpu_model}${h.cpu_cores ? ` · ${h.cpu_cores} cores` : ""}`]);
  if (h.driver_version || h.cuda_version) out.push(["Runtime", [h.cuda_version ? `CUDA ${h.cuda_version}` : null, h.driver_version ? `driver ${h.driver_version}` : null].filter(Boolean).join(" · ")]);
  if (h.hostname) out.push(["Node", h.hostname]);
  if (h.engine) out.push(["Engine", h.engine]);
  if (h.reused) out.push(["Outputs", "reused from earlier runs"]);
  return out;
}

function Compute({ cell, variant, enhancers }: { cell: BenchCell; variant: Variant; enhancers: Enhancer[] }) {
  const h = cell.hardware?.[variant.catalogId];
  const enhRtf = cell.rtf_by_stage?.enh ?? null;
  const total = cell.rtf !== null && cell.rtf !== undefined ? cell.rtf + (enhRtf ?? 0) : null;
  const enh = variant.enh ? enhancers.find((e) => e.id === variant.enh!.id) : null;
  const eh = enh ? cell.hardware?.[enh.catalog_id] : undefined;
  return (
    <Panel title="Compute" actions={<span className="text-[11px] text-muted">{hwLabel(h)}</span>}>
      <div className="grid grid-cols-2 gap-2">
        <div className="bg-canvas border border-track rounded-md px-3 py-2 flex flex-col gap-0.5">
          <span className="text-[11px] text-muted">RTF</span>
          <span className="num text-[16px]">{rtf(total)}</span>
          <span className="text-[10px] text-subtle">compute time ÷ audio time</span>
        </div>
        <div className="bg-canvas border border-track rounded-md px-3 py-2 flex flex-col gap-0.5">
          <span className="text-[11px] text-muted">Speed</span>
          <span className="num text-[16px]">{total ? `${(1 / total).toFixed(0)}×` : "—"}</span>
          <span className="text-[10px] text-subtle">hours of audio per hour</span>
        </div>
      </div>
      <div className="flex flex-col">
        {variant.enh && <Fact k="Enhancer" v={`${variant.enh.display_name} · RTF ${rtf(enhRtf)}${eh?.device ? ` · ${hwLabel(eh)}` : ""}`} />}
        {hwFacts(h).map(([k, v]) => (
          <Fact key={k} k={k} v={<span className="text-[12px]">{v}</span>} />
        ))}
        {cell.image && <Fact k="Image" v={<span className="text-[11px] break-all">{cell.image}</span>} />}
        <Fact k="Wall time" v={cell.wall_s !== null && cell.wall_s !== undefined ? `${duration(cell.wall_s)}${cell.audio_s ? ` for ${duration(cell.audio_s)} of audio` : ""}` : "—"} />
        <Fact k="Scored" v={ago(cell.finished_at)} />
      </div>
      <span className="text-[11px] text-subtle leading-relaxed">
        RTF leaves out the model load (cold start) and queueing; it counts only the time spent on the audio{variant.enh ? ", plus enhancement, which runs once per dataset and is shared by every model" : ""}. Fields the model catalog does not report are not shown.
      </span>
    </Panel>
  );
}
