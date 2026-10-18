import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, qs, type LabelPreview, type LabelResults, type LabelSummary, type Model, type Run, type Job } from "../../lib/api";
import { clock, num, pct, timingLabel } from "../../lib/format";
import { confidenceColor, labelButton, STATUS_LABELS } from "../../lib/label";
import { C } from "../../lib/colors";
import * as I from "../../components/icons";
import { Bar, Button, Chip, Empty, ErrorNote, Fact, JobPill, PageHeader, Panel, Select, Spinner, cx } from "../../components/ui";
import { BarLegend, Crumbs, DatasetChooser, LabelTabs, StatusBar } from "./common";

const STRATEGIES = [
  { value: "best_aligned", label: "Best-aligned single hypothesis", hint: "The member hypothesis with the lowest summed WER to the others. Default." },
  { value: "rover", label: "ROVER fusion", hint: "Word-level vote over word-timed members; segments with fewer than two fall back to best-aligned." },
  { value: "single", label: "One model", hint: "One member's text; the others still measure agreement." },
];

const STATE_TONE = { saved: "good", partial: "warn", new: "default" } as const;
const STATE_TEXT = { saved: "saved outputs", partial: "partly saved", new: "needs inference" };

export function PseudoLabelPage() {
  const { datasetId } = useParams();
  if (!datasetId) {
    return <DatasetChooser title="Pseudo-label" to={(id) => `/label/pseudo/${id}`} subtitle="Label audio with an ensemble of models, then review what they disagree on." />;
  }
  return <PseudoLabel datasetId={Number(datasetId)} />;
}

