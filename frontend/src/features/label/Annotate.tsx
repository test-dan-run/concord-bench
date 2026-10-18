// Annotate (annotation only, no metrics): whole-file timeline, segment grid with multi-select and a
// bulk bar, inline transcript editing with autosave per segment, and the AeroAudio shortcuts:
// Space play/pause · Tab next low-confidence · 1–8 assign speaker · V split · M merge · Enter confirm.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, type AnnotateView, type LabelFile, type SegStatus, type Segment } from "../../lib/api";
import { C, speakerColors } from "../../lib/colors";
import { clock, num } from "../../lib/format";
import { IfFeature } from "../../lib/config";
import { confidenceColor, cycleSpeaker, nextLowConfidence, overlapRegions, selectRange, STATUS_LABELS } from "../../lib/label";
import { AudioPlayer, PlayFrom, useAudio } from "../../components/AudioPlayer";
import { Timeline, type LaneGroup } from "../../components/Timeline";
import * as I from "../../components/icons";
import { Button, Chip, cx, Empty, ErrorNote, PageHeader, Panel, Select, Spinner } from "../../components/ui";
import { Crumbs, DatasetChooser, LabelTabs } from "./common";

type Show = "all" | SegStatus;
const SHOW: { value: Show; label: string }[] = [
  { value: "all", label: "All" },
  { value: "pseudo", label: "Needs review" },
  { value: "auto", label: "Auto-accepted" },
  { value: "rejected", label: "Rejected" },
  { value: "confirmed", label: "Confirmed" },
];
const STATUS_TONE: Record<SegStatus, "good" | "warn" | "bad" | "info" | "default"> = { confirmed: "good", auto: "info", pseudo: "warn", rejected: "bad" };

/** `/label/file/:fileId` (from error analysis): find the file's dataset, then open the annotator on it. */
export function OpenFile() {
  const { fileId } = useParams();
  const [params] = useSearchParams();
  const q = useQuery({ queryKey: ["audio-info", fileId], queryFn: () => api.get<{ dataset_id: number }>(`/audio/${fileId}/info`) });
  if (q.error) return <ErrorNote error={q.error} />;
  if (!q.data) return <Spinner />;
  const seg = params.get("segment");
  return <Navigate replace to={`/label/annotate/${q.data.dataset_id}?file=${fileId}${seg ? `&segment=${seg}` : ""}`} />;
}

export function AnnotatePage() {
  const { datasetId } = useParams();
  if (!datasetId) {
    return <DatasetChooser title="Annotate" to={(id) => `/label/annotate/${id}`} subtitle="Review and correct segments: transcript, speakers, boundaries." />;
  }
  return <AnnotateDataset datasetId={Number(datasetId)} />;
}

function AnnotateDataset({ datasetId }: { datasetId: number }) {
  const [params, setParams] = useSearchParams();
  const files = useQuery({ queryKey: ["label-files", datasetId], queryFn: () => api.get<LabelFile[]>(`/label/datasets/${datasetId}/files`) });
  const status = params.get("status") as SegStatus | null;
  const fileParam = params.get("file");
  const fileId = useMemo(() => {
    if (fileParam) return Number(fileParam);
    const list = (files.data ?? []).filter((f) => f.usable);
    const key = status === "pseudo" ? "review" : status;
    const hit = key ? list.find((f) => f.counts[key as keyof LabelFile["counts"]] > 0) : undefined;
    return (hit ?? list[0])?.id ?? null;
  }, [fileParam, files.data, status]);
  const go = (id: number) => {
    const p = new URLSearchParams(params);
    p.set("file", String(id));
    p.delete("segment");
    setParams(p);
  };
  if (files.isLoading) return <Spinner />;
  if (files.error) return <ErrorNote error={files.error} />;
  if (fileId === null) {
    return (
      <>
        <PageHeader crumbs={<Crumbs />} title="Annotate" />
        <LabelTabs datasetId={datasetId} current="annotate" />
        <Empty>This dataset has no playable files.</Empty>
      </>
    );
  }
  return <Annotator key={fileId} datasetId={datasetId} fileId={fileId} files={files.data ?? []} onFile={go} initialSegment={Number(params.get("segment")) || null} initialShow={status ?? "all"} />;
}

