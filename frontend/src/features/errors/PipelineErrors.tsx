import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, qs, type AlignOp, type Attribution, type ErrorRegion, type Hardware, type Lane, type Page, type PipeGrid, type Run, type SliceReport } from "../../lib/api";
import { useConfig, useNormaliser } from "../../lib/config";
import { STAGE_COLORS, STAGE_LABELS, speakerColors } from "../../lib/colors";
import { clock, deltaPts, deltaTone, duration, hwLabel, num, pct, rtf } from "../../lib/format";
import { NormaliserButtons } from "../../components/NormaliserButtons";
import { SliceTable } from "./AsrLabels";
import { AudioPlayer, useAudio } from "../../components/AudioPlayer";
import { DiffText } from "../../components/DiffText";
import { MetricTile } from "../../components/MetricTile";
import { ExpandCell, SortableTable } from "../../components/SortableTable";
import { StageLegend } from "../../components/StageLegend";
import { ErrorLegend, Timeline } from "../../components/Timeline";
import * as I from "../../components/icons";
import { AnchorButton, Button, cx, Empty, EnhChip, ErrorNote, PageHeader, Panel, Select, Spinner } from "../../components/ui";
import { Crumbs, RunPickers, Switcher, useSwitchLinks } from "./common";
import { speakerLaneGroups } from "./DiarErrors";

interface Summary {
  run: Run;
  score_id: number;
  normaliser: string;
  collar: number;
  oracle: boolean;
  has_speakers: boolean;
  chain: { stage: string; name: string }[];
  tiles: { cpwer: number | null; tcpwer: number | null; tcp_errors: number | null; wer: number | null; der: number | null; attribution: Attribution | null; cp_errors: number | null; n: number | null };
  tcp_collar: number;
  tcp_collars: number[];
  skip_overlap: boolean;
  stages: { kind: "enhance" | "seg" | "asr" | "output"; name: string; metric: number | null; metric_label: string | null; rtf: number | null; hardware?: Hardware; handoff?: string; hears?: string; placement?: string | null; oracle?: boolean; generative?: boolean; wall_s?: number | null }[];
  waterfall: { key: string; label: string; sub: string; value: number | null; total: number }[] | null;
  moments: { key: string; label: string; words: number; parts: Record<string, number>; rate: number; main: string | null }[];
  counterfactuals: { stage: string; label: string; cpwer: number; note: string }[];
  wrong_speaker_pairs: { word: string; move: string; n: number }[];
}

interface FileRow {
  id: number;
  key: string;
  file_id: number;
  duration: number;
  cpwer: number | null;
  wer: number | null;
  der: number | null;
  rtf: number | null;
  attribution: Attribution | null;
}

interface FileDetail extends FileRow {
  ref: Lane[];
  hyp: Lane[];
  mapping: Record<string, string>;
  errors: ErrorRegion[];
  overlaps: { start: number; end: number }[];
  ref_segments: { start: number; end: number; speaker: string; text: string }[];
  hyp_segments: { start: number; end: number; speaker: string | null; text: string }[];
  assignment: [string | null, string | null][];
  pairs: { ref_speaker: string | null; hyp_speaker: string | null; ops: AlignOp[]; errors: number; n: number }[];
  wrong_speaker_words: { word: string; ref_speaker: string; hyp_speaker: string; given_to: string | null }[];
  ref_lines: { start: number; end: number; speaker: string; kinds: string[]; words: { w: string; cause: string | null }[] }[];
  label_url: string | null;
}

const SOURCE_NOTES: Record<string, string> = {
  asr: "Words the ASR model gets wrong even with reference segments and speakers",
  segmentation: "Extra errors from the model's segment boundaries (speech cut, merged or missed)",
  speaker: "Words given to the wrong speaker; each counts twice in cpWER",
};