function PseudoLabel({ datasetId }: { datasetId: number }) {
  const qc = useQueryClient();
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.get<Model[]>("/models") });
  const results = useQuery({
    queryKey: ["label-results", datasetId],
    queryFn: () => api.get<LabelResults>(`/label/datasets/${datasetId}/results`),
    refetchInterval: (q) => (q.state.data?.job && ["queued", "running"].includes(q.state.data.job.status) ? 1500 : false),
  });
  const segModels = useMemo(() => (models.data ?? []).filter((m) => m.stages.includes("seg")), [models.data]);
  const asrModels = useMemo(() => (models.data ?? []).filter((m) => m.stages.includes("asr")), [models.data]);

  const [tasks, setTasks] = useState<Set<string>>(new Set(["asr", "diar"]));
  const [segId, setSegId] = useState<number | null>(null);
  const [asrIds, setAsrIds] = useState<Set<number>>(new Set());
  const [strategy, setStrategy] = useState("best_aligned");
  const [singleId, setSingleId] = useState<number | null>(null);
  const [high, setHigh] = useState(0.85);
  const [low, setLow] = useState(0.5);

  // Defaults once models load: the first diariser, every ASR model; the last run's settings when there is one.
  const lastParams = results.data?.run?.params as Record<string, unknown> | undefined;
  useEffect(() => {
    if (!models.data || !results.data || segId !== null) return;
    if (lastParams?.seg_model_id) {
      setSegId(lastParams.seg_model_id as number);
      setAsrIds(new Set((lastParams.asr_model_ids as number[]) ?? []));
      setTasks(new Set((lastParams.tasks as string[]) ?? ["asr", "diar"]));
      setStrategy((lastParams.strategy as string) ?? "best_aligned");
      setSingleId((lastParams.single_model_id as number | null) ?? null);
      setHigh((lastParams.high as number) ?? 0.85);
      setLow((lastParams.low as number) ?? 0.5);
      return;
    }
    const diar = segModels.find((m) => m.capabilities.includes("diar")) ?? segModels[0];
    if (diar) setSegId(diar.id);
    setAsrIds(new Set(asrModels.map((m) => m.id)));
  }, [models.data, results.data, segModels, asrModels, segId, lastParams]);

  const seg = segModels.find((m) => m.id === segId);
  const wantAsr = tasks.has("asr");
  const ids = [...asrIds].sort((a, b) => a - b);
  const preview = useQuery({
    queryKey: ["label-preview", datasetId, segId, ids.join(",")],
    queryFn: () => api.get<LabelPreview>(`/label/datasets/${datasetId}/preview${qs({ seg_model_id: segId, asr_model_ids: ids.join(",") })}`),
    enabled: segId !== null,
  });
  const start = useMutation({
    mutationFn: () =>
      api.post<{ run: Run; job: Job }>(`/label/datasets/${datasetId}/runs`, {
        tasks: [...tasks],
        seg_model_id: segId,
        asr_model_ids: wantAsr ? ids : [],
        strategy,
        single_model_id: strategy === "single" ? singleId : null,
        high,
        low,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["label-results", datasetId] });
      void qc.invalidateQueries({ queryKey: ["label-console"] });
      void qc.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  const r = results.data;
  const running = !!r?.job && ["queued", "running"].includes(r.job.status);
  const pv = preview.data;
  const saved = wantAsr ? (pv?.saved_members ?? 0) : 0;
  const fresh = wantAsr ? (pv?.new_members ?? 0) : 0;
  const problems: string[] = [];
  if (!seg) problems.push("Pick a VAD / diarisation model for the segments.");
  if (tasks.has("diar") && seg && !seg.capabilities.includes("diar")) problems.push(`${seg.display_name} returns no speakers; pick a diariser for the speakers task.`);
  if (wantAsr && !ids.length) problems.push("Pick at least one ASR model.");
  if (!tasks.size) problems.push("Choose at least one task.");
  if (strategy === "single" && !ids.includes(singleId ?? -1)) problems.push("Pick the model to use as the starting hypothesis.");
  if (low > high) problems.push("The review threshold must not be above the auto-accept threshold.");
  const button = !wantAsr ? "Label speakers" : labelButton(saved, fresh);

  const toggleTask = (t: string) => setTasks((s) => {
    const n = new Set(s);
    if (n.has(t)) n.delete(t);
    else n.add(t);
    return n;
  });

  return (
    <>
      <PageHeader crumbs={<Crumbs name={r?.dataset.name} />} title="Ensemble pseudo-labelling" subtitle="Segments come from one VAD / diarisation model; each segment is transcribed by every ASR member and the members are fused." />
      <LabelTabs datasetId={datasetId} current="pseudo" />
      <ErrorNote error={results.error ?? models.error} />
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
        <Panel title="Set up" subtitle={r ? `${num(r.dataset.usable_files)} usable files` : undefined}>
          <fieldset className="border-0 p-0 m-0 flex flex-col gap-2">
            <legend className="text-[11px] text-muted uppercase tracking-wider mb-1.5">Tasks</legend>
            <span className="flex flex-wrap gap-4 text-[13px]">
              <label className="flex items-center gap-2"><input type="checkbox" checked={tasks.has("asr")} onChange={() => toggleTask("asr")} /> Transcripts</label>
              <label className="flex items-center gap-2"><input type="checkbox" checked={tasks.has("diar")} onChange={() => toggleTask("diar")} /> Speakers</label>
              <label className="flex items-center gap-2 text-subtle" title="Keyword spotting is not built yet (Q6)"><input type="checkbox" disabled /> Keywords</label>
            </span>
          </fieldset>
          <div className="flex flex-col gap-1.5">
            <Select label="Segments from" value={segId ?? ""} onChange={(e) => setSegId(Number(e.target.value))}>
              {segModels.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.display_name} · {m.capabilities.includes("diar") ? "diariser" : "speech only"}
                </option>
              ))}
            </Select>
            {pv && (
              <span className="text-xs text-muted">
                <Chip tone={STATE_TONE[pv.seg.state]}>{STATE_TEXT[pv.seg.state]}</Chip> <span className="num">{pv.seg.cached_files}/{pv.files}</span> files segmented already
              </span>
            )}
            {!segModels.length && models.data && <Empty>No VAD / diarisation model registered. <Link to="/models/add">Add one</Link>.</Empty>}
          </div>
          {wantAsr && (
            <fieldset className="border-0 p-0 m-0 flex flex-col gap-1.5">
              <legend className="text-[11px] text-muted uppercase tracking-wider mb-1.5">ASR members</legend>
              {!asrModels.length && models.data && <Empty>No ASR model registered. <Link to="/models/add">Add one</Link>.</Empty>}
              {asrModels.map((m) => {
                const st = pv?.members.find((x) => x.model_id === m.id);
                return (
                  <label key={m.id} className="flex flex-wrap items-center gap-2 text-[13px] py-1 border-b border-track last:border-0">
                    <input
                      type="checkbox"
                      checked={asrIds.has(m.id)}
                      onChange={() => setAsrIds((s) => {
                        const n = new Set(s);
                        if (n.has(m.id)) n.delete(m.id);
                        else n.add(m.id);
                        return n;
                      })}
                    />
                    <span className="flex-1 min-w-0 truncate">{m.display_name}</span>
                    <span className="text-[11px] text-muted">{timingLabel(m.timing_level)}</span>
                    {st && (
                      <Chip tone={STATE_TONE[st.state]} title={`${st.cached_cuts} of ${st.known_cuts} known segments in the cache${st.files_without_segments ? `; ${st.files_without_segments} files not segmented yet` : ""}`}>
                        {STATE_TEXT[st.state]}
                      </Chip>
                    )}
                  </label>
                );
              })}
            </fieldset>
          )}
          {wantAsr && (
            <fieldset className="border-0 p-0 m-0 flex flex-col gap-1.5">
              <legend className="text-[11px] text-muted uppercase tracking-wider mb-1.5">Starting hypothesis</legend>
              {STRATEGIES.map((s) => (
                <label key={s.value} className="flex items-start gap-2 text-[13px]">
                  <input type="radio" name="strategy" checked={strategy === s.value} onChange={() => setStrategy(s.value)} className="mt-1" />
                  <span className="flex flex-col">
                    {s.label}
                    <span className="text-xs text-muted">{s.hint}</span>
                  </span>
                </label>
              ))}
              {strategy === "single" && (
                <Select label="Model" value={singleId ?? ""} onChange={(e) => setSingleId(Number(e.target.value))}>
                  <option value="">Choose…</option>
                  {asrModels.filter((m) => asrIds.has(m.id)).map((m) => <option key={m.id} value={m.id}>{m.display_name}</option>)}
                </Select>
              )}
            </fieldset>
          )}
          {wantAsr && (
            <fieldset className="border-0 p-0 m-0 flex flex-col gap-2">
              <legend className="text-[11px] text-muted uppercase tracking-wider mb-1.5">Confidence triage (member agreement)</legend>
              <span className="flex flex-wrap gap-4 text-[13px] items-center">
                <label className="flex items-center gap-2">
                  Auto-accept ≥
                  <input type="number" step={0.01} min={0} max={1} value={high} onChange={(e) => setHigh(Number(e.target.value))} className="num w-20 bg-canvas border border-line rounded px-2 min-h-8 text-fg" />
                </label>
                <label className="flex items-center gap-2">
                  Review ≥
                  <input type="number" step={0.01} min={0} max={1} value={low} onChange={(e) => setLow(Number(e.target.value))} className="num w-20 bg-canvas border border-line rounded px-2 min-h-8 text-fg" />
                </label>
                <span className="text-xs text-muted">below: rejected</span>
              </span>
              <span className="text-xs text-muted">Auto-accepted labels can feed training later, but never become a benchmark reference unless you confirm them.</span>
            </fieldset>
          )}
          {problems.length > 0 && <ul className="m-0 pl-4 text-xs text-warn">{problems.map((p) => <li key={p}>{p}</li>)}</ul>}
          <ErrorNote error={start.error} />
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary" disabled={!!problems.length || running || start.isPending || preview.isLoading} onClick={() => start.mutate()}>
              <I.Wand size={14} /> {button}
            </Button>
            {fresh > 0 && <span className="text-xs text-muted">The services for new models are started for you.</span>}
            {fresh === 0 && wantAsr && pv && <span className="text-xs text-muted">No inference needed: every member's output is saved.</span>}
          </div>
        </Panel>
        <Results r={r} running={running} loading={results.isLoading} />
      </div>
    </>
  );
}