function useLabelMutations(fileId: number, datasetId: number) {
  const qc = useQueryClient();
  const done = () => {
    void qc.invalidateQueries({ queryKey: ["annotate", fileId] });
    void qc.invalidateQueries({ queryKey: ["label-files", datasetId] });
    void qc.invalidateQueries({ queryKey: ["label-console"] });
    void qc.invalidateQueries({ queryKey: ["label-results", datasetId] });
  };
  const patch = useMutation({ mutationFn: ({ id, ...body }: { id: number } & Partial<Segment>) => api.patch<Segment>(`/label/segments/${id}`, body), onSuccess: done });
  const bulk = useMutation({ mutationFn: (b: { ids: number[]; action: string; speaker?: string | null }) => api.post(`/label/segments/bulk`, b), onSuccess: done });
  const merge = useMutation({ mutationFn: (ids: number[]) => api.post<Segment>(`/label/segments/merge`, { ids }), onSuccess: done });
  const split = useMutation({ mutationFn: ({ id, t }: { id: number; t: number }) => api.post<Segment[]>(`/label/segments/${id}/split`, { t }), onSuccess: done });
  const create = useMutation({ mutationFn: (b: { start: number; end: number; speaker: string | null }) => api.post<Segment>(`/label/files/${fileId}/segments`, b), onSuccess: done });
  const remove = useMutation({ mutationFn: (id: number) => api.del(`/label/segments/${id}`), onSuccess: done });
  const confirmAuto = useMutation({ mutationFn: () => api.post<{ confirmed: number }>(`/label/files/${fileId}/confirm-auto`), onSuccess: done });
  const error = patch.error ?? bulk.error ?? merge.error ?? split.error ?? create.error ?? remove.error ?? confirmAuto.error;
  return { patch, bulk, merge, split, create, remove, confirmAuto, error };
}

function isTyping(e: KeyboardEvent): boolean {
  const t = e.target as HTMLElement | null;
  return !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
}