export function PipelineErrors() {
  const { runId } = useParams();
  const [params, setParams] = useSearchParams();
  const { data: cfg } = useConfig();
  const [normaliser] = useNormaliser();
  const collar = params.get("collar") ?? undefined;
  const tcpCollar = params.get("tcp_collar") ?? undefined;
  const skipOverlap = params.get("skip_overlap") === "1";
  const s = useQuery({
    queryKey: ["pipe-summary", runId, normaliser, collar, tcpCollar, skipOverlap],
    queryFn: () => api.get<Summary>(`/errors/pipeline/summary${qs({ run_id: runId, normaliser, collar, tcp_collar: tcpCollar, skip_overlap: skipOverlap || undefined })}`),
    placeholderData: keepPreviousData,
  });
  const bySlice = useQuery({
    queryKey: ["slices", s.data?.score_id],
    queryFn: () => api.get<SliceReport>(`/errors/slices${qs({ score_id: s.data!.score_id })}`),
    enabled: !!s.data && s.data.tiles.cpwer !== null,
  });
  const links = useSwitchLinks(s.data?.run.dataset_id, "");
  const set = (k: string, v: string | null) => {
    const p = new URLSearchParams(params);
    if (v === null) p.delete(k);
    else p.set(k, v);
    setParams(p, { replace: true });
  };
  if (s.isLoading) return <Spinner />;
  if (s.error || !s.data) return <ErrorNote error={s.error} />;
  const d = s.data;
  const att = d.tiles.attribution;
  const parts = (["asr", "segmentation", "speaker"] as const).map((k) => ({ k, n: att ? att[`${k}_errors` as "asr_errors"] : 0, v: att ? att[k] : null }));
  const totalPos = parts.reduce((a, p) => a + Math.max(0, p.n), 0) || 1;

  return (
    <>
      <PageHeader
        crumbs={<Crumbs parts={[{ to: "/benchmarks/pipeline", label: "Benchmarks" }, "Pipelines", d.run.dataset]} />}
        title={
          <span className="flex flex-wrap items-center gap-2">
            {d.chain.map((c, i) => (
              <span key={i} className="flex items-center gap-2">
                {i > 0 && <span className="text-subtle">→</span>}
                <span className="flex flex-col">
                  <span className="text-[10px] uppercase tracking-wider font-normal" style={{ color: i === 0 ? STAGE_COLORS.segmentation : STAGE_COLORS.asr }}>
                    {c.stage}
                  </span>
                  <span>{c.name}</span>
                </span>
              </span>
            ))}
          </span>
        }
        actions={
          <RunPickers
            task="pipeline"
            run={d.run}
            path={(r) => `/errors/pipeline/${r.id}${qs({ normaliser, collar })}`}
            extra={
              <>
                <NormaliserButtons />
                <Select label="Collar" value={String(d.collar)} onChange={(e) => set("collar", e.target.value)}>
                  {(cfg?.collars ?? [0, 0.25]).map((x) => (
                    <option key={x} value={x}>
                      ±{x} s
                    </option>
                  ))}
                </Select>
                <Select label="tcpWER collar" value={String(d.tcp_collar)} onChange={(e) => set("tcp_collar", e.target.value)}>
                  {d.tcp_collars.map((x) => (
                    <option key={x} value={x}>
                      ±{x} s
                    </option>
                  ))}
                </Select>
                <label className="flex items-center gap-2 text-xs text-muted cursor-pointer" title="Leave overlapped speech out of the DER of the VAD / diarisation stage">
                  <input type="checkbox" checked={!skipOverlap} onChange={(e) => set("skip_overlap", e.target.checked ? null : "1")} className="accent-[#06B6D4]" />
                  Score overlapping speech
                </label>
              </>
            }
          />
        }
      />
      <Switcher current="pipeline" links={links} />
      <StageCards stages={d.stages} />
      <div className="flex flex-wrap gap-3">
        <MetricTile label="cpWER" value={d.tiles.cpwer === null ? "n/a" : pct(d.tiles.cpwer)} sub={d.tiles.cpwer === null ? "speech-only stage: no speakers" : `${num(d.tiles.cp_errors)} errors / ${num(d.tiles.n)} words`} />
        {d.tiles.tcpwer !== null && <MetricTile label="tcpWER" value={pct(d.tiles.tcpwer)} sub={`words also within ±${d.tcp_collar} s of their time`} />}
        <MetricTile label="WER" value={pct(d.tiles.wer)} sub="speakers ignored" />
        <MetricTile label="DER" value={d.oracle ? "oracle" : pct(d.tiles.der)} sub="of the VAD / diarisation stage" />
        {att && !d.oracle && <MetricTile label="With reference segments" value={pct(att.with_reference_segments)} sub="the same ASR on your segments + speakers" color={STAGE_COLORS.asr} />}
      </div>
      {att && !d.oracle ? (
        <div className="flex flex-col xl:flex-row gap-5 items-start">
          <Panel className="flex-1 w-full" id="src-h" title={`Where the ${num(att.c_errors)} cpWER errors come from`} subtitle={`${num(att.n)} reference words. Each error is traced to the stage that caused it, by swapping stages for your reference labels.`} actions={<StageLegend />}>
            <div className="flex h-3 rounded overflow-hidden bg-track" role="img" aria-label="Errors by stage">
              {parts.map((p) => (
                <span key={p.k} style={{ width: `${(Math.max(0, p.n) / totalPos) * 100}%`, background: STAGE_COLORS[p.k] }} />
              ))}
            </div>
            {parts.map((p) => (
              <div key={p.k} className="grid grid-cols-[minmax(0,1fr)_auto_auto] gap-4 items-center border-b border-track last:border-0 py-2">
                <span className="flex flex-col gap-0.5">
                  <span className="flex items-center gap-2 text-[13px]">
                    <span className="size-2.5 rounded-sm" style={{ background: STAGE_COLORS[p.k] }} />
                    {STAGE_LABELS[p.k]}
                  </span>
                  <span className="text-[11px] text-subtle">{SOURCE_NOTES[p.k]}</span>
                </span>
                <span className="num text-[13px]">{num(p.n)} words</span>
                <span className={cx("num text-[13px] w-16 text-right", (p.v ?? 0) < 0 && "text-warn")} title={(p.v ?? 0) < 0 ? "Negative: model segments suited the ASR better than the reference segments" : undefined}>
                  {pct(p.v)}
                </span>
              </div>
            ))}
            <span className="text-[11px] text-subtle">Wrong-speaker words count twice in cpWER (missing from the right speaker, extra for the wrong one). Per-file negatives are shown raw in the table and clipped to 0 in bars.</span>
          </Panel>
          <aside className="w-full xl:w-[340px] shrink-0 flex flex-col gap-4">
            <Panel title="If one stage were perfect" subtitle="cpWER with that stage replaced by your reference labels">
              {d.counterfactuals.map((f) => (
                <div key={f.stage} className="flex flex-col gap-0.5 border-b border-track last:border-0 pb-2">
                  <span className="flex items-center justify-between gap-3 text-[13px]">
                    <span className="flex items-center gap-2">
                      <span className="size-2.5 rounded-sm shrink-0" style={{ background: STAGE_COLORS[f.stage] }} />
                      {f.label}
                    </span>
                    <span className="num whitespace-nowrap">
                      {pct(f.cpwer)} <span className="text-good text-xs">↓{((d.tiles.cpwer! - f.cpwer) * 100).toFixed(1)}</span>
                    </span>
                  </span>
                  <span className="text-[11px] text-subtle">{f.note}</span>
                </div>
              ))}
            </Panel>
            <Panel title="Words given to the wrong speaker">
              {d.wrong_speaker_pairs.length === 0 && <span className="text-[13px] text-muted">None found.</span>}
              {d.wrong_speaker_pairs.map((p) => (
                <div key={`${p.word}-${p.move}`} className="flex items-center justify-between text-[13px]">
                  <span className="num">
                    “{p.word}” <span className="text-stage-spk">{p.move}</span>
                  </span>
                  <span className="num text-muted">×{p.n}</span>
                </div>
              ))}
            </Panel>
          </aside>
        </div>
      ) : (
        <Empty>
          {d.oracle
            ? "This is the oracle row: reference segments and speakers feed the ASR, so every error is an ASR error."
            : d.has_speakers
              ? "Error attribution needs speaker-attributed transcripts in the dataset."
              : "Speech-only VAD stage: no speakers, so no cpWER or attribution. WER is shown per file below."}{" "}
          <Link to={`/benchmarks/pipeline`}>Back to the grid</Link>
        </Empty>
      )}
      {d.waterfall && !d.oracle && <Waterfall steps={d.waterfall} />}
      {att && !d.oracle && <Upgrade run={d.run} normaliser={normaliser} cpwer={d.tiles.cpwer} />}
      {d.moments.length > 0 && !d.oracle && <Moments rows={d.moments} />}
      {bySlice.data && bySlice.data.labels.some((l) => l.kind !== "unused") && (
        <Panel id="plbl-h" title="By label" subtitle="cpWER per file label, with 95% intervals resampled by file.">
          <SliceTable rows={bySlice.data.finder ?? []} overall={bySlice.data.overall.value} low unit="Files" />
        </Panel>
      )}
      <Panel id="pf-h" title="Files" subtitle="Sort by any column. Open a file to read the transcript with every error tagged by the stage that caused it, and compare speaker lanes." actions={<ErrorLegend />}>
        <PipeFileTable scoreId={d.score_id} modelName={d.chain[0].name} hasAtt={!!att && !d.oracle} />
      </Panel>
    </>
  );
}