function TriageBar({ s }: { s: LabelSummary }) {
  const parts: [keyof typeof STATUS_LABELS, string][] = [
    ["auto", C.good],
    ["pseudo", C.warn],
    ["rejected", C.bad],
  ];
  const total = s.segments || 1;
  return (
    <div className="flex flex-col gap-1.5">
      <span className="flex h-3 rounded overflow-hidden bg-track" role="img" aria-label={parts.map(([k]) => `${STATUS_LABELS[k]} ${s.triage[k] ?? 0}`).join(", ")}>
        {parts.map(([k, c]) => <span key={k} style={{ width: `${(100 * (s.triage[k] ?? 0)) / total}%`, background: c }} />)}
      </span>
      <span className="flex flex-wrap gap-3 text-xs text-muted">
        {parts.map(([k, c]) => (
          <span key={k} className="flex items-center gap-1.5">
            <span className="size-2.5 rounded-sm" style={{ background: c }} />
            {k === "pseudo" ? `Review ${s.thresholds.low.toFixed(2)}–${s.thresholds.high.toFixed(2)}` : k === "auto" ? `Auto-accept ≥ ${s.thresholds.high.toFixed(2)}` : `Reject < ${s.thresholds.low.toFixed(2)}`}
            <span className="num text-fg">{num(s.triage[k] ?? 0)}</span>
          </span>
        ))}
      </span>
    </div>
  );
}

