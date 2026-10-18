import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api, qs, type AlignOp, type Page, type Run, type Utterance, type UtteranceDetail } from "../../lib/api";
import { useNormaliser, useNormalisers } from "../../lib/config";
import { clock, deltaPts, num, pct, timingLabel } from "../../lib/format";
import { AudioPlayer, PlayFrom, useAudio } from "../../components/AudioPlayer";
import { AlignmentGrid, DiffText } from "../../components/DiffText";
import { Delta, MetricTile } from "../../components/MetricTile";
import { ExpandCell, SortableTable } from "../../components/SortableTable";
import { Timeline } from "../../components/Timeline";
import { viewAround } from "../../components/timelineMath";
import * as I from "../../components/icons";
import { Bar, Button, Chip, cx, Empty, ErrorNote, LinkButton, PageHeader, Panel, Select, Spinner } from "../../components/ui";
import { Crumbs, latestRuns, RunPickers, Switcher, useDoneRuns, useSwitchLinks } from "./common";

interface Summary {
  run: Run;
  score_id: number;
  normaliser: string;
  timing_level: string | null;
  tiles: Record<string, number | null>;
  compare: { run: Run; score_id: number; tiles: Record<string, number | null> } | null;
  words: { word: string; errors: number; occurrences: number; rate: number; top: string; top_n: number }[];
  pairs: { ref: string; hyp: string; n: number; note: string }[];
  slices: { group: string; label: string; wer: number | null; n: number; utterances: number }[];
}