function PipeFileTable({ scoreId, modelName, hasAtt }: { scoreId: number; modelName: string; hasAtt: boolean }) {
  const [open, setOpen] = useState<string | number | null>(null);
  const columns = useMemo<ColumnDef<FileRow, unknown>[]>(() => {
    const cols: ColumnDef<FileRow, unknown>[] = [
      { id: "key", header: "File", cell: ({ row }) => <ExpandCell open={open === row.original.id} label={row.original.key} /> },
      { id: "cpwer", header: "cpWER", meta: { align: "right" }, cell: ({ row }) => <span className="num">{row.original.cpwer === null ? <span className="text-subtle">n/a</span> : pct(row.original.cpwer)}</span> },
      { id: "wer_", header: "WER", enableSorting: false, meta: { align: "right" }, cell: ({ row }) => <span className="num">{pct(row.original.wer)}</span> },
      { id: "der", header: "DER", meta: { align: "right" }, cell: ({ row }) => <span className="num">{pct(row.original.der)}</span> },
    ];
    if (hasAtt)
      for (const k of ["asr", "segmentation", "speaker"] as const)
        cols.push({
          id: `att_${k}`,
          header: STAGE_LABELS[k].split(" ")[0],
          enableSorting: false,
          meta: { align: "right" },
          cell: ({ row }) => {
            const v = row.original.attribution?.[k];
            return <span className={cx("num", (v ?? 0) < 0 ? "text-warn" : "")} style={{ color: (v ?? 0) > 0 ? STAGE_COLORS[k] : undefined }}>{pct(v)}</span>;
          },
        });
    cols.push({ id: "rtf", header: "RTF", meta: { align: "right" }, cell: ({ row }) => <span className="num text-muted">{rtf(row.original.rtf)}</span> });
    return cols;
  }, [open, hasAtt]);
  return (
    <SortableTable<FileRow>
      columns={columns}
      caption="Pipeline files"
      queryKey={["pipe-files", scoreId]}
      fetchPage={({ offset, limit, sort, order }) => api.get<Page<FileRow>>(`/errors/pipeline/files${qs({ score_id: scoreId, offset, limit, sort, order })}`)}
      initialSort={{ id: "cpwer", desc: true }}
      rowKey={(r) => r.id}
      expanded={open}
      onExpand={setOpen}
      renderExpanded={(r) => <PipeFileDetail id={r.id} modelName={modelName} />}
      maxHeight="80vh"
    />
  );
}

