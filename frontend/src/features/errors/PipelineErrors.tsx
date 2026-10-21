import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, qs, type AlignOp, type Attribution, type ErrorRegion, type Lane, type Page, type Run } from "../../lib/api";
import { useConfig, useNormaliser, useNormalisers } from "../../lib/config";
import { STAGE_COLORS, STAGE_LABELS, speakerColors } from "../../lib/colors";
import { clock, num, pct, rtf } from "../../lib/format";
import { AudioPlayer, useAudio } from "../../components/AudioPlayer";
import { DiffText } from "../../components/DiffText";
import { MetricTile } from "../../components/MetricTile";
import { ExpandCell, SortableTable } from "../../components/SortableTable";
import { StageLegend } from "../../components/StageLegend";
import { ErrorLegend, Timeline } from "../../components/Timeline";
import { cx, Empty, ErrorNote, PageHeader, Panel, Select, Spinner } from "../../components/ui";
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
  tiles: { cpwer: number | null; wer: number | null; der: number | null; attribution: Attribution | null; cp_errors: number | null; n: number | null };
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
  const { data: norms } = useNormalisers();
  const [normaliser, setNormaliser] = useNormaliser();
  const collar = params.get("collar") ?? undefined;
  const s = useQuery({
    queryKey: ["pipe-summary", runId, normaliser, collar],
    queryFn: () => api.get<Summary>(`/errors/pipeline/summary${qs({ run_id: runId, normaliser, collar })}`),
  });
  const links = useSwitchLinks(s.data?.run.dataset_id, "");
  const set = (k: string, v: string) => {
    const p = new URLSearchParams(params);
    p.set(k, v);
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
                <Select label="Normaliser" value={normaliser} onChange={(e) => setNormaliser(e.target.value)}>
                  {(norms ?? []).map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.label}
                    </option>
                  ))}
                </Select>
                <Select label="Collar" value={String(d.collar)} onChange={(e) => set("collar", e.target.value)}>
                  {(cfg?.collars ?? [0, 0.25]).map((x) => (
                    <option key={x} value={x}>
                      ±{x} s
                    </option>
                  ))}
                </Select>
              </>
            }
          />
        }
      />
      <Switcher current="pipeline" links={links} />
      <div className="flex flex-wrap gap-3">
        <MetricTile label="cpWER" value={d.tiles.cpwer === null ? "n/a" : pct(d.tiles.cpwer)} sub={d.tiles.cpwer === null ? "speech-only stage: no speakers" : `${num(d.tiles.cp_errors)} errors / ${num(d.tiles.n)} words`} />
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
      <Panel id="pf-h" title="Files" subtitle="Sort by any column. Open a file to compare speaker lanes and the speaker-attributed transcript." actions={<ErrorLegend />}>
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
        </div>
        <Timeline fileId={f.file_id} t0={0} t1={f.duration} groups={groups} errors={f.errors} overlaps={f.overlaps} time={ctl.time} onSeek={(t) => ctl.play(t)} />
      </div>
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