function Annotator({
  datasetId,
  fileId,
  files,
  onFile,
  initialSegment,
  initialShow,
}: {
  datasetId: number;
  fileId: number;
  files: LabelFile[];
  onFile: (id: number) => void;
  initialSegment: number | null;
  initialShow: Show;
}) {
  const navigate = useNavigate();
  const q = useQuery({ queryKey: ["annotate", fileId], queryFn: () => api.get<AnnotateView>(`/label/files/${fileId}`) });
  const m = useLabelMutations(fileId, datasetId);
  const ctl = useAudio(fileId);
  const [cur, setCur] = useState<number | null>(initialSegment);
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [anchor, setAnchor] = useState<number | null>(null);
  const [show, setShow] = useState<Show>(initialShow);
  const rowRefs = useRef(new Map<number, HTMLTableRowElement>());

  const v = q.data;
  const segs = useMemo(() => v?.segments ?? [], [v]);
  const visible = useMemo(() => (show === "all" ? segs : segs.filter((s) => s.status === show)), [segs, show]);
  const speakers = v?.speakers ?? [];
  const colors = useMemo(() => speakerColors(speakers), [speakers]);
  const high = v?.thresholds.high ?? 0.85;
  const current = segs.find((s) => s.id === cur) ?? null;

  // Pick a starting segment once loaded: the requested one, else the first low-confidence one.
  useEffect(() => {
    if (!v || (cur !== null && segs.some((s) => s.id === cur))) return;
    setCur(nextLowConfidence(segs, null, high) ?? segs[0]?.id ?? null);
  }, [v, segs, cur, high]);
  useEffect(() => {
    if (cur !== null) rowRefs.current.get(cur)?.scrollIntoView({ block: "nearest" });
  }, [cur]);

  const targets = useCallback((): number[] => (sel.size ? [...sel] : cur !== null ? [cur] : []), [sel, cur]);
  const speakerForKey = (k: number) => speakers[k - 1] ?? `spk${k - 1}`;
  const nextId = (id: number) => {
    const k = segs.findIndex((s) => s.id === id);
    return segs[k + 1]?.id ?? null;
  };

  const confirm = useCallback(() => {
    const ids = targets();
    if (!ids.length) return;
    m.bulk.mutate({ ids, action: "confirm" });
    if (!sel.size && cur !== null) setCur(nextLowConfidence(segs, cur, high) ?? nextId(cur) ?? cur);
    setSel(new Set());
  }, [targets, sel, cur, segs, high]);

  const splitAtPlayhead = useCallback(() => {
    if (!current) return;
    const inside = ctl.time > current.start + 0.05 && ctl.time < current.end - 0.05;
    m.split.mutate({ id: current.id, t: inside ? ctl.time : (current.start + current.end) / 2 });
  }, [current, ctl.time, m.split]);

  const mergeSel = useCallback(() => {
    if (sel.size >= 2) {
      m.merge.mutate([...sel]);
      setSel(new Set());
    } else if (cur !== null && nextId(cur) !== null) {
      m.merge.mutate([cur, nextId(cur)!]);
    }
  }, [sel, cur, segs, m.merge]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === " ") {
        e.preventDefault();
        if (ctl.playing) ctl.pause();
        else if (current) ctl.play(ctl.time > current.start && ctl.time < current.end - 0.05 ? ctl.time : current.start, current.end);
        else ctl.play();
      } else if (e.key === "Tab") {
        e.preventDefault();
        const n = nextLowConfidence(segs, cur, high);
        if (n !== null) setCur(n);
      } else if (/^[1-8]$/.test(e.key)) {
        const ids = targets();
        if (ids.length) m.bulk.mutate({ ids, action: "assign_speaker", speaker: speakerForKey(Number(e.key)) });
      } else if (e.key === "v" || e.key === "V") {
        splitAtPlayhead();
      } else if (e.key === "m" || e.key === "M") {
        mergeSel();
      } else if (e.key === "Enter") {
        e.preventDefault();
        confirm();
      } else if (e.key === "Escape") {
        setSel(new Set());
      } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const k = visible.findIndex((s) => s.id === cur);
        const n = visible[Math.max(0, Math.min(visible.length - 1, k + (e.key === "ArrowDown" ? 1 : -1)))];
        if (n) setCur(n.id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ctl, current, segs, visible, cur, high, targets, splitAtPlayhead, mergeSel, confirm, speakers]);

  if (q.isLoading || !v) return <Spinner />;

  const live = segs.filter((s) => s.status !== "rejected");
  const groups: LaneGroup[] = [
    {
      title: "Speakers",
      lanes: [
        ...speakers.map((name) => ({ name, color: colors[name], segs: live.filter((s) => s.speaker === name).map((s) => [s.start, s.end] as [number, number]) })),
        ...(live.some((s) => !s.speaker) ? [{ name: "no speaker", color: C.subtle, segs: live.filter((s) => !s.speaker).map((s) => [s.start, s.end] as [number, number]) }] : []),
      ],
    },
    { title: "Review", lanes: [{ name: "Flags", color: C.warn, segs: segs.filter((s) => s.flagged).map((s) => [s.start, s.end] as [number, number]) }] },
  ];
  const autoCount = segs.filter((s) => s.status === "auto").length;
  const order = visible.map((s) => s.id);
  const fileIndex = files.findIndex((f) => f.id === fileId);

  return (
    <>
      <PageHeader
        crumbs={<Crumbs name={v.dataset.name} />}
        title={v.file.stem}
        subtitle={<span className="num">{v.file.path} · {clock(v.file.duration)} · file {v.position.index} of {v.position.of}</span>}
        actions={
          <>
            <Select aria-label="File" value={fileId} onChange={(e) => onFile(Number(e.target.value))}>
              {files.map((f) => (
                <option key={f.id} value={f.id} disabled={!f.usable}>
                  {f.stem} · {f.counts.review + f.counts.rejected} to review · {f.counts.confirmed} confirmed
                </option>
              ))}
            </Select>
            <Button disabled={!v.prev_file_id} onClick={() => v.prev_file_id && onFile(v.prev_file_id)} aria-label="Previous file">‹ Prev</Button>
            <Button disabled={!v.next_file_id} onClick={() => v.next_file_id && onFile(v.next_file_id)} aria-label="Next file">Next ›</Button>
          </>
        }
      />
      <LabelTabs datasetId={datasetId} current="annotate" />
      <div className="flex flex-wrap items-center gap-3">
        <span role="group" aria-label="Focus" className="inline-flex border border-line rounded-md overflow-hidden">
          <button type="button" aria-pressed className="min-h-8 px-3 text-xs border-0 bg-active text-fg">Transcript + speakers</button>
          <button type="button" disabled title="Keyword spotting is not built yet (Q6)" className="min-h-8 px-3 text-xs border-0 bg-transparent text-subtle cursor-not-allowed">Keywords</button>
          <button type="button" disabled title="Keyword spotting is not built yet (Q6)" className="min-h-8 px-3 text-xs border-0 bg-transparent text-subtle cursor-not-allowed">Both</button>
        </span>
        <span className="text-xs text-muted num">{fileIndex >= 0 ? `${fileIndex + 1}/${files.length}` : ""}</span>
        <span className="ml-auto flex gap-2">
          <Button size="sm" disabled={!autoCount || m.confirmAuto.isPending} onClick={() => m.confirmAuto.mutate()}>
            <I.Check size={13} /> Confirm all high-confidence ({autoCount})
          </Button>
          <Button size="sm" onClick={() => m.create.mutate({ start: Math.max(0, Math.min(ctl.time, v.file.duration - 1)), end: Math.min(v.file.duration, Math.max(ctl.time, 0) + 1), speaker: current?.speaker ?? null })}>
            <I.Plus size={13} /> Segment at playhead
          </Button>
        </span>
      </div>
      <ErrorNote error={m.error} />
      <Panel>
        <AudioPlayer ctl={ctl} start={0} end={v.file.duration} label="Play file" />
        <Timeline
          fileId={fileId}
          t0={0}
          t1={v.file.duration || 1}
          groups={groups}
          overlaps={overlapRegions(segs)}
          time={ctl.time}
          markers={current ? [{ t0: current.start, t1: current.end, color: C.focus }] : []}
          onSeek={(t) => {
            ctl.seek(t);
            const hit = segs.find((s) => s.start <= t && t <= s.end && s.status !== "rejected");
            if (hit) setCur(hit.id);
          }}
        />
      </Panel>
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
        <Panel
          title={`Segments · ${num(segs.length)}`}
          actions={
            <span role="group" aria-label="Show" className="inline-flex border border-line rounded-md overflow-hidden">
              {SHOW.map((o) => (
                <button key={o.value} type="button" aria-pressed={show === o.value} onClick={() => setShow(o.value)} className={cx("min-h-8 px-2.5 text-xs cursor-pointer border-0", show === o.value ? "bg-active text-fg" : "bg-transparent text-muted hover:text-fg")}>
                  {o.label}
                </button>
              ))}
            </span>
          }
        >
          {sel.size > 0 && (
            <BulkBar
              n={sel.size}
              speakers={speakers}
              onAssign={(spk) => m.bulk.mutate({ ids: [...sel], action: "assign_speaker", speaker: spk })}
              onAction={(action) => {
                m.bulk.mutate({ ids: [...sel], action });
                setSel(new Set());
              }}
              onMerge={() => {
                m.merge.mutate([...sel]);
                setSel(new Set());
              }}
              onClear={() => setSel(new Set())}
            />
          )}
          {visible.length === 0 && <Empty>No segments {show === "all" ? "in this file" : `with status “${STATUS_LABELS[show as SegStatus]}”`}.</Empty>}
          {visible.length > 0 && (
            <div className="overflow-auto scroll-thin max-h-[60vh] border border-track rounded-md">
              <table className="w-full border-collapse text-[13px]" style={{ minWidth: 720 }}>
                <thead className="sticky top-0 z-10 bg-panel">
                  <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
                    <th scope="col" className="px-2 py-2 border-b border-line w-8">
                      <input
                        type="checkbox"
                        aria-label="Select all shown"
                        checked={sel.size > 0 && order.every((id) => sel.has(id))}
                        onChange={(e) => setSel(e.target.checked ? new Set(order) : new Set())}
                      />
                    </th>
                    <th scope="col" className="px-2 py-2 border-b border-line w-8" />
                    <th scope="col" className="px-2 py-2 border-b border-line font-medium">Time</th>
                    <th scope="col" className="px-2 py-2 border-b border-line font-medium">Speaker</th>
                    <th scope="col" className="px-2 py-2 border-b border-line font-medium">Transcript</th>
                    <th scope="col" className="px-2 py-2 border-b border-line font-medium text-right">Conf.</th>
                    <th scope="col" className="px-2 py-2 border-b border-line font-medium">Status</th>
                    <th scope="col" className="px-2 py-2 border-b border-line w-20" />
                  </tr>
                </thead>
                <tbody>
                  {visible.map((s) => (
                    <SegmentRow
                      key={s.id}
                      s={s}
                      rowRef={(el) => {
                        if (el) rowRefs.current.set(s.id, el);
                        else rowRefs.current.delete(s.id);
                      }}
                      current={s.id === cur}
                      checked={sel.has(s.id)}
                      color={s.speaker ? colors[s.speaker] ?? C.subtle : C.subtle}
                      high={high}
                      low={v.thresholds.low}
                      ctl={ctl}
                      onFocus={() => setCur(s.id)}
                      onCheck={(shift) => {
                        setSel(selectRange(order, sel, s.id, anchor, shift));
                        setAnchor(s.id);
                      }}
                      onSpeaker={() => m.patch.mutate({ id: s.id, speaker: cycleSpeaker(speakers.length ? speakers : ["spk0"], s.speaker) })}
                      onText={(text) => m.patch.mutate({ id: s.id, text })}
                      onConfirm={() => m.bulk.mutate({ ids: [s.id], action: "confirm" })}
                      onReject={() => m.bulk.mutate({ ids: [s.id], action: "reject" })}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <Shortcuts />
        </Panel>
        <SidePanel
          s={current}
          ctl={ctl}
          onUse={(text) => current && m.patch.mutate({ id: current.id, text })}
          onFlag={() => current && m.patch.mutate({ id: current.id, flagged: !current.flagged })}
          onSplit={splitAtPlayhead}
          onDelete={() => current && m.remove.mutate(current.id)}
          onLive={() => navigate(`/label/live/${datasetId}`)}
        />
      </div>
    </>
  );
}

function BulkBar({ n, speakers, onAssign, onAction, onMerge, onClear }: { n: number; speakers: string[]; onAssign: (s: string) => void; onAction: (a: string) => void; onMerge: () => void; onClear: () => void }) {
  const [spk, setSpk] = useState("");
  return (
    <div role="toolbar" aria-label="Bulk actions" className="flex flex-wrap items-center gap-2 bg-active/60 border border-line rounded-md px-3 py-2">
      <span className="text-xs num">{n} selected</span>
      <label className="flex items-center gap-1.5 text-xs text-muted">
        Speaker
        <input list="speaker-names" value={spk} onChange={(e) => setSpk(e.target.value)} placeholder="name" className="bg-canvas border border-line rounded px-2 min-h-8 w-28 text-fg text-xs" />
        <datalist id="speaker-names">{speakers.map((s) => <option key={s} value={s} />)}</datalist>
        <Button size="sm" disabled={!spk.trim()} onClick={() => onAssign(spk.trim())}>Assign</Button>
      </label>
      <Button size="sm" disabled={n < 2} onClick={onMerge}>Merge</Button>
      <Button size="sm" variant="primary" onClick={() => onAction("confirm")}><I.Check size={12} /> Confirm</Button>
      <Button size="sm" variant="danger" onClick={() => onAction("reject")}><I.X size={12} /> Reject</Button>
      <Button size="sm" onClick={() => onAction("flag")}><I.Flag size={12} /> Flag</Button>
      <Button size="sm" variant="ghost" onClick={onClear} className="ml-auto">Clear</Button>
    </div>
  );
}

function SegmentRow({
  s,
  rowRef,
  current,
  checked,
  color,
  high,
  low,
  ctl,
  onFocus,
  onCheck,
  onSpeaker,
  onText,
  onConfirm,
  onReject,
}: {
  s: Segment;
  rowRef: (el: HTMLTableRowElement | null) => void;
  current: boolean;
  checked: boolean;
  color: string;
  high: number;
  low: number;
  ctl: ReturnType<typeof useAudio>;
  onFocus: () => void;
  onCheck: (shift: boolean) => void;
  onSpeaker: () => void;
  onText: (t: string) => void;
  onConfirm: () => void;
  onReject: () => void;
}) {
  const [draft, setDraft] = useState(s.text ?? "");
  useEffect(() => setDraft(s.text ?? ""), [s.text]);
  const save = () => {
    if (draft !== (s.text ?? "")) onText(draft);
  };
  const onKey = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      save();
      (e.target as HTMLInputElement).blur();
    } else if (e.key === "Escape") {
      setDraft(s.text ?? "");
      (e.target as HTMLInputElement).blur();
    }
  };
  return (
    <tr ref={rowRef} onClick={onFocus} aria-current={current || undefined} className={cx("border-b border-track", current ? "bg-active" : "hover:bg-track/60", s.status === "rejected" && "opacity-60")}>
      <td className="px-2 py-1.5">
        <input
          type="checkbox"
          aria-label={`Select segment at ${clock(s.start)}`}
          checked={checked}
          onChange={() => undefined}
          onClick={(e) => {
            e.stopPropagation();
            onCheck(e.shiftKey);
          }}
        />
      </td>
      <td className="px-2 py-1.5">
        <PlayFrom ctl={ctl} t={s.start} until={s.end} label={`Play segment at ${clock(s.start)}`} />
      </td>
      <td className="px-2 py-1.5 num text-xs text-muted whitespace-nowrap">
        {clock(s.start)}
        <br />
        {clock(s.end)}
      </td>
      <td className="px-2 py-1.5">
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onSpeaker();
          }}
          title="Click to cycle speakers (or press 1–8)"
          className="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs cursor-pointer bg-transparent text-fg num whitespace-nowrap"
          style={{ borderColor: color }}
        >
          <span className="size-2 rounded-full" style={{ background: color }} />
          {s.speaker ?? "—"}
        </button>
      </td>
      <td className="px-2 py-1.5 w-full">
        <input
          aria-label={`Transcript at ${clock(s.start)}`}
          value={draft}
          placeholder={s.text === null ? "(no transcript)" : ""}
          onFocus={onFocus}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={save}
          onKeyDown={onKey}
          className="w-full bg-transparent border border-transparent hover:border-line focus:border-focus rounded px-1.5 py-1 text-fg text-[13px] outline-none"
        />
      </td>
      <td className="px-2 py-1.5 text-right num text-xs font-semibold" style={{ color: confidenceColor(s.confidence, high, low) }}>
        {s.confidence === null ? "—" : s.confidence.toFixed(2)}
      </td>
      <td className="px-2 py-1.5 whitespace-nowrap">
        <Chip tone={STATUS_TONE[s.status]}>{STATUS_LABELS[s.status]}</Chip>
        {s.flagged && <span className="ml-1 text-warn" title={s.flag_note ?? "Flagged"}><I.Flag size={12} /></span>}
      </td>
      <td className="px-2 py-1.5 whitespace-nowrap">
        <span className="flex gap-1">
          <button type="button" aria-label="Confirm" title="Confirm (Enter)" onClick={(e) => { e.stopPropagation(); onConfirm(); }} className="size-7 inline-flex items-center justify-center rounded border border-line bg-transparent text-good cursor-pointer hover:bg-track">
            <I.Check size={12} />
          </button>
          <button type="button" aria-label="Reject" title="Reject" onClick={(e) => { e.stopPropagation(); onReject(); }} className="size-7 inline-flex items-center justify-center rounded border border-line bg-transparent text-bad cursor-pointer hover:bg-track">
            <I.X size={12} />
          </button>
        </span>
      </td>
    </tr>
  );
}