function PipeFileDetail({ id, modelName }: { id: number; modelName: string }) {
  const q = useQuery({ queryKey: ["pipe-file", id], queryFn: () => api.get<FileDetail>(`/errors/pipeline/files/${id}`) });
  const ctl = useAudio(q.data?.file_id);
  if (!q.data) return <div className="p-4"><Spinner /></div>;
  const f = q.data;
  const groups = speakerLaneGroups(f.ref, f.hyp, f.mapping, modelName);
  const refColors = speakerColors(f.ref_segments.map((s) => s.speaker));
  const assigned = Object.fromEntries(f.assignment.filter(([r, h]) => r && h).map(([r, h]) => [h!, r!]));
  return (
    <div className="p-4 flex flex-col gap-4">
      <div className="flex flex-col gap-3 bg-panel border border-track rounded-md p-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <AudioPlayer ctl={ctl} start={0} end={f.duration} label="Play file" />
          {f.label_url && (
            <AnchorButton href={f.label_url} newTab>
              <I.Pen size={13} /> Open in Label
            </AnchorButton>
          )}
        </div>
        <Timeline fileId={f.file_id} t0={0} t1={f.duration} groups={groups} errors={f.errors} overlaps={f.overlaps} time={ctl.time} onSeek={(t) => ctl.play(t)} />
      </div>
      {f.ref_lines.length > 0 && <TaggedTranscript lines={f.ref_lines} refColors={refColors} ctl={ctl} />}
      <div className="grid gap-4 lg:grid-cols-2">
        <TranscriptSide title="Reference" segs={f.ref_segments.map((s) => ({ ...s, color: refColors[s.speaker] }))} ctl={ctl} />
        <TranscriptSide
          title="Pipeline output"
          segs={f.hyp_segments.map((s) => {
            const to = s.speaker ? assigned[s.speaker] : undefined;
            return { ...s, speaker: s.speaker ? (to ? `${s.speaker} → ${to}` : s.speaker) : "speech", color: to ? refColors[to] : "#64748B" };
          })}
          ctl={ctl}
        />
      </div>
      {f.pairs.length > 0 && (
        <div className="flex flex-col gap-2">
          <span className="text-[11px] text-muted uppercase tracking-wider">cpWER alignment per speaker pair</span>
          {f.pairs.map((p, k) => (
            <div key={k} className="flex flex-col gap-1 bg-panel border border-track rounded-md p-2.5">
              <span className="text-xs text-muted num">
                {p.ref_speaker ?? "(no reference speaker)"} ↔ {p.hyp_speaker ?? "(no output speaker)"} · {p.errors} errors / {p.n} words
              </span>
              <DiffText ops={p.ops} mode="inline" />
            </div>
          ))}
          {f.wrong_speaker_words.length > 0 && (
            <span className="text-xs text-stage-spk">
              Wrong speaker: {f.wrong_speaker_words.map((w) => `“${w.word}” (${w.ref_speaker} → ${w.given_to ?? w.hyp_speaker})`).join(", ")}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function TranscriptSide({ title, segs, ctl }: { title: string; segs: { start: number; end: number; speaker: string; text: string; color: string }[]; ctl: ReturnType<typeof useAudio> }) {
  return (
    <div className="flex flex-col gap-1.5 min-w-0">
      <span className="text-[11px] text-muted uppercase tracking-wider">{title}</span>
      <div className="flex flex-col gap-1 max-h-[340px] overflow-y-auto scroll-thin pr-1">
        {segs.map((s, k) => {
          const active = ctl.time >= s.start && ctl.time <= s.end && ctl.playing;
          return (
            <button key={k} type="button" onClick={() => ctl.play(s.start, s.end)} className={cx("text-left bg-transparent border border-transparent rounded-md px-2 py-1 cursor-pointer hover:bg-track", active && "!border-focus")}>
              <span className="flex items-center gap-2 text-[11px]">
                <span className="size-2 rounded-sm" style={{ background: s.color }} />
                <span className="num" style={{ color: s.color }}>
                  {s.speaker}
                </span>
                <span className="num text-subtle">{clock(s.start)}</span>
              </span>
              <span className="text-[13px] text-fg">{s.text || <span className="text-subtle italic">(nothing)</span>}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

const CAUSE_LABEL: Record<string, string> = { asr: "ASR", segmentation: "Segmentation", speaker: "Speaker" };
const KIND_COLOR: Record<string, string> = { enhance: "var(--color-enh)", seg: STAGE_COLORS.segmentation, asr: STAGE_COLORS.asr, output: "var(--color-fg)" };
const KIND_LABEL: Record<string, string> = { enhance: "Enhancement", seg: "VAD / diarisation", asr: "ASR", output: "Output" };

function StageCards({ stages }: { stages: Summary["stages"] }) {
  return (
    <section aria-label="Pipeline stages" className="flex flex-wrap items-stretch gap-2">
      {stages.map((s, i) => (
        <div key={s.kind} className="flex items-stretch gap-2 flex-1 min-w-[200px]">
          <div className="flex-1 bg-panel border border-track rounded-lg px-3.5 py-3 flex flex-col gap-1.5" style={{ borderTop: `2px solid ${KIND_COLOR[s.kind]}` }}>
            <span className="flex items-center gap-2 text-[11px] uppercase tracking-wider" style={{ color: KIND_COLOR[s.kind] }}>
              <span className="num">{i + 1}</span>
              {KIND_LABEL[s.kind]}
              {s.hears && <span className={cx("ml-auto normal-case tracking-normal text-[10px] px-1.5 rounded-full", s.hears === "enhanced" ? "bg-enh/15 text-enh-soft" : "bg-track text-muted")}>hears {s.hears}</span>}
            </span>
            <span className="text-[13px] font-medium flex items-center gap-1.5">
              {s.name} {s.generative && <EnhChip>generative</EnhChip>}
            </span>
            {s.metric_label && (
              <span className="flex items-baseline gap-1.5">
                <span className="num text-[18px]">{s.oracle ? "oracle" : pct(s.metric)}</span>
                <span className="text-[11px] text-muted">{s.metric_label}</span>
              </span>
            )}
            <span className="num text-[11px] text-muted">
              {s.kind === "output" ? `RTF ${rtf(s.rtf)}${s.wall_s ? ` · ${duration(s.wall_s)}` : ""}` : `${s.hardware ? hwLabel(s.hardware) : ""}${s.rtf !== null && s.rtf !== undefined ? ` · RTF ${rtf(s.rtf)}` : ""}`}
            </span>
            {s.placement && <span className="text-[11px] text-enh-soft">{s.placement === "before" ? "before diarisation" : "after diarisation"}</span>}
          </div>
          {s.handoff && i < stages.length - 1 && (
            <span className="flex flex-col items-center justify-center gap-1 text-subtle w-20 shrink-0 text-center">
              <I.Arrow size={16} />
              <span className="text-[10px] leading-tight">{s.handoff}</span>
            </span>
          )}
        </div>
      ))}
    </section>
  );
}

function Waterfall({ steps }: { steps: NonNullable<Summary["waterfall"]> }) {
  const max = Math.max(0.01, ...steps.map((s) => s.total)) * 1.05;
  let prev = 0;
  const colors: Record<string, string> = { ...STAGE_COLORS, timing: "var(--color-warn)" };
  return (
    <Panel id="wf-h" title="How the errors add up" subtitle="Start from the ASR on your reference segments and speakers, then add back each stage's mistakes.">
      <div className="flex flex-col gap-1.5" role="img" aria-label={steps.map((s) => `${s.label} ${pct(s.value)}, total ${pct(s.total)}`).join("; ")}>
        {steps.map((s) => {
          const from = prev;
          prev = s.total;
          const lo = Math.min(from, s.total);
          const hi = Math.max(from, s.total);
          return (
            <div key={s.key} className="grid grid-cols-[minmax(150px,220px)_minmax(0,1fr)_80px] gap-3 items-center">
              <span className="flex flex-col">
                <span className="text-[13px]">{s.label}</span>
                <span className="text-[11px] text-subtle">{s.sub}</span>
              </span>
              <span className="relative h-5 bg-track/40 rounded">
                <span className="absolute top-0 bottom-0 rounded" style={{ left: `${(lo / max) * 100}%`, width: `${Math.max(0.4, ((hi - lo) / max) * 100)}%`, background: colors[s.key] ?? "var(--color-secondary)", opacity: s.key === "asr" ? 1 : 0.85 }} />
              </span>
              <span className="num text-[13px] text-right">
                {s.key === "asr" ? pct(s.value) : `${(s.value ?? 0) >= 0 ? "+" : "−"}${Math.abs((s.value ?? 0) * 100).toFixed(1)}`}
                <span className="block text-[11px] text-subtle">= {pct(s.total)}</span>
              </span>
            </div>
          );
        })}
      </div>
      <span className="text-[11px] text-subtle">
        ASR alone is cpWER with reference segments and speakers. Segmentation and speaker attribution add the rest of cpWER; timing adds the words tcpWER also counts wrong because they fall outside the collar.
      </span>
    </Panel>
  );
}

function Upgrade({ run, normaliser, cpwer }: { run: Run; normaliser: string; cpwer: number | null }) {
  const qc = useQueryClient();
  const g = useQuery({ queryKey: ["pipes", run.dataset_id, normaliser], queryFn: () => api.get<PipeGrid>(`/benchmarks-pipelines${qs({ dataset_id: run.dataset_id, normaliser })}`) });
  const [pick, setPick] = useState<{ seg: number | null; asr: number } | null>(null);
  const runCell = useMutation({
    mutationFn: (c: { seg: number | null; asr: number; oracle: boolean }) => api.post("/runs", { task: "pipeline", dataset_id: run.dataset_id, asr_model_id: c.asr, seg_model_id: c.seg, oracle: c.oracle }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["pipes"] }),
  });
  if (!g.data) return null;
  const cell = (seg: number | null, asr: number) => g.data.cells.find((c) => c.seg_model_id === seg && c.asr_model_id === asr);
  const vals = g.data.cells.map((c) => c.cpwer).filter((v): v is number => v !== null && v !== undefined);
  const best = vals.length ? Math.min(...vals) : null;
  const sel = pick ? cell(pick.seg, pick.asr) : undefined;
  const oracleRow = g.data.cells.filter((c) => c.oracle && c.cpwer !== null && c.cpwer !== undefined);
  const curAsr = run.asr_model_id;
  const oracleSame = oracleRow.find((c) => c.asr_model_id === curAsr);
  return (
    <Panel id="up-h" title="Which stage to upgrade" subtitle="cpWER for every diarisation × ASR pair you have run, on original audio. Pick a cell to compare it with this pipeline.">
      <div className="overflow-x-auto scroll-thin">
        <table className="border-collapse text-[13px]">
          <thead>
            <tr>
              <th scope="col" className="text-left text-[11px] text-muted font-medium px-2 py-1">Diarisation ╲ ASR</th>
              {g.data.cols.map((c) => (
                <th key={c.asr_model_id} scope="col" className="text-[11px] text-muted font-medium px-2 py-1 text-right">{c.name}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {g.data.rows.map((r) => (
              <tr key={r.seg_model_id ?? "oracle"}>
                <th scope="row" className="text-left font-normal px-2 py-1">
                  <span className="flex flex-col">
                    <span className={cx(r.oracle && "text-secondary")}>{r.name}</span>
                    <span className="text-[11px] text-subtle">{r.oracle ? "perfect diarisation (not a real pipeline)" : r.has_speakers ? "speakers" : "speech only"}</span>
                  </span>
                </th>
                {g.data.cols.map((col) => {
                  const c = cell(r.seg_model_id, col.asr_model_id);
                  const isCur = r.seg_model_id === run.seg_model_id && col.asr_model_id === curAsr;
                  if (!c?.score_id)
                    return (
                      <td key={col.asr_model_id} className="p-0.5 text-right">
                        {r.available ? (
                          <Button size="sm" disabled={runCell.isPending} onClick={() => runCell.mutate({ seg: r.seg_model_id, asr: col.asr_model_id, oracle: r.oracle })} aria-label={`Run ${r.name} → ${col.name}`}>
                            Run
                          </Button>
                        ) : (
                          <span className="text-[11px] text-subtle">—</span>
                        )}
                      </td>
                    );
                  const v = c.cpwer;
                  return (
                    <td key={col.asr_model_id} className="p-0.5">
                      <button type="button" aria-pressed={pick?.seg === r.seg_model_id && pick?.asr === col.asr_model_id} onClick={() => setPick({ seg: r.seg_model_id, asr: col.asr_model_id })} aria-label={`${r.name} → ${col.name}: ${v === null || v === undefined ? "n/a" : pct(v)}`} className={cx("w-full min-w-[96px] flex flex-col items-end px-2.5 py-1.5 rounded-md cursor-pointer border", v !== null && v === best ? "bg-good/15 border-good/50" : r.oracle ? "bg-secondary/15 border-transparent" : "bg-track/60 border-transparent hover:bg-track", isCur && "!border-focus")}>
                        <span className="num">{v === null || v === undefined ? "n/a" : pct(v)}</span>
                        {isCur && <span className="text-[10px] text-focus">this pipeline</span>}
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {sel && cpwer !== null && sel.cpwer !== null && sel.cpwer !== undefined && (
        <span className="flex flex-wrap items-baseline gap-2 text-[13px]">
          <span>{sel.label}</span>
          <span className={cx("num", deltaTone(sel.cpwer - cpwer))}>{deltaPts(sel.cpwer - cpwer)} pts against this pipeline</span>
          <span className="num text-xs text-muted">RTF {rtf(sel.rtf)}</span>
        </span>
      )}
      {oracleSame?.cpwer !== null && oracleSame?.cpwer !== undefined && cpwer !== null && (
        <span className="text-xs text-muted">
          <span className="text-fg">Perfect diarisation</span> would take this ASR to {pct(oracleSame.cpwer)} ({deltaPts(oracleSame.cpwer - cpwer)} pts), so {cpwer - oracleSame.cpwer > 0.02 ? "the diariser is where to look" : "the ASR is where most errors are"}.
        </span>
      )}
      <ErrorNote error={runCell.error} />
    </Panel>
  );
}

function Moments({ rows }: { rows: Summary["moments"] }) {
  const max = Math.max(0.01, ...rows.map((r) => r.rate));
  return (
    <Panel id="rg-h" title="Where errors start" subtitle="Reference words in each kind of moment, and the share the pipeline gets wrong, split by the stage that caused the errors. The bar length is that share." actions={<StageLegend />}>
      <div className="flex flex-col gap-1">
        <div className="grid grid-cols-[minmax(170px,1.2fr)_64px_minmax(0,2fr)_70px_120px] gap-3 text-[11px] text-muted uppercase tracking-wider px-2">
          <span>Moment</span>
          <span className="text-right">Words</span>
          <span>Errors by cause</span>
          <span className="text-right">Wrong</span>
          <span>Main cause</span>
        </div>
        {rows.map((r) => (
          <div key={r.key} className="grid grid-cols-[minmax(170px,1.2fr)_64px_minmax(0,2fr)_70px_120px] gap-3 items-center px-2 py-1.5 border-b border-track last:border-0">
            <span className="text-[13px]">{r.label}</span>
            <span className="num text-right">{num(r.words)}</span>
            <span className="flex h-3 rounded overflow-hidden" style={{ width: `${(r.rate / max) * 100}%` }}>
              {(["asr", "segmentation", "speaker"] as const).map((k) => (
                <span key={k} title={`${STAGE_LABELS[k]} ${pct(r.parts[k])}`} style={{ width: `${(r.parts[k] / (r.rate || 1)) * 100}%`, background: STAGE_COLORS[k] }} />
              ))}
            </span>
            <span className="num text-right">{pct(r.rate)}</span>
            <span className="flex items-center gap-1.5 text-xs">
              {r.main && <span className="size-2.5 rounded-sm" style={{ background: STAGE_COLORS[r.main] }} />}
              {r.main ? STAGE_LABELS[r.main] : "—"}
            </span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

function TaggedTranscript({ lines, refColors, ctl }: { lines: FileDetail["ref_lines"]; refColors: Record<string, string>; ctl: ReturnType<typeof useAudio> }) {
  const errs = lines.reduce((a, l) => a + l.words.filter((w) => w.cause).length, 0);
  return (
    <div className="flex flex-col gap-1.5">
      <span className="flex flex-wrap items-center gap-3">
        <span className="text-[11px] text-muted uppercase tracking-wider">Reference, every error tagged by its cause · {errs}</span>
        <StageLegend />
      </span>
      <div className="flex flex-col gap-1 max-h-[340px] overflow-y-auto scroll-thin pr-1">
        {lines.map((l, k) => (
          <button key={k} type="button" onClick={() => ctl.play(l.start, l.end)} className="text-left bg-transparent border border-transparent rounded-md px-2 py-1 cursor-pointer hover:bg-track">
            <span className="flex items-center gap-2 text-[11px]">
              <span className="size-2 rounded-sm" style={{ background: refColors[l.speaker] }} />
              <span className="num" style={{ color: refColors[l.speaker] }}>{l.speaker}</span>
              <span className="num text-subtle">{clock(l.start)}</span>
              {l.kinds.map((x) => (
                <span key={x} className="text-[10px] text-subtle border border-line rounded px-1">{x.replace("_", " ")}</span>
              ))}
            </span>
            <span className="text-[13px]">
              {l.words.map((w, i) => (
                <span key={i} title={w.cause ? `${CAUSE_LABEL[w.cause]} error` : undefined} className={cx("num mr-1", w.cause ? "underline decoration-2 underline-offset-4" : "text-fg")} style={w.cause ? { color: STAGE_COLORS[w.cause], textDecorationColor: STAGE_COLORS[w.cause] } : undefined}>
                  {w.w}
                </span>
              ))}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
