import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api, qs, type ErrorRegion, type Lane, type Page, type Run, type SliceReport } from "../../lib/api";
import { useConfig } from "../../lib/config";
import { ERROR_COLORS, ERROR_LABELS, SPEAKERS, speakerColors } from "../../lib/colors";
import { clock, deltaPts, duration, hwLabel, num, pct, rtf, speed } from "../../lib/format";
import { AudioPlayer, ClipButton, PlayFrom, useAudio } from "../../components/AudioPlayer";
import { Delta, MetricTile } from "../../components/MetricTile";
import { ExpandCell, SortableTable } from "../../components/SortableTable";
import { ErrorLegend, Timeline, type LaneGroup } from "../../components/Timeline";
import { lanesBySpeaker } from "../../components/timelineMath";
import * as I from "../../components/icons";
import { AnchorButton, Bar, Chip, cx, ErrorNote, PageHeader, Panel, Select, Spinner } from "../../components/ui";
import { Crumbs, latestRuns, RunPickers, Switcher, useDoneRuns, useSwitchLinks } from "./common";
import { SliceTable } from "./AsrLabels";

interface RegionRow {
  key: string;
  label: string;
  seconds: number;
  speech_s: number;
  error_s: number;
  share_speech: number | null;
  share_errors: number | null;
  lift: number | null;
  mix: Record<string, number>;
  der: number | null;
  worst: { file_id: number; file: string; start: number; end: number; dur: number; type: string }[];
}

interface Regions {
  rows: RegionRow[];
  speech_s: number;
  error_s: number;
}

const CONTEXT: Record<string, string> = { overlap: "overlap", near_change: "near a speaker change", short: "short turn", nonspeech: "non-speech" };

interface Summary {
  run: Run;
  score_id: number;
  collar: number;
  has_speakers: boolean;
  tiles: Record<string, number | null>;
  skip_overlap: boolean;
  compare: { run: Run; score_id: number; tiles: Record<string, number | null>; regions: Regions } | null;
  regions: Regions;
  speaker_counts: { rows: { label: number; cells: number[] }[]; cols: number[]; files: number; exact: number; under: number; over: number };
  conditions: { group: string; label: string; der: number | null; vad_error: number | null; files: number }[];
  turns_found: { label: string; turns: number; found: number; rate: number | null }[];
  boundaries: { edges: number[]; counts: number[]; n: number; median_abs: number | null };
}

interface FileRow {
  id: number;
  key: string;
  file_id: number;
  duration: number;
  der: number | null;
  missed: number | null;
  false_alarm: number | null;
  confusion: number | null;
  total: number | null;
  jer: number | null;
  ref_speakers: number | null;
  hyp_speakers: number | null;
  vad_error: number | null;
  vad_missed: number | null;
  vad_false_alarm: number | null;
  vad_total: number | null;
}

interface FileDetail extends FileRow {
  ref: Lane[];
  hyp: Lane[];
  mapping: Record<string, string>;
  errors: (ErrorRegion & { context?: string[] })[];
  overlaps: { start: number; end: number }[];
  largest: (ErrorRegion & { context?: string[] })[];
  speaker_matrix: { cols: string[]; rows: { speaker: string; talk_s: number; cells: number[]; missed_s: number }[]; mapping: Record<string, string>; notes: { kind: "merge" | "split"; hyp: string | string[]; ref: string[] }[] };
  speaker_labels: Record<string, Record<string, unknown>>;
  file_labels: Record<string, unknown>;
  label_url: string | null;
}

