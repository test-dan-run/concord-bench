// Enhancement error analysis (EVALUATE_PLAN.md §6): for one diarisation → ASR pair on a dataset, what each
// enhancer did before and after diarisation, where it helps by label, the clips it changed most (with A / B
// listening), and the enhancement models with their copies. Quality scores (DNSMOS, SNR) are not built yet.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router-dom";
import { api, qs, type EnhancedCopy, type PipeGrid, type PipeRow, type Run, type SliceReport } from "../../lib/api";
import { useNormaliser } from "../../lib/config";
import { STAGE_COLORS } from "../../lib/colors";
import { bytes, clock, deltaPts, deltaTone, pct, rtf } from "../../lib/format";
import { ClipButton } from "../../components/AudioPlayer";
import { NormaliserButtons } from "../../components/NormaliserButtons";
import * as I from "../../components/icons";
import { Chip, cx, Empty, EnhChip, ErrorNote, Fact, PageHeader, Panel, Segmented, Select, Spinner } from "../../components/ui";
import { Crumbs, Switcher, useDoneRuns, useSwitchLinks } from "./common";

/** "Error analysis" → Enhancement with no run: the newest pipeline run on an enhanced copy. */
export function EnhanceIndex() {
  const runs = useDoneRuns("pipeline");
  if (runs.isLoading) return <Spinner />;
  const r = (runs.data ?? []).find((x) => x.enh_model_id !== null);
  if (r) return <Navigate to={`/errors/enh/${r.id}`} replace />;
  return (
    <>
      <PageHeader crumbs="Evaluate" title="Enhancement error analysis" />
      <Empty>
        No pipeline has run on an enhanced copy yet. In <Link to="/benchmarks/pipeline">Pipelines</Link>, start a New pipeline run with an enhancer ticked.
      </Empty>
    </>
  );
}

interface Clips {
  copy_id: number | null;
  clips: { file_id: number; file: string; start: number; end: number; speaker: string; change: number; original: { errors: number; words: { w: string; cause: string | null }[] }; enhanced: { errors: number; words: { w: string; cause: string | null }[] } }[];
}