export function AsrErrors() {
  const { runId } = useParams();
  const [params, setParams] = useSearchParams();
  const [normaliser, setNormaliser] = useNormaliser();
  const compare = params.get("compare") ?? undefined;
  const { data: norms } = useNormalisers();
  const s = useQuery({
    queryKey: ["asr-summary", runId, normaliser, compare],
    queryFn: () => api.get<Summary>(`/errors/asr/summary${qs({ run_id: runId, normaliser, compare_run_id: compare })}`),
  });
  const links = useSwitchLinks(s.data?.run.dataset_id, "");
  const others = latestRuns(useDoneRuns("asr").data ?? []).filter((r) => s.data && r.dataset_id === s.data.run.dataset_id && r.id !== s.data.run.id);
  const [q, setQ] = useState("");
  const [onlyErrors, setOnlyErrors] = useState(false);
  const [word, setWord] = useState<string | null>(null);
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
  const tiles: { key: string; label: string; color?: string; fmt: (v: number | null) => string; sub: string; count?: boolean }[] = [
    { key: "wer", label: "WER", fmt: (v) => pct(v), sub: `${num(t.errors)} errors / ${num(t.n)} words` },
    { key: "cer", label: "CER", fmt: (v) => pct(v), sub: "characters" },
    { key: "s", label: "Substitutions", color: "var(--color-sub)", fmt: (v) => num(v), sub: `${pct((t.s ?? 0) / (t.n || 1))} of words`, count: true },
    { key: "d", label: "Deletions", color: "var(--color-del)", fmt: (v) => num(v), sub: `${pct((t.d ?? 0) / (t.n || 1))} of words`, count: true },
    { key: "i", label: "Insertions", color: "var(--color-ins)", fmt: (v) => num(v), sub: `${pct((t.i ?? 0) / (t.n || 1))} of words`, count: true },
  ];

  return (
    <>
      <PageHeader
        crumbs={<Crumbs parts={[{ to: "/benchmarks/asr", label: "Benchmarks" }, "ASR", d.run.dataset]} />}
        title={
          <span className="flex flex-wrap items-baseline gap-2">
            {d.run.label}
            <Chip>{timingLabel(d.timing_level)}</Chip>
          </span>
        }
        actions={
          <RunPickers
            task="asr"
            run={d.run}
            path={(r) => `/errors/asr/${r.id}${qs({ normaliser })}`}
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
                <Select label="Normaliser" value={normaliser} onChange={(e) => setNormaliser(e.target.value)}>
                  {(norms ?? []).map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.label}
                    </option>
                  ))}
                </Select>
              </>
            }
          />
        }
      />
      <Switcher current="asr" links={links} />
      <div className="flex flex-wrap gap-3">
        {tiles.map((k) => (
          <MetricTile
            key={k.key}
            label={k.label}
            color={k.color}
            value={k.fmt(t[k.key])}
            sub={c ? `vs ${d.compare!.run.label}: ${k.fmt(c[k.key])}` : k.sub}
            delta={c && <Delta value={t[k.key]} compare={c[k.key]} format={(x) => (k.count ? num(x) : deltaPts(x).replace(/^[+−]/, ""))} />}
          />
        ))}
      </div>
      <Panel
        id="utt-h"
        title="Utterances"
        subtitle="Reference vs hypothesis for every utterance. Sort by any metric; open a row to listen and inspect."
        actions={
          <>
            <label className="relative flex">
              <span className="sr-only">Search transcripts</span>
              <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-subtle">
                <I.Search size={15} />
              </span>
              <input
                type="search"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search reference or hypothesis"
                className="min-h-9 w-[260px] bg-canvas text-fg border border-line rounded-md pl-8 pr-2 text-[13px]"
              />
            </label>
            <label className="flex items-center gap-2 text-[13px] text-muted cursor-pointer">
              <input type="checkbox" checked={onlyErrors} onChange={(e) => setOnlyErrors(e.target.checked)} className="accent-[#06B6D4]" />
              Only with errors
            </label>
            {word && (
              <Button size="sm" onClick={() => setWord(null)} aria-label={`Clear word filter ${word}`}>
                word: <span className="num">{word}</span> <I.X size={12} />
              </Button>
            )}
            <a
              className="inline-flex items-center gap-1.5 min-h-8 px-2.5 text-xs rounded-md border border-line !text-fg no-underline hover:bg-track"
              href={`/api/errors/asr/utterances.csv${qs({ score_id: d.score_id, q, only_errors: onlyErrors || undefined, word: word ?? undefined })}`}
            >
              <I.Download size={13} /> Export CSV
            </a>
          </>
        }
      >
        <UtteranceTable scoreId={d.score_id} q={q} onlyErrors={onlyErrors} word={word} timing={d.timing_level} />
      </Panel>
      <div className="grid gap-5 lg:grid-cols-3">
        <Panel title="Most mis-transcribed words" subtitle="Click a word to filter the table">
          {d.words.length === 0 && <span className="text-[13px] text-muted">No word errors.</span>}
          {d.words.map((w) => (
            <button
              key={w.word}
              type="button"
              onClick={() => setWord(w.word === word ? null : w.word)}
              aria-pressed={w.word === word}
              className={cx("grid grid-cols-[1fr_auto] gap-x-3 gap-y-1 items-center text-left bg-transparent border rounded-md px-2 py-1.5 cursor-pointer", w.word === word ? "border-focus" : "border-transparent hover:bg-track")}
            >
              <span className="flex items-baseline gap-2 min-w-0">
                <span className="num text-[13px] text-fg">{w.word}</span>
                <span className="text-[11px] text-subtle truncate">→ {w.top}</span>
              </span>
              <span className="num text-xs text-muted">
                {w.errors}/{w.occurrences}
              </span>
              <Bar value={w.rate} color="var(--color-bad)" className="col-span-2" />
            </button>
          ))}
        </Panel>
        <Panel title="Top substitution pairs">
          {d.pairs.length === 0 && <span className="text-[13px] text-muted">No substitutions.</span>}
          {d.pairs.map((p) => (
            <div key={`${p.ref}-${p.hyp}`} className="flex items-center justify-between gap-2 text-[13px]">
              <span className="num">
                <span className="text-fg">{p.ref}</span> <span className="text-sub">→ {p.hyp}</span>
              </span>
              <span className="flex items-center gap-2">
                {p.note && <span className="text-[11px] text-subtle">{p.note}</span>}
                <span className="num text-muted">×{p.n}</span>
              </span>
            </div>
          ))}
        </Panel>
        <Panel title="WER by slice" subtitle="File metadata, utterance length and speaker">
          {d.slices.length === 0 && <span className="text-[13px] text-muted">No slices: add metadata fields (e.g. condition) to the annotations.</span>}
          {d.slices.map((sl) => (
            <div key={`${sl.group}-${sl.label}`} className="grid grid-cols-[minmax(0,1fr)_90px_56px] gap-2 items-center text-[13px]">
              <span className="truncate">
                <span className="text-subtle text-[11px]">{sl.group}</span> {sl.label}
              </span>
              <Bar value={sl.wer} max={Math.max(0.01, ...d.slices.map((x) => x.wer ?? 0))} color="var(--color-secondary)" />
              <span className="num text-right">{pct(sl.wer)}</span>
            </div>
          ))}
        </Panel>
      </div>
    </>
  );
}