/** Reference lanes plus model lanes, coloured so a mapped model speaker shares its reference colour. */
export function speakerLaneGroups(ref: Lane[], hyp: Lane[], mapping: Record<string, string>, modelTitle = "Model"): LaneGroup[] {
  const refLanes = lanesBySpeaker(ref as [number, number, string][]);
  const colors = speakerColors(refLanes.map((l) => l.name));
  let extra = Object.keys(colors).length;
  const hypLanes = lanesBySpeaker(hyp.map(([s, e, mapped, orig]) => [s, e, orig ?? mapped] as [number, number, string]));
  return [
    { title: "Reference", lanes: refLanes.map((l) => ({ name: l.name, color: colors[l.name], segs: l.segs })) },
    {
      title: modelTitle,
      lanes: hypLanes.map((l) => {
        const to = mapping[l.name];
        const color = to && colors[to] ? colors[to] : SPEAKERS[extra++ % SPEAKERS.length];
        return { name: to ? `${l.name} → ${to}` : l.name, color, segs: l.segs };
      }),
    },
  ];
}

export function DiarErrors() {
  const { runId } = useParams();
  const [params, setParams] = useSearchParams();
  const { data: cfg } = useConfig();
  const collar = params.get("collar") ?? undefined;
  const compare = params.get("compare") ?? undefined;
  const skipOverlap = params.get("skip_overlap") === "1";
  const s = useQuery({
    queryKey: ["diar-summary", runId, collar, compare, skipOverlap],
    queryFn: () => api.get<Summary>(`/errors/diar/summary${qs({ run_id: runId, collar, compare_run_id: compare, skip_overlap: skipOverlap || undefined })}`),
    placeholderData: keepPreviousData,
  });
  const bySlice = useQuery({
    queryKey: ["slices", s.data?.score_id, s.data?.compare?.score_id],
    queryFn: () => api.get<SliceReport>(`/errors/slices${qs({ score_id: s.data!.score_id, compare_score_id: s.data!.compare?.score_id })}`),
    enabled: !!s.data,
  });
  const links = useSwitchLinks(s.data?.run.dataset_id, "");
  const others = latestRuns(useDoneRuns("seg").data ?? []).filter((r) => s.data && r.dataset_id === s.data.run.dataset_id && r.id !== s.data.run.id);
  const set = (k: string, v: string | undefined) => {
    const p = new URLSearchParams(params);
    if (v) p.set(k, v);
    else p.delete(k);
    setParams(p, { replace: true });
  };
  if (s.isLoading) return <Spinner />;
  if (s.error || !s.data) return <ErrorNote error={s.error} />;
  const d = s.data;
  const t = d.tiles;
  const c = d.compare?.tiles;
  const tiles = d.has_speakers
    ? [
        { key: "der", label: "DER", sub: "missed + false alarm + confusion" },
        { key: "missed", label: "Missed speech", color: ERROR_COLORS.missed, sub: "of reference speech" },
        { key: "false_alarm", label: "False alarm", color: ERROR_COLORS.false_alarm, sub: "of reference speech" },
        { key: "confusion", label: "Speaker confusion", color: ERROR_COLORS.confusion, sub: "of reference speech" },
        { key: "jer", label: "JER", sub: "mean per file" },
        { key: "speaker_count_acc", label: "Speaker count right", sub: "share of files", higher: true },
      ]
    : [
        { key: "vad_error", label: "VAD error", sub: "missed + false-alarm speech" },
        { key: "vad_missed", label: "Missed speech", color: ERROR_COLORS.missed, sub: "of reference speech" },
        { key: "vad_false_alarm", label: "False-alarm speech", color: ERROR_COLORS.false_alarm, sub: "of reference speech" },
      ];
  const maxCount = Math.max(1, ...d.boundaries.counts);

  return (
    <>
      <PageHeader
        crumbs={<Crumbs parts={[{ to: "/benchmarks/diar", label: "Benchmarks" }, "VAD / diarisation", d.run.dataset]} />}
        title={
          <span className="flex flex-wrap items-baseline gap-2">
            {d.run.label} <Chip>{d.has_speakers ? "speakers" : "speech only"}</Chip>
          </span>
        }
        actions={
          <RunPickers
            task="seg"
            run={d.run}
            path={(r) => `/errors/diar/${r.id}${qs({ collar })}`}
            extra={
              <>
                <Select label="Compare against" value={compare ?? ""} onChange={(e) => set("compare", e.target.value || undefined)}>
                  <option value="">—</option>
                  {others.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.label}
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
                <label className="flex items-center gap-2 text-xs text-muted cursor-pointer" title="Leave overlapped speech out of DER and JER">
                  <input type="checkbox" checked={!skipOverlap} onChange={(e) => set("skip_overlap", e.target.checked ? undefined : "1")} className="accent-[#06B6D4]" />
                  Score overlapping speech
                </label>
              </>
            }
          />
        }
      />
      <Switcher current="diar" links={links} />
      <div className="flex flex-wrap gap-3">
        {tiles.map((k) => (
          <MetricTile
            key={k.key}
            label={k.label}
            color={k.color}
            value={pct(t[k.key])}
            sub={c ? `vs ${d.compare!.run.label}: ${pct(c[k.key])}` : k.sub}
            delta={c && <Delta value={t[k.key]} compare={c[k.key]} lowerIsBetter={!k.higher} format={(x) => deltaPts(x).replace(/^[+−]/, "")} />}
          />
        ))}
      </div>
      <ComputeStrip run={d.run} compare={d.compare?.run} />
      <WhereErrors regions={d.regions} compare={d.compare?.regions} compareName={d.compare?.run.label} />
      {d.has_speakers && (
        <div className="grid gap-5 lg:grid-cols-2">
          <SpeakerCounts sc={d.speaker_counts} />
          <Confusion scoreId={d.score_id} />
        </div>
      )}
      {bySlice.data && (bySlice.data.labels.some((l) => l.kind !== "unused") ? (
        <Panel id="dlbl-h" title={`Where ${d.run.label} fails, by label`} subtitle={`File labels. Diarisation is scored per file, so intervals are resampled by file and are wider than for ASR; fewer than ${bySlice.data.min_units} files is too few to call.`}>
          <SliceTable rows={bySlice.data.finder ?? []} overall={bySlice.data.overall.value} low cmpName={d.compare?.run.label} unit="Files" />
        </Panel>
      ) : null)}
      <Panel id="files-h" title="Files" subtitle="Sort by any column; open a file for speaker lanes, the error lane and the largest errors." actions={<ErrorLegend />}>
        <FileTable scoreId={d.score_id} hasSpeakers={d.has_speakers} modelName={d.run.label} />
      </Panel>
      <div className="grid gap-5 lg:grid-cols-3">
        <Panel title="Error by condition" subtitle="From file metadata">
          {d.conditions.length === 0 && <span className="text-[13px] text-muted">No conditions in the annotations' metadata.</span>}
          {d.conditions.map((x) => {
            const v = d.has_speakers ? x.der : x.vad_error;
            return (
              <div key={`${x.group}-${x.label}`} className="grid grid-cols-[minmax(0,1fr)_90px_56px] gap-2 items-center text-[13px]">
                <span className="truncate">
                  <span className="text-subtle text-[11px]">{x.group}</span> {x.label} <span className="text-subtle num text-[11px]">({x.files})</span>
                </span>
                <Bar value={v} max={Math.max(0.01, ...d.conditions.map((y) => (d.has_speakers ? y.der : y.vad_error) ?? 0))} color="var(--color-secondary)" />
                <span className="num text-right">{pct(v)}</span>
              </div>
            );
          })}
        </Panel>
        <Panel title="Turns found, by length" subtitle="A reference turn counts as found when its mapped model speaker covers half of it">
          {!d.has_speakers && <span className="text-[13px] text-muted">Speech-only models have no speaker turns.</span>}
          {d.has_speakers &&
            d.turns_found.map((x) => (
              <div key={x.label} className="grid grid-cols-[64px_minmax(0,1fr)_88px] gap-2 items-center text-[13px]">
                <span className="num">{x.label}</span>
                <Bar value={x.rate} color="var(--color-good)" />
                <span className="num text-right text-muted">
                  {pct(x.rate, 0)} <span className="text-subtle">of {x.turns}</span>
                </span>
              </div>
            ))}
        </Panel>
        <Panel title="Boundary accuracy" subtitle={`Model boundary minus nearest reference boundary · median |offset| ${d.boundaries.median_abs !== null ? `${(d.boundaries.median_abs * 1000).toFixed(0)} ms` : "—"}`}>
          <div className="flex items-end gap-0.5 h-28" role="img" aria-label="Boundary offset histogram">
            {d.boundaries.counts.map((n, i) => (
              <span key={i} className="flex-1 rounded-t-sm" style={{ height: `${(n / maxCount) * 100}%`, background: Math.abs(d.boundaries.edges[i] + 0.05) < 0.11 ? "var(--color-good)" : "var(--color-secondary)" }} title={`${d.boundaries.edges[i].toFixed(1)}…${d.boundaries.edges[i + 1].toFixed(1)} s: ${n}`} />
            ))}
          </div>
          <div className="flex justify-between num text-[10px] text-subtle">
            <span>−1.0 s</span>
            <span>0</span>
            <span>+1.0 s</span>
          </div>
          <span className="text-[11px] text-subtle num">{num(d.boundaries.n)} boundaries within ±1 s</span>
        </Panel>
      </div>
    </>
  );
}

function FileTable({ scoreId, hasSpeakers, modelName }: { scoreId: number; hasSpeakers: boolean; modelName: string }) {
  const [open, setOpen] = useState<string | number | null>(null);
  const columns = useMemo<ColumnDef<FileRow, unknown>[]>(() => {
    const p = (k: keyof FileRow, header: string, color?: string): ColumnDef<FileRow, unknown> => ({
      id: k,
      header,
      meta: { align: "right" },
      cell: ({ row }) => {
        const v = row.original[k] as number | null;
        const tot = ["missed", "false_alarm", "confusion"].includes(k) ? row.original.total : ["vad_missed", "vad_false_alarm"].includes(k) ? row.original.vad_total : null;
        const shown = tot ? (v ?? 0) / tot : v;
        return <span className="num" style={color && shown ? { color } : undefined}>{pct(shown)}</span>;
      },
    });
    const cols: ColumnDef<FileRow, unknown>[] = [
      { id: "key", header: "File", cell: ({ row }) => <ExpandCell open={open === row.original.id} label={row.original.key} /> },
      { id: "duration", header: "Duration", meta: { align: "right", sortKey: "duration" }, cell: ({ row }) => <span className="num">{clock(row.original.duration, false)}</span> },
    ];
    if (hasSpeakers) {
      cols.push(p("der", "DER"), p("missed", "Missed", ERROR_COLORS.missed), p("false_alarm", "False alarm", ERROR_COLORS.false_alarm), p("confusion", "Confusion", ERROR_COLORS.confusion), p("jer", "JER"));
      cols.push({
        id: "hyp_speakers",
        header: "Speakers",
        meta: { align: "right" },
        cell: ({ row }) => (
          <span className={row.original.hyp_speakers === row.original.ref_speakers ? "num" : "num text-warn"}>
            {row.original.hyp_speakers} / {row.original.ref_speakers}
          </span>
        ),
      });
    } else {
      cols.push(p("vad_error", "VAD error"), p("vad_missed", "Missed", ERROR_COLORS.missed), p("vad_false_alarm", "False alarm", ERROR_COLORS.false_alarm));
    }
    return cols;
  }, [open, hasSpeakers]);
  return (
    <SortableTable<FileRow>
      columns={columns}
      caption="Files"
      queryKey={["diar-files", scoreId]}
      fetchPage={({ offset, limit, sort, order }) => api.get<Page<FileRow>>(`/errors/diar/files${qs({ score_id: scoreId, offset, limit, sort, order })}`)}
      initialSort={{ id: hasSpeakers ? "der" : "vad_error", desc: true }}
      rowKey={(r) => r.id}
      expanded={open}
      onExpand={setOpen}
      renderExpanded={(r) => <FileDetailView id={r.id} modelName={modelName} />}
      maxHeight="80vh"
    />
  );
}

function FileDetailView({ id, modelName }: { id: number; modelName: string }) {
  const q = useQuery({ queryKey: ["diar-file", id], queryFn: () => api.get<FileDetail>(`/errors/diar/files/${id}`) });
  const ctl = useAudio(q.data?.file_id);
  if (!q.data) return <div className="p-4"><Spinner /></div>;
  const f = q.data;
  const groups = speakerLaneGroups(f.ref, f.hyp, f.mapping, modelName);
  return (
    <div className="p-4 grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      <div className="flex flex-col gap-3 bg-panel border border-track rounded-md p-3 min-w-0">
        <AudioPlayer ctl={ctl} start={0} end={f.duration} errorSpans={f.largest.map((e) => [e.start, e.end])} label="Play file" />
        <Timeline fileId={f.file_id} t0={0} t1={f.duration} groups={groups} errors={f.errors} overlaps={f.overlaps} time={ctl.time} onSeek={(t) => ctl.play(t)} />
        {f.label_url && (
          <span className="self-start">
            <AnchorButton href={f.label_url} newTab>
              <I.Pen size={13} /> Open in Label
            </AnchorButton>
          </span>
        )}
      </div>
      <div className="flex flex-col gap-4">
        {Object.keys(f.mapping).length > 0 && (
          <div className="flex flex-col gap-1.5">
            <span className="text-[11px] text-muted uppercase tracking-wider">Speaker mapping</span>
            {Object.entries(f.mapping).map(([h, r]) => (
              <span key={h} className="num text-[13px]">
                {h} → {r}
              </span>
            ))}
            {groups[1].lanes.filter((l) => !l.name.includes("→")).map((l) => (
              <span key={l.name} className="num text-[13px] text-warn">
                {l.name} → (unmapped: extra speaker)
              </span>
            ))}
          </div>
        )}
        <div className="flex flex-col gap-1.5">
          <span className="text-[11px] text-muted uppercase tracking-wider">Largest errors</span>
          {f.largest.length === 0 && <span className="text-[13px] text-muted">No errors.</span>}
          {f.largest.map((e, k) => (
            <div key={k} className="flex items-center gap-3 text-[13px]">
              <PlayFrom ctl={ctl} t={e.start - 0.3} until={e.end + 0.3} label={`Play error ${k + 1}`} />
              <span className="num text-xs text-muted w-[84px]">{clock(e.start)}</span>
              <span className="flex items-center gap-1.5 w-36 whitespace-nowrap">
                <span className="size-2.5 rounded-sm" style={{ background: ERROR_COLORS[e.type] }} />
                {ERROR_LABELS[e.type]}
              </span>
              <span className="num text-muted">{e.dur.toFixed(2)} s</span>
              {(e.context ?? []).map((c) => (
                <Chip key={c}>{CONTEXT[c] ?? c}</Chip>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function ComputeStrip({ run, compare }: { run: Run; compare?: Run }) {
  const h = Object.values(run.hardware ?? {}).find((x) => x.device);
  const r = run.rtf?.total ?? null;
  const ch = compare ? Object.values(compare.hardware ?? {}).find((x) => x.device) : undefined;
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 bg-panel border border-track rounded-lg px-4 py-2.5 text-xs">
      <span className="flex items-center gap-1.5 text-muted">
        <I.Cpu size={14} /> Compute
      </span>
      <span className="num">{hwLabel(h)}{h?.vram_gb ? ` · ${h.vram_gb} GB declared VRAM` : ""}{h?.peak_vram_gb ? ` · ${h.peak_vram_gb.toFixed(1)} GB peak` : ""}</span>
      <span className="num">RTF {rtf(r)} · {speed(r)}</span>
      {run.wall_s !== null && <span className="num">{duration(run.wall_s)}{run.rtf?.audio_s ? ` for ${duration(run.rtf.audio_s)} of audio` : ""}</span>}
      <span className="num text-subtle">cold start not in RTF</span>
      {compare && <span className="num text-muted">{compare.label}: {hwLabel(ch)} · RTF {rtf(compare.rtf?.total)}</span>}
    </div>
  );
}

function WhereErrors({ regions, compare, compareName }: { regions: Regions; compare?: Regions; compareName?: string }) {
  const [open, setOpen] = useState<string | null>(null);
  const rows = regions.rows.filter((r) => r.seconds > 0);
  const cur = rows.find((r) => r.key === open) ?? rows.slice().sort((a, b) => (b.lift ?? 0) - (a.lift ?? 0))[0];
  if (!rows.length) return null;
  return (
    <Panel
      id="where-h"
      title="Where the errors are"
      subtitle={`Each kind of moment's share of reference speech against its share of error time (${duration(regions.error_s)} of errors in ${duration(regions.speech_s)} of speech). Lift above 1 means errors gather there. Kinds overlap.`}
      actions={
        <span className="flex flex-wrap gap-3 text-[11px] text-muted">
          <span className="flex items-center gap-1.5"><span className="h-1.5 w-4 rounded bg-secondary" />Share of speech</span>
          <span className="flex items-center gap-1.5"><span className="h-1.5 w-4 rounded bg-bad" />Share of errors</span>
          {(["missed", "false_alarm", "confusion"] as const).map((k) => (
            <span key={k} className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm" style={{ background: ERROR_COLORS[k] }} />{ERROR_LABELS[k]}</span>
          ))}
        </span>
      }
    >
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <div className="flex flex-col gap-1 min-w-0">
          <div className="grid grid-cols-[minmax(150px,1.2fr)_minmax(140px,1.4fr)_56px_100px_90px] gap-3 text-[11px] text-muted uppercase tracking-wider px-2">
            <span>Moment</span>
            <span>Speech vs errors</span>
            <span className="text-right">Lift</span>
            <span>Error mix</span>
            <span className="text-right">DER here</span>
          </div>
          {rows.map((r) => {
            const c = compare?.rows.find((x) => x.key === r.key);
            return (
              <button key={r.key} type="button" aria-pressed={cur?.key === r.key} onClick={() => setOpen(r.key)} className={cx("grid grid-cols-[minmax(150px,1.2fr)_minmax(140px,1.4fr)_56px_100px_90px] gap-3 items-center px-2 py-2 rounded-md cursor-pointer border text-left", cur?.key === r.key ? "border-focus bg-active/50" : "border-transparent bg-transparent hover:bg-track")}>
                <span className="text-[13px]">{r.label}</span>
                <span className="flex flex-col gap-1">
                  <span className="flex items-center gap-2"><Bar value={r.share_speech} color="var(--color-secondary)" className="flex-1" /><span className="num text-[11px] w-10 text-right">{pct(r.share_speech, 0)}</span></span>
                  <span className="flex items-center gap-2"><Bar value={r.share_errors} color="var(--color-bad)" className="flex-1" /><span className="num text-[11px] w-10 text-right">{pct(r.share_errors, 0)}</span></span>
                </span>
                <span className={cx("num text-right", (r.lift ?? 0) > 1.2 ? "text-bad" : "text-muted")}>{r.lift === null ? "—" : `${r.lift.toFixed(1)}×`}</span>
                <span className="flex h-2 rounded overflow-hidden bg-track">
                  {(["missed", "false_alarm", "confusion"] as const).map((k) => (
                    <span key={k} style={{ width: `${(r.mix[k] ?? 0) * 100}%`, background: ERROR_COLORS[k] }} />
                  ))}
                </span>
                <span className="flex flex-col items-end">
                  <span className="num">{r.key === "nonspeech" ? `${duration(r.error_s)} FA` : pct(r.der)}</span>
                  {c && <span className="num text-[11px] text-muted">{compareName?.split(" ")[0]} {c.key === "nonspeech" ? duration(c.error_s) : pct(c.der)}</span>}
                </span>
              </button>
            );
          })}
        </div>
        <div className="flex flex-col gap-2">
          <span className="text-[11px] text-muted uppercase tracking-wider">Worst moments · {cur?.label}</span>
          {(cur?.worst ?? []).length === 0 && <span className="text-[13px] text-muted">No errors here.</span>}
          {(cur?.worst ?? []).map((m, k) => (
            <div key={k} className="flex items-center gap-3 text-[13px]">
              <ClipButton fileId={m.file_id} start={Math.max(0, m.start - 0.5)} end={m.end + 0.5} label={`Play ${m.file} at ${clock(m.start)}`} />
              <span className="flex flex-col min-w-0">
                <span className="flex items-center gap-1.5">
                  <span className="size-2.5 rounded-sm" style={{ background: ERROR_COLORS[m.type] }} />
                  {ERROR_LABELS[m.type]} <span className="num text-muted">{m.dur.toFixed(2)} s</span>
                </span>
                <span className="num text-[11px] text-subtle truncate">{m.file} · {clock(m.start)}</span>
              </span>
            </div>
          ))}
        </div>
      </div>
    </Panel>
  );
}

function SpeakerCounts({ sc }: { sc: Summary["speaker_counts"] }) {
  if (!sc.files) return null;
  const max = Math.max(1, ...sc.rows.flatMap((r) => r.cells));
  return (
    <Panel id="cnt-h" title="How many speakers it finds" subtitle="Files by true speaker count (rows) and the count the model found (columns)">
      <table className="border-collapse text-[13px] self-start">
        <thead>
          <tr>
            <th scope="col" className="text-left text-[11px] text-muted font-medium px-2 py-1">True ╲ found</th>
            {sc.cols.map((c) => (
              <th key={c} scope="col" className="num text-[11px] text-muted font-medium px-3 py-1">{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sc.rows.map((r) => (
            <tr key={r.label}>
              <th scope="row" className="num font-normal text-left px-2 py-1">{r.label}</th>
              {r.cells.map((n, i) => (
                <td key={i} className="p-0.5">
                  <span className={cx("num flex items-center justify-center w-12 h-8 rounded", sc.cols[i] === r.label && n ? "border border-good/60" : "")} style={{ background: n ? `rgba(99,102,241,${(0.15 + (n / max) * 0.55).toFixed(2)})` : "rgba(30,41,59,0.5)" }}>
                    {n || ""}
                  </span>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <span className="text-xs text-muted">
        Exact in <span className="num">{sc.exact} of {sc.files}</span> files. Under-counting (<span className="num">{sc.under}</span>) {sc.under > sc.over ? "is more common than" : sc.under < sc.over ? "is less common than" : "is as common as"} over-counting (<span className="num">{sc.over}</span>){sc.under > sc.over ? ": speakers get merged, not split." : "."}
      </span>
    </Panel>
  );
}

function Confusion({ scoreId }: { scoreId: number }) {
  const files = useQuery({ queryKey: ["diar-files-all", scoreId], queryFn: () => api.get<Page<FileRow>>(`/errors/diar/files${qs({ score_id: scoreId, sort: "der", order: "desc", limit: 200 })}`) });
  const [fid, setFid] = useState<number | null>(null);
  const id = fid ?? files.data?.rows[0]?.id;
  const f = useQuery({ queryKey: ["diar-file", id], queryFn: () => api.get<FileDetail>(`/errors/diar/files/${id}`), enabled: !!id });
  const cm = f.data?.speaker_matrix;
  const colors = speakerColors(cm?.rows.map((r) => r.speaker) ?? []);
  return (
    <Panel
      id="cm-h"
      title="Who was mistaken for whom"
      subtitle="Seconds of each reference speaker's speech, by the model speaker it went to"
      actions={
        <Select label="File" value={id ?? ""} onChange={(e) => setFid(Number(e.target.value))}>
          {(files.data?.rows ?? []).map((r) => (
            <option key={r.id} value={r.id}>
              {r.key} · DER {pct(r.der)}
            </option>
          ))}
        </Select>
      }
    >
      {!cm ? (
        <Spinner />
      ) : (
        <>
          <div className="overflow-x-auto scroll-thin">
            <table className="border-collapse text-[12px]">
              <thead>
                <tr>
                  <th scope="col" className="text-left text-[11px] text-muted font-medium px-2 py-1">Reference speaker</th>
                  {cm.cols.map((c) => (
                    <th key={c} scope="col" className="num text-[11px] text-muted font-medium px-2 py-1">{c}{cm.mapping[c] ? ` → ${cm.mapping[c]}` : ""}</th>
                  ))}
                  <th scope="col" className="num text-[11px] text-muted font-medium px-2 py-1">missed</th>
                </tr>
              </thead>
              <tbody>
                {cm.rows.map((r) => {
                  const tags = f.data?.speaker_labels?.[r.speaker] ?? {};
                  return (
                    <tr key={r.speaker}>
                      <th scope="row" className="text-left font-normal px-2 py-1">
                        <span className="flex flex-col">
                          <span className="flex items-center gap-1.5">
                            <span className="size-2 rounded-sm" style={{ background: colors[r.speaker] }} />
                            <span className="num">{r.speaker}</span>
                            <span className="num text-[11px] text-subtle">{r.talk_s.toFixed(1)} s</span>
                          </span>
                          {Object.keys(tags).length > 0 && (
                            <span className="flex flex-wrap gap-1">
                              {Object.entries(tags).slice(0, 3).map(([k, v]) => (
                                <span key={k} className="text-[10px] text-muted border border-line rounded px-1">{String(v)}</span>
                              ))}
                            </span>
                          )}
                        </span>
                      </th>
                      {r.cells.map((v, i) => {
                        const share = r.talk_s ? v / r.talk_s : 0;
                        const right = cm.mapping[cm.cols[i]] === r.speaker;
                        return (
                          <td key={i} className="p-0.5" title={`${r.speaker} → ${cm.cols[i]}: ${v.toFixed(1)} s (${pct(share, 0)})`}>
                            <span className="num flex items-center justify-center min-w-14 h-7 rounded px-1" style={{ background: v ? (right ? `rgba(16,185,129,${(0.12 + share * 0.5).toFixed(2)})` : `rgba(168,85,247,${(0.12 + share * 0.6).toFixed(2)})`) : "rgba(30,41,59,0.5)" }}>
                              {v ? v.toFixed(1) : ""}
                            </span>
                          </td>
                        );
                      })}
                      <td className="p-0.5"><span className="num flex items-center justify-center min-w-14 h-7 rounded px-1" style={{ background: r.missed_s ? "rgba(244,63,94,0.25)" : "rgba(30,41,59,0.5)" }}>{r.missed_s ? r.missed_s.toFixed(1) : ""}</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {cm.notes.map((n, k) => (
            <div key={k} role="note" className="flex items-start gap-2 text-xs text-muted">
              <I.Info size={14} />
              <span>
                <span className="text-fg">{n.kind === "merge" ? "Merge:" : "Split:"}</span>{" "}
                {n.kind === "merge" ? `most of ${n.ref.join(" and ")}'s speech went to ${String(n.hyp)}, so the model heard one voice.` : `${n.ref[0]}'s speech is spread over ${(n.hyp as string[]).join(" and ")}.`}
              </span>
            </div>
          ))}
        </>
      )}
    </Panel>
  );
}