function Results({ r, running, loading }: { r?: LabelResults; running: boolean; loading: boolean }) {
  if (loading || !r) return <Panel title="Results"><Spinner /></Panel>;
  const s = r.summary;
  const name = (svc: string) => (svc === "rover" ? "ROVER fusion" : r.model_names[svc] ?? svc);
  return (
    <div className="flex flex-col gap-5 min-w-0">
      <Panel title="Dataset progress" actions={r.job && <JobPill status={r.job.status} progress={r.job.progress} />}>
        <StatusBar p={r.progress} className="h-3" />
        <BarLegend p={r.progress} />
        {running && r.job?.message && <span className="text-xs text-muted num">{r.job.message}</span>}
        {r.job?.status === "failed" && <ErrorNote error={r.job.error} />}
        {r.job?.status === "blocked" && <ErrorNote error={`Blocked: ${r.job.error}`} />}
      </Panel>
      {!s && !running && <Empty>No pseudo-labels yet. Set up an ensemble and start.</Empty>}
      {s && (
        <>
          <Panel title="Triage" subtitle={`${num(s.segments)} segments in ${num(s.files)} files from run ${r.run?.id}${r.segmenter ? ` · segments from ${r.segmenter}` : ""}`}>
            <TriageBar s={s} />
            <div className="grid sm:grid-cols-2 gap-x-6">
              <Fact k="Left to reviewed work" v={num(s.kept_reviewed)} />
              <Fact k="Earlier pseudo-labels replaced" v={num(s.replaced)} />
              {Object.entries(s.strategy_applied).map(([k, v]) => <Fact key={k} k={`Fused with ${k.replace("_", "-")}`} v={num(v)} />)}
            </div>
          </Panel>
          {s.agreement.members.length > 1 && (
            <Panel title="Member agreement" subtitle="Pairwise WER: row = reference, column = hypothesis (after the basic normaliser).">
              <div className="overflow-x-auto scroll-thin">
                <table className="border-collapse text-[13px]">
                  <thead>
                    <tr>
                      <th scope="col" className="px-2 py-1.5" />
                      {s.agreement.members.map((m) => <th key={m} scope="col" className="px-2 py-1.5 text-[11px] text-muted font-medium text-right">{name(m)}</th>)}
                    </tr>
                  </thead>
                  <tbody>
                    {s.agreement.members.map((a) => (
                      <tr key={a}>
                        <th scope="row" className="px-2 py-1.5 text-[11px] text-muted font-medium text-left">{name(a)}</th>
                        {s.agreement.members.map((b) => {
                          const c = s.agreement.cells.find((x) => x.ref === a && x.hyp === b);
                          return (
                            <td key={b} className="px-2 py-1.5 text-right num" style={c?.wer !== undefined && c?.wer !== null ? { background: `rgba(244,63,94,${Math.min(0.5, c.wer)})` } : undefined}>
                              {a === b ? "—" : pct(c?.wer)}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
          )}
          {s.chosen.length > 0 && (
            <Panel title="Chosen as starting hypothesis">
              {s.chosen.map((c) => (
                <div key={c.member} className="grid grid-cols-[minmax(0,180px)_minmax(0,1fr)_64px] items-center gap-3 text-[13px]">
                  <span className="truncate">{name(c.member)}</span>
                  <Bar value={c.share} />
                  <span className="num text-right">{pct(c.share, 0)}</span>
                </div>
              ))}
            </Panel>
          )}
        </>
      )}
      <Panel title="Review queue" subtitle="Unreviewed segments, lowest confidence first." actions={r.queue.length > 0 && <Link to={`/label/annotate/${r.dataset.id}?segment=${r.queue[0].id}`} className="text-xs">Start reviewing →</Link>}>
        {r.queue.length === 0 && <span className="text-[13px] text-muted">Nothing waiting for review.</span>}
        <ul className="m-0 p-0 list-none flex flex-col max-h-[420px] overflow-auto scroll-thin">
          {r.queue.map((q) => (
            <li key={q.id} className="border-b border-track last:border-0">
              <Link to={`/label/annotate/${r.dataset.id}?file=${q.file_id}&segment=${q.id}`} className="grid grid-cols-[48px_minmax(0,1fr)] gap-3 items-start px-1.5 py-2 no-underline !text-fg hover:bg-track rounded">
                <span className="num text-xs font-semibold" style={{ color: confidenceColor(q.confidence) }}>{q.confidence === null ? "—" : q.confidence.toFixed(2)}</span>
                <span className="flex flex-col min-w-0">
                  <span className="truncate text-[13px]">{q.text || <span className="text-subtle">(no text)</span>}</span>
                  <span className={cx("text-[11px] text-muted num")}>{q.file} · {clock(q.start)} · {q.speaker ?? "—"} · {STATUS_LABELS[q.status]}</span>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}