function UtteranceTable({ scoreId, q, onlyErrors, word, timing }: { scoreId: number; q: string; onlyErrors: boolean; word: string | null; timing: string | null }) {
  const [open, setOpen] = useState<string | number | null>(null);
  const columns = useMemo<ColumnDef<Utterance, unknown>[]>(
    () => [
      { id: "key", header: "ID", meta: { width: "130px" }, cell: ({ row }) => <ExpandCell open={open === row.original.id} label={row.original.key} /> },
      { id: "speaker", header: "Spk", meta: { width: "80px" }, cell: ({ row }) => <span className="num text-xs text-muted">{row.original.speaker ?? "—"}</span> },
      { id: "duration", header: "Dur", meta: { align: "right", width: "64px" }, cell: ({ row }) => <span className="num text-xs">{row.original.duration.toFixed(1)}s</span> },
      { id: "ref", header: "Reference", enableSorting: false, cell: ({ row }) => <DiffText ops={row.original.ops} mode="ref" highlight={word ?? undefined} /> },
      { id: "hyp", header: "Hypothesis", enableSorting: false, cell: ({ row }) => <DiffText ops={row.original.ops} mode="hyp" /> },
      { id: "n", header: "N", meta: { align: "right", width: "48px" }, cell: ({ row }) => <span className="num">{row.original.n}</span> },
      { id: "s", header: "S", meta: { align: "right", width: "40px" }, cell: ({ row }) => <span className={cx("num", row.original.s ? "text-sub" : "text-subtle")}>{row.original.s}</span> },
      { id: "d", header: "D", meta: { align: "right", width: "40px" }, cell: ({ row }) => <span className={cx("num", row.original.d ? "text-del" : "text-subtle")}>{row.original.d}</span> },
      { id: "i", header: "I", meta: { align: "right", width: "40px" }, cell: ({ row }) => <span className={cx("num", row.original.i ? "text-ins" : "text-subtle")}>{row.original.i}</span> },
      {
        id: "wer",
        header: "WER",
        meta: { align: "right", width: "110px" },
        cell: ({ row }) => (
          <span className="inline-flex items-center gap-2 justify-end">
            <Bar value={row.original.wer} className="w-12" color={row.original.wer ? "var(--color-bad)" : "var(--color-good)"} />
            <span className="num">{pct(row.original.wer)}</span>
          </span>
        ),
      },
    ],
    [open, word],
  );
  return (
    <SortableTable<Utterance>
      columns={columns}
      caption="Utterances"
      queryKey={["utts", scoreId, q, onlyErrors, word]}
      fetchPage={({ offset, limit, sort, order }) =>
        api.get<Page<Utterance>>(`/errors/asr/utterances${qs({ score_id: scoreId, offset, limit, sort, order, q, only_errors: onlyErrors || undefined, word: word ?? undefined })}`)
      }
      initialSort={{ id: "wer", desc: true }}
      rowKey={(r) => r.id}
      expanded={open}
      onExpand={setOpen}
      renderExpanded={(r) => <UtteranceDetailView id={r.id} timing={timing} />}
      empty="No utterances match."
    />
  );
}

function errorMarks(ops: AlignOp[]): { t0: number; t1: number; letter: string; op: AlignOp }[] {
  return ops
    .filter((o) => o.op !== "eq" && o.t0 !== undefined && o.t1 !== undefined)
    .map((o) => ({ t0: o.t0!, t1: Math.max(o.t1!, o.t0! + 0.08), letter: { sub: "S", del: "D", ins: "I" }[o.op as "sub"], op: o }));
}

const MARK_COLOR: Record<string, string> = { S: "#F59E0B", D: "#F43F5E", I: "#10B981" };

