import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api, qs, type ErrorRegion, type Lane, type Page, type Run } from "../../lib/api";
import { useConfig } from "../../lib/config";
import { ERROR_COLORS, ERROR_LABELS, SPEAKERS, speakerColors } from "../../lib/colors";
import { clock, deltaPts, num, pct } from "../../lib/format";
import { AudioPlayer, PlayFrom, useAudio } from "../../components/AudioPlayer";
import { Delta, MetricTile } from "../../components/MetricTile";
import { ExpandCell, SortableTable } from "../../components/SortableTable";
import { ErrorLegend, Timeline, type LaneGroup } from "../../components/Timeline";
import { lanesBySpeaker } from "../../components/timelineMath";
import { Bar, Chip, ErrorNote, PageHeader, Panel, Select, Spinner } from "../../components/ui";
import { Crumbs, latestRuns, RunPickers, Switcher, useDoneRuns, useSwitchLinks } from "./common";

interface Summary {
  run: Run;
  score_id: number;
  collar: number;
  has_speakers: boolean;
  tiles: Record<string, number | null>;
  compare: { run: Run; tiles: Record<string, number | null> } | null;
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
  errors: ErrorRegion[];
  overlaps: { start: number; end: number }[];
  largest: ErrorRegion[];
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
  const s = useQuery({
    queryKey: ["diar-summary", runId, collar, compare],
    queryFn: () => api.get<Summary>(`/errors/diar/summary${qs({ run_id: runId, collar, compare_run_id: compare })}`),
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
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