export function EnhanceErrors() {
  const { runId } = useParams();
  const nav = useNavigate();
  const [normaliser] = useNormaliser();
  const run = useQuery({ queryKey: ["run", runId], queryFn: () => api.get<Run>(`/runs/${runId}`) });
  const dsId = run.data?.dataset_id;
  const g = useQuery({ queryKey: ["pipes", dsId, normaliser], queryFn: () => api.get<PipeGrid>(`/benchmarks-pipelines${qs({ dataset_id: dsId, normaliser })}`), enabled: !!dsId });
  const copies = useQuery({ queryKey: ["enhanced", dsId], queryFn: () => api.get<EnhancedCopy[]>(`/datasets/${dsId}/enhanced`), enabled: !!dsId });
  const links = useSwitchLinks(dsId, "");
  const [enhSel, setEnhSel] = useState<number | null>(null);
  if (run.isLoading || g.isLoading) return <Spinner />;
  if (run.error || !run.data) return <ErrorNote error={run.error} />;
  if (!g.data) return <ErrorNote error={g.error} />;
  const r0 = run.data;
  const lb = g.data.leaderboard;
  const segId = r0.seg_model_id ?? null;
  const asrId = r0.asr_model_id!;
  const pair = lb.filter((x) => x.seg_model_id === segId && x.asr_model_id === asrId);
  const base = pair.find((x) => x.enh_model_id === null);
  const enhIds = [...new Set(pair.map((x) => x.enh_model_id).filter((x): x is number => x !== null))];
  const enhId = enhSel ?? r0.enh_model_id ?? enhIds[0] ?? null;
  const sel = pair.find((x) => x.run_id === r0.id) ?? pair.find((x) => x.enh_model_id === enhId) ?? base;
  const enhName = (id: number | null) => g.data.enhancers.find((e) => e.id === id)?.display_name ?? "enhancer";
  const segName = g.data.rows.find((x) => x.seg_model_id === segId)?.name ?? "";
  const asrName = g.data.cols.find((x) => x.asr_model_id === asrId)?.name ?? "";
  const pairs = [...new Map(lb.filter((x) => x.enh_model_id !== null).map((x) => [`${x.seg_model_id}-${x.asr_model_id}`, x])).values()];
  const go = (row: PipeRow | undefined) => row && nav(`/errors/enh/${row.run_id}${qs({ normaliser })}`);
  const of = (e: number | null, place: "before" | "after" | null) => pair.find((x) => x.enh_model_id === e && (e === null || x.placement === place || (segId === null && x.placement === null)));
  const val = (x: PipeRow | undefined) => (x ? (x.cpwer ?? x.wer ?? null) : null);
  const metricName = base?.cpwer !== null && base?.cpwer !== undefined ? "cpWER" : "WER";
  const building = (e: number | null) => g.data.copies.find((c) => c.enh_model_id === e && c.status === "building");

  return (
    <>
      <PageHeader
        crumbs={<Crumbs parts={[{ to: "/benchmarks/pipeline", label: "Benchmarks" }, "Pipelines", r0.dataset]} />}
        title={`Enhancement · ${segName} → ${asrName}`}
        actions={
          <div className="flex flex-wrap items-center gap-3">
            <Select
              label="Pipeline"
              value={`${segId}-${asrId}`}
              onChange={(e) => go(pairs.find((x) => `${x.seg_model_id}-${x.asr_model_id}` === e.target.value))}
            >
              {pairs.map((x) => (
                <option key={`${x.seg_model_id}-${x.asr_model_id}`} value={`${x.seg_model_id}-${x.asr_model_id}`}>
                  {g.data.rows.find((r) => r.seg_model_id === x.seg_model_id)?.name} → {g.data.cols.find((c) => c.asr_model_id === x.asr_model_id)?.name}
                </option>
              ))}
            </Select>
            <NormaliserButtons />
          </div>
        }
      />
      <Switcher current="enh" links={links} />
      {!enhIds.length ? (
        <Empty>This pipeline has not run on an enhanced copy yet.</Empty>
      ) : (
        <div className="flex flex-col xl:flex-row gap-5 items-start">
          <div className="flex-1 w-full flex flex-col gap-5 min-w-0">
            <Panel
              id="pl-h"
              title={`Placement · ${enhName(enhId)}`}
              subtitle="Which audio each stage heard, and what it scored. Click a card to inspect it."
              actions={<Segmented label="Enhancer" hideLabel value={enhId ?? 0} onChange={(v) => setEnhSel(v)} options={enhIds.map((e) => ({ value: e, label: enhName(e) }))} />}
            >
              <div className="grid gap-3 md:grid-cols-3">
                {([[null, null, "Original audio"], [enhId, "before", "Before diarisation"], [enhId, "after", "After diarisation"]] as const).map(([e, place, title]) => {
                  const x = of(e, place);
                  const v = val(x);
                  const b = val(base);
                  const all = pair.filter((y) => y.enh_model_id === enhId).map(val).filter((y): y is number => y !== null);
                  const isBest = e !== null && v !== null && all.length > 0 && v === Math.min(...all) && b !== null && v < b;
                  const busy = !x && e !== null && building(e);
                  return (
                    <button key={title} type="button" disabled={!x} aria-pressed={sel?.run_id === x?.run_id} onClick={() => go(x)} className={cx("text-left flex flex-col gap-2 rounded-lg border p-3 cursor-pointer disabled:cursor-default", sel && x && sel.run_id === x.run_id ? "border-focus bg-active/50" : "border-track bg-canvas hover:bg-track/40")}>
                      <span className="flex items-center gap-2 text-[13px] font-medium">
                        {title} {isBest && <Chip tone="good">Best</Chip>}
                      </span>
                      <span className="flex flex-col gap-1 text-[11px]">
                        <span className="flex items-center gap-2"><span style={{ color: STAGE_COLORS.segmentation }}>VAD / diar.</span><span className={cx("px-1.5 rounded-full", e !== null && place === "before" ? "bg-enh/15 text-enh-soft" : "bg-track text-muted")}>{e !== null && place === "before" ? "enhanced" : "original"}</span></span>
                        <span className="flex items-center gap-2"><span style={{ color: STAGE_COLORS.asr }}>ASR</span><span className={cx("px-1.5 rounded-full", e !== null ? "bg-enh/15 text-enh-soft" : "bg-track text-muted")}>{e !== null ? "enhanced" : "original"}</span></span>
                      </span>
                      {x ? (
                        <>
                          <span className="flex items-baseline gap-2">
                            <span className="num text-[22px]">{pct(v)}</span>
                            <span className="text-[11px] text-muted">{metricName}</span>
                            {e !== null && v !== null && b !== null && <span className={cx("num text-xs", deltaTone(v - b))}>{deltaPts(v - b)}</span>}
                          </span>
                          <span className="num text-[11px] text-subtle">DER {pct(x.der)} · WER {pct(x.wer)}</span>
                        </>
                      ) : busy ? (
                        <span className="text-xs text-enh-soft inline-flex items-center gap-1.5"><span className="size-2 rounded-full bg-enh animate-pulse" />Building enhanced copy · {Math.round(busy.progress * 100)}%</span>
                      ) : (
                        <span className="text-xs text-subtle">Not run. Start it from Pipelines → New pipeline run.</span>
                      )}
                    </button>
                  );
                })}
              </div>
            </Panel>
            <Panel id="grid-h" title={`Enhancer × placement · ${metricName}`} subtitle={`${segName} → ${asrName} · change against the same pipeline on original audio · lower is better`}>
              <div className="grid gap-1.5" style={{ gridTemplateColumns: "minmax(150px, 1.2fr) repeat(2, minmax(120px, 1fr))" }}>
                <span />
                <span className="flex flex-col text-right px-2"><span className="text-[12px]">Before diarisation</span><span className="text-[11px] text-subtle">both stages hear enhanced</span></span>
                <span className="flex flex-col text-right px-2"><span className="text-[12px]">After diarisation</span><span className="text-[11px] text-subtle">only ASR hears enhanced</span></span>
                <span className="flex flex-col justify-center"><span className="text-[13px]">No enhancement</span><span className="text-[11px] text-subtle">baseline · original audio</span></span>
                <button type="button" onClick={() => go(base)} className="col-span-2 rounded-md bg-track/60 border-0 text-right px-3 py-2 cursor-pointer num">Same for both placements · {pct(val(base))}</button>
                {enhIds.map((e) => (
                  <GridRow key={e} name={enhName(e)} generative={!!g.data.enhancers.find((x) => x.id === e)?.generative} cells={(["before", "after"] as const).map((p) => of(e, p))} base={val(base)} sel={sel?.run_id ?? undefined} onPick={go} building={!!building(e)} />
                ))}
              </div>
              <span className="flex flex-wrap gap-4 text-[11px] text-muted">
                <span><span className="num">−</span> better than baseline</span>
                <span><span className="num">+</span> worse than baseline</span>
              </span>
            </Panel>
            {sel && base && sel.enh_model_id !== null && sel.score_id && base.score_id && <Helps score={sel.score_id} base={base.score_id} />}
            {sel && base && sel.enh_model_id !== null && <Listen run={sel} base={base} normaliser={normaliser} enh={enhName(sel.enh_model_id)} />}
            <Panel id="mod-h" title="Enhancement models" subtitle="Each enhancer's copy of this dataset and what it does downstream. Signal-quality scores (DNSMOS, SI-SDR, PESQ) are not computed yet.">
              <div className="overflow-x-auto scroll-thin">
                <table className="w-full border-collapse text-[13px]">
                  <thead>
                    <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
                      <th scope="col" className="font-medium px-3 py-2 border-b border-line">Model</th>
                      <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">Length shift</th>
                      <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">RTF</th>
                      <th scope="col" className="font-medium px-3 py-2 border-b border-line">Enhanced copy</th>
                      <th scope="col" className="font-medium px-3 py-2 border-b border-line">Best placement</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(copies.data ?? []).map((c) => {
                      const rows = pair.filter((x) => x.enh_model_id === c.model_id);
                      const best = rows.slice().sort((a, b) => (val(a) ?? 9) - (val(b) ?? 9))[0];
                      const b = val(base);
                      return (
                        <tr key={c.id} className="border-b border-track">
                          <td className="px-3 py-2"><span className="flex items-center gap-1.5">{c.display_name} {c.generative && <EnhChip>generative</EnhChip>}</span></td>
                          <td className="px-3 py-2 text-right num text-xs">{c.max_shift_ms === null ? "—" : c.max_shift_ms ? `${c.max_shift_ms.toFixed(1)} ms, fixed` : "none"}</td>
                          <td className="px-3 py-2 text-right num text-xs">{rtf(c.rtf)}</td>
                          <td className="px-3 py-2 text-xs">{c.status === "building" ? `building · ${Math.round(c.progress * 100)}%` : `${c.status}${c.size_bytes ? ` · ${bytes(c.size_bytes)}` : ""}`}</td>
                          <td className="px-3 py-2 text-xs">
                            {best ? (
                              <span className="flex items-center gap-2">
                                {best.placement === "before" ? "Before diarisation" : best.placement === "after" ? "After diarisation" : "ASR only"}
                                {b !== null && val(best) !== null && <span className={cx("num", deltaTone(val(best)! - b))}>{deltaPts(val(best)! - b)}</span>}
                              </span>
                            ) : (
                              <span className="text-subtle">not run with this pipeline</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </Panel>
          </div>
          <aside aria-label="Selected pipeline" className="w-full xl:w-[340px] shrink-0 flex flex-col gap-4">
            {sel && <Inspect r={sel} base={base} segName={segName} asrName={asrName} enhName={enhName(sel.enh_model_id)} normaliser={normaliser} />}
            <Panel title="How enhancement runs">
              <p className="m-0 text-[13px] text-muted leading-relaxed"><span className="text-fg">One enhanced copy per enhancer.</span> Same file names, same length, so your references still line up. It is built once, reused by every run that asks for it, and listed on the dataset's page.</p>
              <p className="m-0 text-[13px] text-muted leading-relaxed"><span className="text-enh-soft">Before diarisation:</span> diarisation and ASR both read the enhanced copy.</p>
              <p className="m-0 text-[13px] text-muted leading-relaxed"><span className="text-enh-soft">After diarisation:</span> diarisation reads the original; ASR transcribes the enhanced copy, cut at the diarised segments.</p>
              <p className="m-0 text-[13px] text-muted leading-relaxed"><span className="text-fg">Length check.</span> An output up to 10 ms off is padded or trimmed to the original length; further off, the file fails.</p>
            </Panel>
          </aside>
        </div>
      )}
    </>
  );
}

function GridRow({ name, generative, cells, base, sel, onPick, building }: { name: string; generative: boolean; cells: (PipeRow | undefined)[]; base: number | null; sel?: number; onPick: (r: PipeRow | undefined) => void; building: boolean }) {
  return (
    <>
      <span className="flex flex-col justify-center">
        <span className="text-[13px] flex items-center gap-1.5">{name} {generative && <EnhChip>generative</EnhChip>}</span>
      </span>
      {cells.map((c, i) => {
        const v = c ? (c.cpwer ?? c.wer ?? null) : null;
        if (!c || v === null)
          return (
            <span key={i} className="rounded-md bg-track/40 px-3 py-2 text-right text-[11px] text-subtle">
              {building ? "building copy" : "not run"}
            </span>
          );
        const d = base !== null ? v - base : null;
        const good = d !== null && d < 0;
        const rel = d === null ? 0 : Math.min(1, Math.abs(d) / 0.03);
        return (
          <button key={i} type="button" aria-pressed={sel === c.run_id} onClick={() => onPick(c)} aria-label={`${name} ${i === 0 ? "before" : "after"} diarisation: ${pct(v)}`} className={cx("flex flex-col items-end rounded-md px-3 py-2 cursor-pointer border", sel === c.run_id ? "border-focus" : "border-transparent")} style={{ background: d === null || Math.abs(d) < 1e-9 ? "rgba(30,41,59,0.6)" : `${good ? "rgba(16,185,129," : "rgba(244,63,94,"}${(0.12 + rel * 0.5).toFixed(2)})` }}>
            <span className="num text-[13px]">{d === null ? "—" : deltaPts(d)}</span>
            <span className="num text-[11px] text-muted">{pct(v)}</span>
          </button>
        );
      })}
    </>
  );
}

function Helps({ score, base }: { score: number; base: number }) {
  const [label, setLabel] = useState<string | null>(null);
  const all = useQuery({ queryKey: ["slices", score, base], queryFn: () => api.get<SliceReport>(`/errors/slices${qs({ score_id: score, compare_score_id: base })}`) });
  const usable = (all.data?.labels ?? []).filter((l) => l.kind !== "unused");
  const cur = label ?? usable[0]?.key ?? null;
  const one = useQuery({ queryKey: ["slices", score, base, cur], queryFn: () => api.get<SliceReport>(`/errors/slices${qs({ score_id: score, compare_score_id: base, label: cur })}`), enabled: !!cur });
  const rows = one.data?.rows ?? [];
  const deltas = rows.map((r) => (r.score !== null && r.compare?.score !== null && r.compare?.score !== undefined ? r.score - r.compare.score : null));
  const max = Math.max(0.005, ...deltas.map((d) => Math.abs(d ?? 0)));
  return (
    <Panel
      id="snr-h"
      title="Where enhancement helps · by label"
      subtitle="cpWER change against original audio in each slice of a file label. Green bars left of the line are better."
      actions={
        usable.length ? (
          <Select label="Label" value={cur ?? ""} onChange={(e) => setLabel(e.target.value)}>
            {usable.map((l) => (
              <option key={l.key} value={l.key}>{l.key}</option>
            ))}
          </Select>
        ) : undefined
      }
    >
      {all.isLoading && <Spinner />}
      {all.data && !usable.length && <span className="text-[13px] text-muted">This dataset has no file labels to break the change down by. SNR estimates are not built yet.</span>}
      {rows.map((r, i) => {
        const d = deltas[i];
        return (
          <div key={String(r.value)} className="grid grid-cols-[minmax(110px,180px)_minmax(0,1fr)_70px_60px] gap-3 items-center text-[13px]">
            <span className="num truncate">{String(r.value)}</span>
            <span className="relative h-3">
              <span className="absolute left-1/2 top-0 bottom-0 border-l border-line" />
              {d !== null && <span className="absolute top-0 bottom-0 rounded" style={{ left: d < 0 ? `${50 - (Math.abs(d) / max) * 50}%` : "50%", width: `${(Math.abs(d) / max) * 50}%`, background: d < 0 ? "var(--color-good)" : "var(--color-bad)" }} />}
            </span>
            <span className={cx("num text-right", deltaTone(d))}>{d === null ? "—" : deltaPts(d)}</span>
            <span className="num text-right text-xs text-muted">{r.n} files</span>
          </div>
        );
      })}
      {rows.length > 0 && <span className="text-[11px] text-subtle">A gain on noisy files and a cost on clean ones points to enhancing only some files; that is not an option in run setup yet.</span>}
    </Panel>
  );
}

function Listen({ run, base, normaliser, enh }: { run: PipeRow; base: PipeRow; normaliser: string; enh: string }) {
  const q = useQuery({ queryKey: ["enh-clips", run.run_id, base.run_id, normaliser], queryFn: () => api.get<Clips>(`/errors/enh/clips${qs({ run_id: run.run_id, base_run_id: base.run_id, normaliser })}`) });
  const [k, setK] = useState(0);
  const [side, setSide] = useState<"original" | "enhanced">("enhanced");
  const clips = q.data?.clips ?? [];
  const c = clips[Math.min(k, clips.length - 1)];
  return (
    <Panel id="ab-h" title="Listen · original vs enhanced" subtitle="Reference segments where enhancement changed the transcript most, in either direction. Words in colour were wrong, coloured by the stage that caused the error.">
      {q.isLoading && <Spinner />}
      {q.data && !clips.length && <span className="text-[13px] text-muted">Enhancement changed no segment's errors in this pipeline.</span>}
      {c && (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <Select label="Clip" value={k} onChange={(e) => setK(Number(e.target.value))}>
              {clips.map((x, i) => (
                <option key={i} value={i}>
                  {x.file} · {clock(x.start, false)}–{clock(x.end, false)} · {x.change > 0 ? "+" : ""}{x.change} errors
                </option>
              ))}
            </Select>
            <Segmented label="Audio version" hideLabel value={side} onChange={setSide} options={[{ value: "original", label: "Original" }, { value: "enhanced", label: enh }]} />
            <ClipButton key={`${side}-${k}`} fileId={c.file_id} start={c.start} end={c.end} copyId={side === "enhanced" ? q.data!.copy_id : null} label={`Play the ${side} clip`} />
          </div>
          {(["original", "enhanced"] as const).map((s) => (
            <div key={s} className={cx("flex flex-col gap-1 rounded-md border p-2.5", s === side ? "border-focus/60" : "border-track")}>
              <span className="flex items-center gap-2 text-[11px] text-muted">
                {s === "original" ? "Original audio" : enh} · <span className="num">{c[s].errors} error{c[s].errors === 1 ? "" : "s"}</span> · {c.speaker}
              </span>
              <span className="text-[13px]">
                {c[s].words.map((w, i) => (
                  <span key={i} className={cx("num mr-1", w.cause ? "underline decoration-2 underline-offset-4" : "")} style={w.cause ? { color: STAGE_COLORS[w.cause], textDecorationColor: STAGE_COLORS[w.cause] } : undefined}>
                    {w.w}
                  </span>
                ))}
              </span>
            </div>
          ))}
        </>
      )}
    </Panel>
  );
}

function Inspect({ r, base, segName, asrName, enhName, normaliser }: { r: PipeRow; base: PipeRow | undefined; segName: string; asrName: string; enhName: string; normaliser: string }) {
  const enhanced = r.enh_model_id !== null;
  const d = (k: "der" | "wer" | "cpwer") => (base && r[k] !== null && r[k] !== undefined && base[k] !== null && base[k] !== undefined ? (r[k] as number) - (base[k] as number) : null);
  const dc = d("cpwer");
  const dw = d("wer");
  return (
    <Panel>
      <span className="text-[11px] text-muted uppercase tracking-wider">Inspecting pipeline</span>
      <span className="flex flex-col gap-1 text-[13px]">
        <span className="flex items-center gap-2"><span className="text-[10px] uppercase tracking-wider w-20 text-enh">Enhance</span>{enhanced ? enhName : "none"}</span>
        <span className="flex items-center gap-2"><span className="text-[10px] uppercase tracking-wider w-20" style={{ color: STAGE_COLORS.segmentation }}>VAD / diar.</span>{segName}</span>
        <span className="flex items-center gap-2"><span className="text-[10px] uppercase tracking-wider w-20" style={{ color: STAGE_COLORS.asr }}>ASR</span>{asrName}</span>
      </span>
      <span className="flex items-baseline gap-2">
        <span className="num text-[28px]">{pct(r.cpwer ?? r.wer)}</span>
        <span className="text-xs text-muted">{r.cpwer !== null && r.cpwer !== undefined ? "cpWER" : "WER"}</span>
        {enhanced && dc !== null && <span className={cx("num text-[13px]", deltaTone(dc))}>{deltaPts(dc)} pts</span>}
      </span>
      {enhanced && base && (
        <div className="flex flex-col text-[12px]">
          {(["der", "wer", "cpwer"] as const).map((k) => (
            <div key={k} className="grid grid-cols-[1fr_auto_auto] gap-3 border-t border-track py-1">
              <span className="num">{k === "cpwer" ? "cpWER" : k.toUpperCase()}</span>
              <span className="num">{pct(base[k])} → {pct(r[k])}</span>
              <span className={cx("num w-14 text-right", deltaTone(d(k)))}>{d(k) === null ? "—" : deltaPts(d(k))}</span>
            </div>
          ))}
        </div>
      )}
      {enhanced && dc !== null && dw !== null && (
        <div className="flex flex-col gap-1.5">
          <span className="text-xs text-muted">Where the cpWER change comes from</span>
          {[
            { k: "ASR part", v: dw, color: STAGE_COLORS.asr },
            { k: "Segmentation and speakers", v: dc - dw, color: STAGE_COLORS.speaker },
          ].map((p) => (
            <div key={p.k} className="flex items-center justify-between text-[13px]">
              <span className="flex items-center gap-2 text-muted"><span className="size-2.5 rounded-sm" style={{ background: p.color }} />{p.k}</span>
              <span className={cx("num", deltaTone(p.v))}>{deltaPts(p.v)}</span>
            </div>
          ))}
          <span className="text-[11px] text-subtle">The ASR part is the WER change; the rest is segmentation and speaker attribution.</span>
        </div>
      )}
      <div className="flex flex-col">
        <Fact k="RTF (enhance + diar. + ASR)" v={rtf(r.rtf_total)} />
        <Fact k="Placement" v={!enhanced ? "—" : r.placement === "before" ? "before diarisation" : r.placement === "after" ? "after diarisation" : "ASR only"} />
      </div>
      <Link to={`/errors/pipeline/${r.run_id}${qs({ normaliser })}`} className="inline-flex items-center gap-1.5 text-[13px]">
        Open in pipeline error analysis <I.Arrow size={14} />
      </Link>
    </Panel>
  );
}
