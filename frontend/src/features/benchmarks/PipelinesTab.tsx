import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, qs, type PipeCell, type PipeGrid } from "../../lib/api";
import { pct, rtf } from "../../lib/format";
import * as I from "../../components/icons";
import { AttributionBar } from "../../components/StageLegend";
import { Button, cx, Empty, ErrorNote, Fact, Panel, Select, Spinner, StatusDot, Toggle } from "../../components/ui";

export function PipelinesTab({ normaliser, collar }: { normaliser: string; collar: number }) {
  const qc = useQueryClient();
  const [dsId, setDsId] = useState<number | null>(null);
  const [metric, setMetric] = useState<"cpwer" | "wer">("cpwer");
  const [pick, setPick] = useState<{ seg: number | null; asr: number } | null>(null);
  const q = useQuery({
    queryKey: ["pipes", dsId, normaliser, collar],
    queryFn: () => api.get<PipeGrid>(`/benchmarks-pipelines${qs({ dataset_id: dsId, normaliser, collar })}`),
    refetchInterval: (query) => (query.state.data?.cells.some((c) => c.status === "queued" || c.status === "waiting" || c.status === "running") ? 3000 : false),
  });
  const run = useMutation({
    mutationFn: (c: PipeCell) =>
      api.post("/runs", { task: "pipeline", dataset_id: q.data!.dataset_id, asr_model_id: c.asr_model_id, seg_model_id: c.seg_model_id, oracle: c.oracle }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["pipes"] }),
  });
  if (q.isLoading) return <Spinner />;
  if (q.error || !q.data) return <ErrorNote error={q.error} />;
  const g = q.data;
  const eligible = g.datasets.filter((d) => d.has_labels);
  if (!eligible.length) return <Empty>Pipelines need a dataset with transcripts. Upload one with speaker-attributed transcripts (STM, SegLST or JSON segments) to get cpWER.</Empty>;
  if (!g.cols.length) return <Empty>No ASR model has run yet. Pick catalog models with Run evaluation (full grid) to build pipelines, or see <Link to="/models">Models</Link>.</Empty>;
  const cell = (seg: number | null, asr: number) => g.cells.find((c) => c.seg_model_id === seg && c.asr_model_id === asr)!;
  const val = (c: PipeCell) => (metric === "cpwer" ? c.cpwer : c.wer);
  const vals = g.cells.map(val).filter((v): v is number => v !== null && v !== undefined);
  const best = vals.length ? Math.min(...vals) : null;
  const firstDone = g.runs[0];
  const sel = pick ? cell(pick.seg, pick.asr) : firstDone;
  const rowName = (seg: number | null) => g.rows.find((r) => r.seg_model_id === seg)?.name ?? "";
  const colName = (asr: number) => g.cols.find((c) => c.asr_model_id === asr)?.name ?? "";
  const speechOnly = g.runs.filter((r) => r.cpwer === null || r.cpwer === undefined);

  return (
    <div className="flex flex-col xl:flex-row gap-5 items-start">
      <div className="flex-1 w-full flex flex-col gap-5 min-w-0">
        <Panel
          title={`Ablation grid · ${metric === "cpwer" ? "cpWER" : "WER"}`}
          subtitle="Rows: VAD / diarisation · columns: ASR · lower is better. Click a cell to inspect the pipeline."
          actions={
            <>
              <Select label="Dataset" value={g.dataset_id ?? ""} onChange={(e) => setDsId(Number(e.target.value))}>
                {eligible.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </Select>
              <Toggle label="Metric" value={metric} onChange={(v) => setMetric(v as "cpwer" | "wer")} options={[{ value: "cpwer", label: "cpWER" }, { value: "wer", label: "WER" }]} />
            </>
          }
        >
          <div className="overflow-x-auto scroll-thin">
            <div className="grid gap-1.5 min-w-[520px]" style={{ gridTemplateColumns: `minmax(180px, 1.3fr) repeat(${g.cols.length}, minmax(120px, 1fr))` }}>
              <span />
              {g.cols.map((c) => (
                <span key={c.asr_model_id} className="flex flex-col px-2 pb-1 text-right">
                  <span className="text-[12px] font-medium">{c.name}</span>
                  <span className="text-[11px] text-subtle">{c.timing_level ? `${c.timing_level} times` : "ASR"}</span>
                </span>
              ))}
              {g.rows.map((r) => (
                <RowCells
                  key={r.seg_model_id ?? "oracle"}
                  row={r}
                  cols={g.cols}
                  cell={cell}
                  val={val}
                  best={best}
                  metric={metric}
                  sel={sel}
                  onPick={(c) => setPick({ seg: c.seg_model_id, asr: c.asr_model_id })}
                  onRun={(c) => run.mutate(c)}
                />
              ))}
            </div>
          </div>
          <span className="flex flex-wrap gap-4 text-[11px] text-muted">
            <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm bg-good/30 border border-good" />Best in grid</span>
            <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm border border-focus" />Inspecting</span>
            <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm bg-secondary/40" />Reference segments + speakers (oracle)</span>
            <span className="flex items-center gap-1.5"><span className="num text-subtle">n/a</span>Speech-only model: no speakers, so no cpWER</span>
          </span>
          <ErrorNote error={run.error} />
        </Panel>
        <Panel title="All pipeline runs" subtitle={`Ranked by cpWER; speech-only runs after, by WER. ${speechOnly.length ? `${speechOnly.length} speech-only.` : ""}`}>
          {g.runs.length === 0 ? (
            <Empty>No pipeline runs on this dataset yet. Use Run in the grid or “Run evaluation”.</Empty>
          ) : (
            <div className="overflow-x-auto scroll-thin">
              <table className="w-full border-collapse text-[13px]">
                <thead>
                  <tr className="text-[11px] text-muted uppercase tracking-wider">
                    <th scope="col" className="text-left font-medium px-3 py-2 border-b border-line">VAD / diarisation → ASR</th>
                    <th scope="col" className="text-right font-medium px-3 py-2 border-b border-line">cpWER</th>
                    <th scope="col" className="text-right font-medium px-3 py-2 border-b border-line">WER</th>
                    <th scope="col" className="text-right font-medium px-3 py-2 border-b border-line">DER</th>
                    <th scope="col" className="text-right font-medium px-3 py-2 border-b border-line">RTF</th>
                    <th scope="col" className="px-3 py-2 border-b border-line"><span className="sr-only">Action</span></th>
                  </tr>
                </thead>
                <tbody>
                  {g.runs.map((r) => (
                    <tr key={`${r.seg_model_id}-${r.asr_model_id}`} className={cx("border-b border-track", sel === r && "bg-active/50")}>
                      <td className="px-3 py-2">
                        <span className="flex flex-wrap items-center gap-1.5">
                          <span className={cx(r.oracle && "text-secondary")}>{rowName(r.seg_model_id)}</span>
                          <span className="text-subtle">→</span>
                          <span>{colName(r.asr_model_id)}</span>
                        </span>
                      </td>
                      <td className="px-3 py-2 text-right num">{r.cpwer === null || r.cpwer === undefined ? <span className="text-subtle">n/a</span> : pct(r.cpwer)}</td>
                      <td className="px-3 py-2 text-right num">{pct(r.wer)}</td>
                      <td className="px-3 py-2 text-right num">{r.oracle ? <span className="text-subtle">oracle</span> : pct(r.der)}</td>
                      <td className="px-3 py-2 text-right num">{rtf(r.rtf)}</td>
                      <td className="px-3 py-2 text-right">
                        <Button size="sm" onClick={() => setPick({ seg: r.seg_model_id, asr: r.asr_model_id })}>Inspect</Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
      <aside aria-label="Selected pipeline" className="w-full xl:w-[340px] shrink-0 flex flex-col gap-4">
        {sel && sel.score_id ? <PipeDetail c={sel} seg={rowName(sel.seg_model_id)} asr={colName(sel.asr_model_id)} ds={g.dataset?.name ?? ""} normaliser={normaliser} collar={collar} /> : (
          <Panel title="Inspecting pipeline"><span className="text-[13px] text-muted">Run a pipeline, then click its cell.</span></Panel>
        )}
        <Panel title="How pipelines are scored">
          <p className="m-0 text-[13px] text-muted leading-relaxed"><span className="text-fg num">cpWER</span> matches each reference speaker to one output speaker, then counts word errors, so words given to the wrong speaker count as errors (twice: missing from one speaker, extra for another).</p>
          <p className="m-0 text-[13px] text-muted leading-relaxed"><span className="text-fg num">WER</span> ignores speakers, so it shows transcription quality after segmentation.</p>
          <p className="m-0 text-[13px] text-muted leading-relaxed"><span className="text-fg">VAD / diarisation</span> is one stage. Models that find speakers get DER and cpWER; speech-only models get missed and false-alarm speech and WER.</p>
        </Panel>
      </aside>
    </div>
  );
}

function RowCells({
  row,
  cols,
  cell,
  val,
  best,
  metric,
  sel,
  onPick,
  onRun,
}: {
  row: PipeGrid["rows"][number];
  cols: PipeGrid["cols"];
  cell: (seg: number | null, asr: number) => PipeCell;
  val: (c: PipeCell) => number | null | undefined;
  best: number | null;
  metric: string;
  sel: PipeCell | undefined;
  onPick: (c: PipeCell) => void;
  onRun: (c: PipeCell) => void;
}) {
  return (
    <>
      <span className={cx("flex flex-col justify-center px-2 rounded-md", row.oracle && "bg-secondary/15")}>
        <span className="text-[13px]">{row.name}</span>
        <span className="text-[11px] text-subtle">{row.oracle ? "oracle" : row.has_speakers ? "speakers" : "speech only"}</span>
      </span>
      {cols.map((col) => {
        const c = cell(row.seg_model_id, col.asr_model_id);
        const v = val(c);
        const busy = c.status === "queued" || c.status === "waiting" || c.status === "running";
        if (!c.score_id) {
          return (
            <span key={col.asr_model_id} className="flex items-center justify-end gap-2 min-h-12 px-2 rounded-md bg-track/40">
              {busy ? (
                <span className="text-[11px] text-muted inline-flex items-center gap-1.5"><StatusDot state={c.status ?? "queued"} />{c.status === "waiting" ? "waiting for compute" : c.status}</span>
              ) : !row.available ? (
                <span className="text-[11px] text-subtle" title="The oracle needs speaker labels in this dataset">no speaker labels</span>
              ) : (
                <>
                  {(c.status === "failed" || c.status === "blocked") && <span className="text-[11px] text-bad" title={c.error ?? ""}>{c.status}</span>}
                  <span className="text-subtle">—</span>
                  <Button size="sm" onClick={() => onRun(c)} aria-label={`Run ${row.name} → ${col.name}`}>Run</Button>
                </>
              )}
            </span>
          );
        }
        const na = metric === "cpwer" && (v === null || v === undefined);
        const isBest = !na && v === best;
        return (
          <button
            key={col.asr_model_id}
            type="button"
            aria-pressed={sel === c}
            aria-label={`${row.name} → ${col.name}: ${na ? "n/a" : pct(v)}`}
            onClick={() => onPick(c)}
            className={cx(
              "min-h-12 px-3 rounded-md text-right cursor-pointer border",
              isBest ? "bg-good/15 border-good/50" : row.oracle ? "bg-secondary/15 border-transparent" : "bg-track/60 border-transparent hover:bg-track",
              sel === c && "!border-focus",
            )}
          >
            <span className={cx("num text-[14px]", na && "text-subtle")}>{na ? "n/a" : pct(v)}</span>
          </button>
        );
      })}
    </>
  );
}

function PipeDetail({ c, seg, asr, ds, normaliser, collar }: { c: PipeCell; seg: string; asr: string; ds: string; normaliser: string; collar: number }) {
  return (
    <Panel>
      <div className="flex flex-col gap-1.5">
        <span className="text-[11px] text-muted uppercase tracking-wider">Inspecting pipeline</span>
        <span className="flex flex-col gap-1 text-[13px]">
          <span className="flex items-center gap-2"><span className="text-[10px] uppercase tracking-wider text-stage-seg w-24">VAD / diar.</span>{seg}</span>
          <span className="flex items-center gap-2"><span className="text-[10px] uppercase tracking-wider text-stage-asr w-24">ASR</span>{asr}</span>
        </span>
        <span className="text-xs text-muted">{ds}</span>
      </div>
      <div className="flex items-baseline gap-2">
        <span className="num text-[30px] font-medium">{c.cpwer === null || c.cpwer === undefined ? pct(c.wer) : pct(c.cpwer)}</span>
        <span className="text-xs text-muted">{c.cpwer === null || c.cpwer === undefined ? "WER (speech-only, no cpWER)" : "cpWER"}</span>
      </div>
      {c.attribution && !c.oracle && (
        <div className="flex flex-col gap-2">
          <span className="text-xs text-muted">Where the errors come from</span>
          <AttributionBar att={c.attribution} />
          <span className="text-[11px] text-subtle">Measured by swapping each stage for your reference labels and re-scoring.</span>
        </div>
      )}
      <div className="flex flex-col">
        <Fact k="WER" v={pct(c.wer)} />
        <Fact k="DER" v={c.oracle ? "oracle" : pct(c.der)} />
        {c.attribution && !c.oracle && <Fact k="With reference segments" v={pct(c.attribution.with_reference_segments)} />}
        <Fact k="RTF (seg + ASR)" v={`${rtf(c.rtf_by_stage?.seg)} + ${rtf(c.rtf_by_stage?.asr)}`} />
      </div>
      {c.run_id && (
        <Link to={`/errors/pipeline/${c.run_id}${qs({ normaliser, collar })}`} className="inline-flex items-center gap-1.5 text-[13px]">
          Open pipeline error analysis <I.Arrow size={14} />
        </Link>
      )}
    </Panel>
  );
}
