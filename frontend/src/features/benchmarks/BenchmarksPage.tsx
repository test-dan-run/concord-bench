import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, qs, type BenchCell, type BenchTable, type Dataset } from "../../lib/api";
import { useConfig, useNormaliser, useNormalisers } from "../../lib/config";
import { ERROR_COLORS } from "../../lib/colors";
import { ago, deltaPts, pct, rtf } from "../../lib/format";
import * as I from "../../components/icons";
import { Button, cx, Empty, ErrorNote, Fact, LinkButton, PageHeader, Panel, Select, Spinner, StatusDot } from "../../components/ui";
import { DatasetPicker } from "./DatasetPicker";
import { PipelinesTab } from "./PipelinesTab";
import { EMPTY_PLAN, PlanPanel, planCount, type PlanValue } from "./PlanPanel";

const SEL_KEY = "concord.benchmarks.datasets";

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

export function BenchmarksPage() {
  const { tab = "asr" } = useParams();
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const { data: cfg } = useConfig();
  const { data: norms } = useNormalisers();
  const [normaliser, setNormaliser] = useNormaliser();
  const collar = Number(params.get("collar") ?? cfg?.default_collar ?? 0.25);
  const [showPlan, setShowPlan] = useState(false);
  const tasks = (cfg?.tasks ?? []).filter((t) => t.enabled);
  const setParam = (k: string, v: string) => {
    const p = new URLSearchParams(params);
    p.set(k, v);
    setParams(p, { replace: true });
  };

  return (
    <>
      <PageHeader
        crumbs="Evaluate"
        title="Benchmarks"
        subtitle="Every model you have tried, scored on the datasets you uploaded."
        actions={
          <>
            <LinkButton to="/datasets/upload">
              <I.Upload /> Upload dataset
            </LinkButton>
            <LinkButton to="/models">
              <I.Cpu /> Models
            </LinkButton>
            <Button variant="primary" onClick={() => setShowPlan((s) => !s)} aria-expanded={showPlan}>
              <I.Play size={12} /> Run evaluation
            </Button>
          </>
        }
      />
      {showPlan && <RunEvaluation onClose={() => setShowPlan(false)} />}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-track">
        <div role="tablist" aria-label="Speech task" className="flex gap-1">
          {tasks.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              onClick={() => nav(`/benchmarks/${t.id}${params.toString() ? `?${params}` : ""}`)}
              className={cx(
                "bg-transparent border-0 border-b-2 px-3 pb-2.5 pt-1 text-[13px] cursor-pointer",
                tab === t.id ? "border-primary text-fg font-medium" : "border-transparent text-muted hover:text-fg",
              )}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap gap-3 pb-2">
          {(tab === "asr" || tab === "pipeline") && (
            <Select label="Text normaliser" value={normaliser} onChange={(e) => setNormaliser(e.target.value)}>
              {(norms ?? []).map((n) => (
                <option key={n.id} value={n.id} title={n.description}>
                  {n.label}
                </option>
              ))}
            </Select>
          )}
          {(tab === "diar" || tab === "pipeline") && (
            <Select label="Collar" value={String(collar)} onChange={(e) => setParam("collar", e.target.value)}>
              {(cfg?.collars ?? [0, 0.25]).map((c) => (
                <option key={c} value={c}>
                  ±{c} s
                </option>
              ))}
            </Select>
          )}
        </div>
      </div>
      {tab === "pipeline" ? <PipelinesTab normaliser={normaliser} collar={collar} /> : <Leaderboard tab={tab} normaliser={normaliser} collar={collar} />}
    </>
  );
}

function RunEvaluation({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const { data: datasets } = useQuery({ queryKey: ["datasets"], queryFn: () => api.get<Dataset[]>("/datasets") });
  const ready = (datasets ?? []).filter((d) => d.status === "ready" && d.label_types.length);
  const [dsId, setDsId] = useState<number | null>(null);
  const ds = ready.find((d) => d.id === dsId) ?? ready[0];
  const [plan, setPlan] = useState<PlanValue>(EMPTY_PLAN);
  const run = useMutation({
    mutationFn: () => api.post<{ queued: number }>("/benchmarks/plan", { dataset_id: ds!.id, ...plan }),
    onSuccess: () => {
      void qc.invalidateQueries();
      onClose();
    },
  });
  if (!datasets) return <Spinner />;
  if (!ds) return <Empty>Upload a labelled dataset first.</Empty>;
  const n = planCount(plan, ds.label_types);
  return (
    <Panel title="Run evaluation" subtitle="Pick a dataset and catalog models to run on it. The model catalog starts each model when its turn comes." actions={<Button size="sm" variant="ghost" onClick={onClose}>Close</Button>}>
      <Select label="Dataset" value={ds.id} onChange={(e) => setDsId(Number(e.target.value))}>
        {ready.map((d) => (
          <option key={d.id} value={d.id}>
            {d.name}
          </option>
        ))}
      </Select>
      <PlanPanel key={ds.id} datasetId={ds.id} value={plan} onChange={setPlan} />
      <ErrorNote error={run.error} />
      <div>
        <Button variant="primary" disabled={!n || run.isPending} onClick={() => run.mutate()}>
          Run {n} {plan.mode === "grid" ? `pipeline${n === 1 ? "" : "s"}` : `run${n === 1 ? "" : "s"}`}
        </Button>
      </div>
    </Panel>
  );
}

function Leaderboard({ tab, normaliser, collar }: { tab: string; normaliser: string; collar: number }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["bench", tab, normaliser, collar],
    queryFn: () => api.get<BenchTable>(`/benchmarks/${tab}${qs({ normaliser, collar })}`),
    refetchInterval: (query) =>
      query.state.data?.cells.some((c) => c.status === "queued" || c.status === "waiting" || c.status === "running" || c.rerunning || c.pending_score_job) ? 3000 : false,
  });
  const [selected, setSelected] = useState<number[] | null>(loadSel);
  const [inspect, setInspect] = useState<{ m: number; d: number } | null>(null);
  const data = q.data;
  const sel = useMemo(() => selected ?? data?.datasets.filter((d) => d.has_labels).map((d) => d.id) ?? [], [selected, data]);
  useEffect(() => {
    if (selected) saveSel(selected);
  }, [selected]);

  const runCell = useMutation({
    mutationFn: (c: BenchCell) => api.post("/runs", { task: tab === "asr" ? "asr" : "seg", model_id: c.model_id, dataset_id: c.dataset_id }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["bench"] }),
  });

  if (q.isLoading) return <Spinner />;
  if (q.error || !data) return <ErrorNote error={q.error} />;
  const taskWord = tab === "asr" ? "transcript" : "speaker";
  const cols = data.datasets.filter((d) => sel.includes(d.id) && d.has_labels);
  const cell = (m: number, d: number) => data.cells.find((c) => c.model_id === m && c.dataset_id === d)!;
  const best: Record<number, number | null> = {};
  for (const d of cols) {
    const vals = data.models.map((m) => cell(m.id, d.id)?.value).filter((v): v is number => v !== null && v !== undefined);
    best[d.id] = vals.length > 1 ? Math.min(...vals) : null;
  }
  const firstWithValue = data.cells.find((c) => c.value !== null && cols.some((d) => d.id === c.dataset_id));
  const insp = inspect ?? (firstWithValue ? { m: firstWithValue.model_id, d: firstWithValue.dataset_id } : null);
  const ic = insp ? cell(insp.m, insp.d) : null;
  const im = insp ? data.models.find((m) => m.id === insp.m) : null;
  const ids = data.datasets.find((d) => d.id === insp?.d);
  const metricName = tab === "asr" ? "WER" : "DER";

  return (
    <>
      <DatasetPicker datasets={data.datasets} selected={sel} onChange={setSelected} taskWord={taskWord} />
      <div className="flex flex-col xl:flex-row gap-5 items-start">
        <Panel
          className="flex-1 w-full"
          title={tab === "asr" ? "Word error rate" : "Diarisation error rate"}
          subtitle={tab === "asr" ? `Lower is better · normaliser ${data.normaliser}` : `Lower is better · collar ±${data.collar} s · speech-only models show VAD error (missed + false alarm)`}
          actions={
            <span className="flex gap-3 text-[11px] text-muted">
              <span className="flex items-center gap-1.5">
                <span className="size-2.5 rounded-sm bg-good/30 border border-good" />
                Best per dataset
              </span>
              <span className="flex items-center gap-1.5">
                <span className="size-2.5 rounded-sm border border-focus" />
                Inspecting
              </span>
              <span>Δ vs previous run</span>
            </span>
          }
        >
          {!data.models.length ? (
            <Empty>
              No {tab === "asr" ? "ASR" : "VAD / diarisation"} model has run yet. Pick models from the catalog with Run evaluation, or see <Link to="/models">Models</Link>.
            </Empty>
          ) : !cols.length ? (
            <Empty>Select at least one dataset with {taskWord} labels to see scores.</Empty>
          ) : (
            <div className="overflow-x-auto scroll-thin">
              <table className="w-full border-collapse text-[13px]">
                <thead>
                  <tr>
                    <th scope="col" className="text-left px-3 py-2 border-b border-line text-[11px] font-medium text-muted uppercase tracking-wider">
                      Model
                    </th>
                    {cols.map((d) => (
                      <th key={d.id} scope="col" className="text-right px-3 py-2 border-b border-line align-bottom">
                        <span className="text-[12px] font-medium text-fg">{d.name}</span>
                        <br />
                        <span className="num text-[11px] text-subtle font-normal">
                          {d.files ?? 0} files · {d.hours ? `${(d.hours * 60).toFixed(1)} min` : "—"}
                        </span>
                      </th>
                    ))}
                    <th scope="col" className="text-right px-3 py-2 border-b border-line text-[11px] font-medium text-muted uppercase tracking-wider">
                      RTF
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {data.models.map((m) => {
                    const rtfs = cols.map((d) => cell(m.id, d.id)?.rtf).filter((v): v is number => v !== null && v !== undefined);
                    return (
                      <tr key={m.id} className="border-b border-track">
                        <th scope="row" className="text-left px-3 py-2.5 font-normal">
                          <span className="flex flex-col gap-0.5">
                            <span>{m.display_name}</span>
                            <span className="text-[11px] text-subtle num">
                              {m.kind === "vad" ? "speech only" : m.kind === "diar" ? "speakers" : m.timing_level ? `${m.timing_level} times` : ""}
                            </span>
                          </span>
                        </th>
                        {cols.map((d) => {
                          const c = cell(m.id, d.id);
                          const isBest = c.value !== null && best[d.id] !== null && c.value === best[d.id];
                          const isInsp = insp?.m === m.id && insp?.d === d.id;
                          return (
                            <td key={d.id} className="px-1.5 py-1.5 text-right">
                              <CellView
                                c={c}
                                best={isBest}
                                inspecting={isInsp}
                                onInspect={() => setInspect({ m: m.id, d: d.id })}
                                onRun={() => runCell.mutate(c)}
                                running={runCell.isPending && runCell.variables === c}
                                metric={metricName}
                                modelName={m.display_name}
                                datasetName={d.name}
                              />
                            </td>
                          );
                        })}
                        <td className="px-3 py-2 text-right num text-xs text-muted">{rtfs.length ? rtf(rtfs.reduce((a, b) => a + b, 0) / rtfs.length) : "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <ErrorNote error={runCell.error} />
        </Panel>
        <aside aria-label="Selected result" className="w-full xl:w-[340px] shrink-0 flex flex-col gap-4">
          {ic && im && ids && ic.value !== null ? (
            <Inspector tab={tab} cell={ic} model={im.display_name} dataset={ids.name} normaliser={normaliser} collar={collar} />
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

function CellView({
  c,
  best,
  inspecting,
  onInspect,
  onRun,
  running,
  metric,
  modelName,
  datasetName,
}: {
  c: BenchCell;
  best: boolean;
  inspecting: boolean;
  onInspect: () => void;
  onRun: () => void;
  running: boolean;
  metric: string;
  modelName: string;
  datasetName: string;
}) {
  if (c.value === null || c.value === undefined) {
    const busy = c.status === "queued" || c.status === "waiting" || c.status === "running" || running;
    return (
      <span className="inline-flex items-center justify-end gap-2 min-h-9 px-2">
        {c.status === "failed" || c.status === "blocked" ? (
          <span className="text-[11px] text-bad" title={c.error ?? undefined}>
            {c.status}
          </span>
        ) : (
          <span className="text-subtle">—</span>
        )}
        {busy ? (
          <span className="text-[11px] text-muted inline-flex items-center gap-1.5">
            <StatusDot state={c.status ?? "queued"} />
            {c.status === "waiting" ? "waiting for compute" : (c.status ?? "queued")}
          </span>
        ) : (
          <Button size="sm" onClick={onRun} aria-label={`Run ${modelName} on ${datasetName}`}>
            Run
          </Button>
        )}
      </span>
    );
  }
  const better = c.delta !== null && c.delta !== undefined ? c.delta < 0 : null;
  return (
    <button
      type="button"
      onClick={onInspect}
      aria-pressed={inspecting}
      aria-label={`${modelName} on ${datasetName}: ${c.metric ?? metric} ${pct(c.value)}`}
      className={cx(
        "w-full inline-flex flex-col items-end gap-0.5 rounded-md px-2.5 py-1.5 cursor-pointer border",
        best ? "bg-good/15 border-good/50" : "bg-transparent border-transparent hover:bg-track",
        inspecting && "!border-focus",
      )}
    >
      <span className="flex items-baseline gap-1.5">
        <span className="num text-[14px] text-fg">{pct(c.value)}</span>
        {c.delta !== null && c.delta !== undefined && (
          <span className={cx("num text-[11px]", better === null ? "text-muted" : better ? "text-good" : "text-bad")}>
            {better ? "↓" : better === false ? "↑" : ""}
            {deltaPts(c.delta).replace(/^[+−]/, "")}
          </span>
        )}
      </span>
      {c.metric && c.metric !== metric && <span className="text-[10px] text-subtle">{c.metric}</span>}
      {c.rerunning && <span className="text-[10px] text-primary">re-running…</span>}
    </button>
  );
}

const PART_COLORS: Record<string, string> = {
  s: "var(--color-sub)",
  d: "var(--color-del)",
  i: "var(--color-ins)",
  missed: ERROR_COLORS.missed,
  false_alarm: ERROR_COLORS.false_alarm,
  confusion: ERROR_COLORS.confusion,
  vad_missed: ERROR_COLORS.missed,
  vad_false_alarm: ERROR_COLORS.false_alarm,
};

function Inspector({ tab, cell, model, dataset, normaliser, collar }: { tab: string; cell: BenchCell; model: string; dataset: string; normaliser: string; collar: number }) {
  const b = cell.breakdown;
  const parts = b?.parts ?? [];
  const total = parts.reduce((a, p) => a + (p.count ?? p.seconds ?? 0), 0) || 1;
  const href =
    tab === "asr" ? `/errors/asr/${cell.run_id}${qs({ normaliser })}` : `/errors/diar/${cell.run_id}${qs({ collar })}`;
  return (
    <Panel>
      <div className="flex flex-col gap-1">
        <span className="text-[11px] text-muted uppercase tracking-wider">Inspecting</span>
        <span className="font-medium">{model}</span>
        <span className="text-xs text-muted">{dataset}</span>
      </div>
      <div className="flex items-baseline gap-2">
        <span className="num text-[30px] font-medium">{pct(cell.value)}</span>
        <span className="text-xs text-muted">{cell.metric}</span>
      </div>
      <div className="flex h-2 rounded overflow-hidden bg-track" role="img" aria-label="Error breakdown">
        {parts.map((p) => (
          <span key={p.key} style={{ width: `${((p.count ?? p.seconds ?? 0) / total) * 100}%`, background: PART_COLORS[p.key] }} />
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
              {p.count !== undefined ? p.count : `${(p.seconds ?? 0).toFixed(1)} s`} <span className="text-subtle">{pct(p.pct)}</span>
            </span>
          </div>
        ))}
      </div>
      <div className="flex flex-col">
        {Object.entries(b?.facts ?? {}).map(([k, v]) => (
          <Fact key={k} k={k} v={typeof v === "number" && v <= 1 && (k.includes("CER") || k.includes("JER") || k.includes("right")) ? pct(v) : v === null ? "—" : typeof v === "number" ? Number.isInteger(v) ? v : v.toFixed(1) : v} />
        ))}
        <Fact k="RTF" v={rtf(cell.rtf)} />
        <Fact k="Scored" v={ago(cell.finished_at)} />
        {cell.image && <Fact k="Image" v={<span className="text-[11px] break-all">{cell.image}</span>} />}
      </div>
      {cell.run_id && (
        <Link to={href} className="inline-flex items-center gap-1.5 text-[13px]">
          Open error analysis <I.Arrow size={14} />
        </Link>
      )}
    </Panel>
  );
}