function SidePanel({ s, ctl, onUse, onFlag, onSplit, onDelete, onLive }: { s: Segment | null; ctl: ReturnType<typeof useAudio>; onUse: (t: string) => void; onFlag: () => void; onSplit: () => void; onDelete: () => void; onLive: () => void }) {
  if (!s) return <Panel title="Segment"><span className="text-[13px] text-muted">Select a segment.</span></Panel>;
  return (
    <Panel title="Segment" subtitle={<span className="num">{clock(s.start)} – {clock(s.end)} · {(s.end - s.start).toFixed(2)} s</span>}>
      <div className="flex flex-col gap-2">
        <span className="text-[11px] text-muted uppercase tracking-wider">Model outputs</span>
        {!s.alternatives?.length && <span className="text-[13px] text-muted">None stored for this span{s.reviewed ? " (edited or created by hand)" : ""}.</span>}
        {s.alternatives?.map((a) => (
          <div key={a.service} className="flex flex-col gap-1 bg-canvas border border-track rounded-md p-2">
            <span className="flex items-center justify-between gap-2 text-xs">
              <span className="truncate">
                {a.model}
                {s.chosen === a.service && <span className="ml-1.5 text-primary">· starting hypothesis</span>}
              </span>
              {s.agreement?.[a.service] !== undefined && <span className="num text-muted">agrees {(100 * s.agreement[a.service]).toFixed(0)}%</span>}
            </span>
            <span className="text-[13px]">{a.text || <span className="text-subtle">(empty)</span>}</span>
            <Button size="sm" variant="ghost" className="self-start" disabled={a.text === s.text} onClick={() => onUse(a.text)}>Use this text</Button>
          </div>
        ))}
      </div>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={() => ctl.play(s.start, s.end)}><I.Play size={11} /> Play</Button>
        <Button size="sm" onClick={onSplit} title="Split at the playhead (V)">Split at playhead</Button>
        <Button size="sm" variant={s.flagged ? "danger" : "outline"} onClick={onFlag} aria-pressed={s.flagged}><I.Flag size={12} /> {s.flagged ? "Unflag" : "Flag"}</Button>
        <Button size="sm" variant="ghost" onClick={onDelete}>Delete</Button>
      </div>
      <span className="text-xs text-muted">
        {s.reviewed ? "Reviewed: pseudo-labelling will not replace this segment." : "Not reviewed yet: a new pseudo-label run may replace it."}
      </span>
      <div className="flex flex-wrap gap-2 pt-2 border-t border-track">
        <Button size="sm" variant="ghost" onClick={onLive}><I.Pulse size={12} /> Live evaluation</Button>
        <IfFeature name="improve">
          <Button size="sm" disabled title="Training (Phase 3) is not scoped yet">
            <I.Flask size={12} /> Use for retraining
          </Button>
        </IfFeature>
      </div>
    </Panel>
  );
}

export function Shortcuts() {
  const keys: [string, string][] = [
    ["Space", "play / pause"],
    ["Tab", "next low-confidence"],
    ["1–8", "assign speaker"],
    ["V", "split at playhead"],
    ["M", "merge"],
    ["Enter", "confirm"],
    ["↑ ↓", "move"],
    ["Shift-click", "select range"],
  ];
  return (
    <span className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted" aria-label="Keyboard shortcuts">
      {keys.map(([k, d]) => (
        <span key={k} className="flex items-center gap-1.5">
          <kbd className="num text-[10px] border border-line rounded px-1 py-px text-fg bg-track">{k}</kbd>
          {d}
        </span>
      ))}
      <Link to="/label" className="ml-auto text-[11px]">Back to the console</Link>
    </span>
  );
}