function UtteranceDetailView({ id, timing }: { id: number; timing: string | null }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["utt", id], queryFn: () => api.get<UtteranceDetail>(`/errors/asr/utterances/${id}`) });
  const ctl = useAudio(q.data?.file_id);
  const flag = useMutation({
    mutationFn: (flagged: boolean) => api.post(`/segments/${q.data!.segment_id}/flag`, { flagged }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["utt", id] });
      void qc.invalidateQueries({ queryKey: ["utts"] });
    },
  });
  if (q.isLoading || !q.data) return <div className="p-4"><Spinner /></div>;
  const u = q.data;
  const marks = errorMarks(u.ops);
  const errs = u.ops.filter((o) => o.op !== "eq");
  const view = viewAround(u.start, u.end, u.end + 1, 0.3);
  return (
    <div className="p-4 grid gap-4 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
      <div className="flex flex-col gap-4 min-w-0">
        <div className="flex flex-col gap-2 bg-panel border border-track rounded-md p-3">
          <AudioPlayer ctl={ctl} start={u.start} end={u.end} errorSpans={u.timed ? marks.map((m) => [m.t0, m.t1]) : undefined} label="Play utterance" />
          <Timeline
            fileId={u.file_id}
            t0={view.t0}
            t1={view.t1}
            time={ctl.time}
            onSeek={(t) => ctl.play(t, u.end)}
            markers={u.timed ? marks.map((m) => ({ t0: m.t0, t1: m.t1, color: MARK_COLOR[m.letter], label: m.letter })) : errs.length ? [{ t0: u.start, t1: u.end, color: "#F59E0B", label: `${errs.length} errors` }] : []}
          />
          {!u.timed && <span className="text-[11px] text-subtle">This model gives {timingLabel(timing)}, so errors are marked on the whole utterance.</span>}
        </div>
        <div className="flex flex-col gap-2">
          <span className="text-[11px] text-muted uppercase tracking-wider">Word alignment</span>
          <AlignmentGrid ops={u.ops} />
        </div>
        <div className="flex flex-col gap-1.5">
          <span className="text-[11px] text-muted uppercase tracking-wider">Errors · {errs.length}</span>
          {errs.length === 0 && <span className="text-[13px] text-muted">No errors.</span>}
          {errs.map((o, k) => (
            <div key={k} className="flex items-center gap-3 text-[13px]">
              <PlayFrom ctl={ctl} t={o.t0 !== undefined ? o.t0 - 0.3 : u.start} until={o.t1 !== undefined ? o.t1 + 0.3 : u.end} label={`Play from error ${k + 1}`} />
              <span className="num text-xs text-muted w-[84px]">{o.t0 !== undefined ? clock(o.t0) : "—"}</span>
              <span className={cx("text-xs w-24", { sub: "text-sub", del: "text-del", ins: "text-ins" }[o.op as "sub"])}>{{ sub: "substitution", del: "deletion", ins: "insertion" }[o.op as "sub"]}</span>
              <span className="num">
                {o.op === "sub" ? `${o.ref} → ${o.hyp}` : o.op === "del" ? `−${o.ref}` : `+${o.hyp}`}
              </span>
            </div>
          ))}
        </div>
      </div>
      <div className="flex flex-col gap-4 min-w-0">
        <div className="flex flex-col gap-2">
          <span className="text-[11px] text-muted uppercase tracking-wider">Other models on this utterance</span>
          {u.others.length === 0 && <span className="text-[13px] text-muted">No other ASR models have run on this dataset yet.</span>}
          {u.others.map((o) => (
            <div key={o.run_id} className="flex flex-col gap-1 bg-panel border border-track rounded-md p-2.5">
              <span className="flex items-center justify-between text-xs">
                <span>{o.model}</span>
                <span className="num text-muted">WER {pct(o.wer)}</span>
              </span>
              <DiffText ops={o.ops} mode="inline" />
            </div>
          ))}
        </div>
        <div className="flex flex-wrap gap-2">
          <LinkButton to={`/label/file/${u.file_id}${u.segment_id ? `?segment=${u.segment_id}` : ""}`}>
            <I.Pen size={14} /> Open in annotator
          </LinkButton>
          <Button variant={u.flagged ? "danger" : "outline"} disabled={!u.segment_id || flag.isPending} onClick={() => flag.mutate(!u.flagged)} aria-pressed={!!u.flagged}>
            <I.Flag size={14} /> {u.flagged ? "Reference flagged — unflag" : "Flag reference as wrong"}
          </Button>
        </div>
        {u.flagged && <span className="text-xs text-warn">Flagged references show in the annotator's flags lane; scores still use them until you change them.</span>}
        {errs.length === 0 && u.others.length === 0 && <Empty>Nothing more to show.</Empty>}
      </div>
    </div>
  );
}
