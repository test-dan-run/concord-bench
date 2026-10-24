// The pipeline leaderboard (EVALUATE_PLAN.md §6): every enhancement x VAD / diarisation x ASR combination run
// on a dataset, ranked by cpWER (speech-only pipelines follow, by WER), with each enhanced pipeline's change
// against the same pipeline on original audio. The grid of every diarisation x ASR pair lives on the pipeline
// error page now, as "Which stage to upgrade".

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, qs, type PipeGrid, type PipeRow } from "../../lib/api";
import { deltaPts, deltaTone, hwLabel, pct, rtf } from "../../lib/format";
import * as I from "../../components/icons";
import { AttributionBar } from "../../components/StageLegend";
import { Button, cx, Empty, EnhChip, ErrorNote, Fact, Panel, Select, Spinner } from "../../components/ui";

const TOP = 15;

export function PipelinesTab({ normaliser, collar, skipOverlap, onNewRun }: { normaliser: string; collar: number; skipOverlap: boolean; onNewRun: () => void }) {
  const [dsId, setDsId] = useState<number | null>(null);
  const [fEnh, setFEnh] = useState<string>("all");
  const [fPlace, setFPlace] = useState<string>("all");
  const [fSeg, setFSeg] = useState<string>("all");
  const [fAsr, setFAsr] = useState<string>("all");
  const [showAll, setShowAll] = useState(false);
  const [pick, setPick] = useState<number | null>(null);
  const q = useQuery({
    queryKey: ["pipes", dsId, normaliser, collar, skipOverlap],
    queryFn: () => api.get<PipeGrid>(`/benchmarks-pipelines${qs({ dataset_id: dsId, normaliser, collar, skip_overlap: skipOverlap || undefined })}`),
    refetchInterval: (query) => (query.state.data?.queued || query.state.data?.cells.some((c) => c.status === "queued" || c.status === "waiting" || c.status === "running") ? 3000 : false),
  });
  if (q.isLoading) return <Spinner />;
  if (q.error || !q.data) return <ErrorNote error={q.error} />;
  const g = q.data;
  const eligible = g.datasets.filter((d) => d.has_labels);
  if (!eligible.length) return <Empty>Pipelines need a dataset with transcripts. Upload one with speaker-attributed transcripts (STM, SegLST or JSON segments) to get cpWER.</Empty>;
  const segName = (id: number | null) => g.rows.find((r) => r.seg_model_id === id)?.name ?? "";
  const asrName = (id: number) => g.cols.find((c) => c.asr_model_id === id)?.name ?? "";
  const enhName = (id: number | null) => (id === null ? "original audio" : (g.enhancers.find((e) => e.id === id)?.display_name ?? "enhancer"));
  const keep = (r: PipeRow) =>
    (fEnh === "all" || (fEnh === "none" ? r.enh_model_id === null : String(r.enh_model_id) === fEnh)) &&
    (fPlace === "all" || r.placement === fPlace) &&
    (fSeg === "all" || String(r.seg_model_id) === fSeg) &&
    (fAsr === "all" || String(r.asr_model_id) === fAsr);
  const ranked = g.leaderboard.filter(keep);
  const shown = showAll ? ranked : ranked.slice(0, TOP);
  const best = ranked.find((r) => r.cpwer !== null && r.cpwer !== undefined)?.cpwer ?? null;
  const sel = ranked.find((r) => r.run_id === pick) ?? ranked.find((r) => r.cpwer !== null && !r.oracle) ?? ranked[0];
  const base = sel ? g.leaderboard.find((r) => r.enh_model_id === null && r.seg_model_id === sel.seg_model_id && r.asr_model_id === sel.asr_model_id) : undefined;
  const building = g.copies.filter((c) => c.status === "building");
  const filters: { label: string; value: string; set: (v: string) => void; options: [string, string][] }[] = [
    { label: "Enhancer", value: fEnh, set: setFEnh, options: [["all", "All"], ["none", "None"], ...g.enhancers.map((e) => [String(e.id), e.display_name] as [string, string])] },
    { label: "Placement", value: fPlace, set: setFPlace, options: [["all", "All"], ["before", "Before"], ["after", "After"]] },
    { label: "VAD / diar.", value: fSeg, set: setFSeg, options: [["all", "All"], ...g.rows.map((r) => [String(r.seg_model_id), r.oracle ? "reference" : r.name] as [string, string])] },
    { label: "ASR", value: fAsr, set: setFAsr, options: [["all", "All"], ...g.cols.map((c) => [String(c.asr_model_id), c.name] as [string, string])] },
  ];
  return (
    <div className="flex flex-col xl:flex-row gap-5 items-start">
      <Panel
        className="flex-1 w-full"
        id="plb-h"
        title="Pipeline leaderboard"
        subtitle={`Every enhancement × VAD / diarisation × ASR combination on ${g.dataset?.name ?? ""}. Ranked by cpWER; speech-only pipelines follow, ranked by WER. Text normaliser: ${g.normaliser}.`}
        actions={
          <Select label="Dataset" value={g.dataset_id ?? ""} onChange={(e) => setDsId(Number(e.target.value))}>
            {eligible.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </Select>
        }
      >
        <div className="flex flex-wrap gap-x-5 gap-y-2">
          {filters.map((f) => (
            <span key={f.label} className="inline-flex flex-wrap items-center gap-2">
              <span className="text-[11px] text-muted uppercase tracking-wider">{f.label}</span>
              <span role="group" aria-label={f.label} className="inline-flex flex-wrap border border-line rounded-md overflow-hidden">
                {f.options.map(([v, l]) => (
                  <button key={v} type="button" aria-pressed={f.value === v} onClick={() => { f.set(v); setShowAll(false); }} className={cx("min-h-7 px-2.5 text-xs border-0 cursor-pointer", f.value === v ? "bg-active text-fg" : "bg-transparent text-muted hover:text-fg")}>
                    {l}
                  </button>
                ))}
              </span>
            </span>
          ))}
        </div>
        {g.leaderboard.length === 0 ? (
          <Empty>
            No pipeline runs on this dataset yet.{" "}
            <Button size="sm" variant="primary" onClick={onNewRun}>
              New pipeline run
            </Button>
          </Empty>
        ) : (
          <div className="overflow-x-auto scroll-thin">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="text-[11px] text-muted uppercase tracking-wider">
                  <th scope="col" className="text-right font-medium px-2 py-2 border-b border-line">#</th>
                  <th scope="col" className="text-left font-medium px-3 py-2 border-b border-line">Enhance ▸ VAD / diarisation → ASR</th>
                  <th scope="col" className="text-left font-medium px-3 py-2 border-b border-line">Placement</th>
                  <th scope="col" className="text-right font-medium px-3 py-2 border-b border-line">cpWER</th>
                  <th scope="col" className="text-right font-medium px-3 py-2 border-b border-line">vs original audio</th>
                  <th scope="col" className="text-right font-medium px-3 py-2 border-b border-line">WER</th>
                  <th scope="col" className="text-right font-medium px-3 py-2 border-b border-line">DER</th>
                  <th scope="col" className="text-right font-medium px-3 py-2 border-b border-line">RTF</th>
                  <th scope="col" className="px-3 py-2 border-b border-line">
                    <span className="sr-only">Action</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {shown.map((r, i) => {
                  const speechOnly = r.cpwer === null || r.cpwer === undefined;
                  const d = r.vs_original;
                  return (
                    <tr key={r.run_id} className={cx("border-b border-track", sel?.run_id === r.run_id && "bg-active/60 shadow-[inset_2px_0_0_#38BDF8]")}>
                      <td className="px-2 py-2 text-right num text-subtle">{i + 1}</td>
                      <td className="px-3 py-2 min-w-[340px]">
                        <span className="flex flex-wrap items-center gap-1.5 whitespace-nowrap">
                          {r.enh_model_id === null ? <span className="inline-flex rounded px-1.5 py-px text-[11px] bg-track text-muted">original audio</span> : <EnhChip>{enhName(r.enh_model_id)}{r.generative ? " · generative" : ""}</EnhChip>}
                          <span className="text-subtle">▸</span>
                          <span className={cx(r.oracle && "text-secondary")}>{segName(r.seg_model_id)}</span>
                          <span className="text-subtle">→</span>
                          <span>{asrName(r.asr_model_id)}</span>
                        </span>
                      </td>
                      <td className="px-3 py-2 text-xs text-muted">{r.enh_model_id === null ? "—" : r.placement === "before" ? "Before diarisation" : r.placement === "after" ? "After diarisation" : "ASR only"}</td>
                      <td className={cx("px-3 py-2 text-right num", !speechOnly && r.cpwer === best && "text-good font-medium")}>{speechOnly ? <span className="text-subtle">n/a</span> : pct(r.cpwer)}</td>
                      <td className={cx("px-3 py-2 text-right num text-xs", d === undefined ? "text-subtle" : d < -0.0005 ? "text-good" : d > 0.0005 ? "text-bad" : "text-muted")}>
                        {r.enh_model_id === null ? "baseline" : d === undefined ? "—" : `${deltaPts(d)} pts${r.vs_metric === "wer" ? " WER" : ""}`}
                      </td>
                      <td className="px-3 py-2 text-right num">{pct(r.wer)}</td>
                      <td className="px-3 py-2 text-right num">{r.oracle ? <span className="text-subtle">oracle</span> : r.der === null || r.der === undefined ? <span className="text-subtle">n/a</span> : pct(r.der)}</td>
                      <td className="px-3 py-2 text-right num text-xs">{rtf(r.rtf_total)}</td>
                      <td className="px-3 py-2 text-right">
                        {speechOnly ? <span className="text-[11px] text-subtle">WER only</span> : <Button size="sm" onClick={() => setPick(r.run_id)}>Inspect</Button>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
          <span className="num">
            Showing {shown.length} of {ranked.length} pipelines
          </span>
          {ranked.length > TOP && (
            <Button size="sm" onClick={() => setShowAll((s) => !s)}>
              {showAll ? `Show top ${TOP}` : `Show all ${ranked.length}`}
            </Button>
          )}
          {(g.queued ?? 0) > 0 && (
            <span className="inline-flex items-center gap-1.5 text-enh-soft">
              <span className="size-2 rounded-full bg-enh animate-pulse" />
              {g.queued} queued{building.length ? `: waiting for ${building.map((c) => enhName(c.enh_model_id)).join(", ")} copies` : ""}
            </span>
          )}
        </div>
      </Panel>
      <aside aria-label="Selected pipeline" className="w-full xl:w-[360px] shrink-0 flex flex-col gap-4">
        {sel && sel.score_id ? (
          <PipeDetail r={sel} base={base} seg={segName(sel.seg_model_id)} asr={asrName(sel.asr_model_id)} enh={enhName(sel.enh_model_id)} ds={g.dataset?.name ?? ""} normaliser={normaliser} collar={collar} copy={g.copies.find((c) => c.enh_model_id === sel.enh_model_id)?.status} />
        ) : (
          <Panel title="Inspecting pipeline">
            <span className="text-[13px] text-muted">Run a pipeline, then inspect it here.</span>
          </Panel>
        )}
        <Panel title="How pipelines are scored">
          <p className="m-0 text-[13px] text-muted leading-relaxed">
            <span className="text-fg num">cpWER</span> matches each reference speaker to one output speaker, then counts word errors, so words given to the wrong speaker count as errors (twice: missing from one speaker, extra for another).
          </p>
          <p className="m-0 text-[13px] text-muted leading-relaxed">
            <span className="text-fg num">WER</span> ignores speakers. Speech-only VAD models get WER, not cpWER.
          </p>
          <p className="m-0 text-[13px] text-muted leading-relaxed">
            <span className="text-enh-soft">Enhancement placement.</span> Before diarisation, both stages hear the enhanced copy. After diarisation, diarisation hears the original and ASR hears the enhanced copy, cut at the diarised segments.
          </p>
        </Panel>
      </aside>
    </div>
  );
}

function PipeDetail({ r, base, seg, asr, enh, ds, normaliser, collar, copy }: { r: PipeRow; base: PipeRow | undefined; seg: string; asr: string; enh: string; ds: string; normaliser: string; collar: number; copy?: string }) {
  const enhanced = r.enh_model_id !== null;
  const segHears = enhanced && r.placement === "before" ? "enhanced" : "original";
  const route = [
    { tag: "Enhance", color: "var(--color-enh)", name: enhanced ? enh : "none", hears: null },
    { tag: "VAD / diar.", color: "var(--color-stage-seg)", name: seg, hears: r.oracle ? null : segHears },
    { tag: "ASR", color: "var(--color-stage-asr)", name: asr, hears: enhanced ? "enhanced" : "original" },
  ];
  const cmp = (["der", "wer", "cpwer"] as const).map((k) => ({ k, from: base?.[k] ?? null, to: r[k] ?? null }));
  const h = r.hardware ? Object.values(r.hardware).find((x) => x.device) : undefined;
  return (
    <Panel>
      <div className="flex flex-col gap-1.5">
        <span className="text-[11px] text-muted uppercase tracking-wider">Inspecting pipeline</span>
        <span className="flex flex-col gap-1 text-[13px]">
          {route.map((s) => (
            <span key={s.tag} className="flex items-center gap-2">
              <span className="text-[10px] uppercase tracking-wider w-24 shrink-0" style={{ color: s.color }}>
                {s.tag}
              </span>
              <span className="flex-1 min-w-0 truncate">{s.name}</span>
              {s.hears && <span className={cx("text-[11px] px-2 rounded-full", s.hears === "enhanced" ? "bg-enh/15 text-enh-soft" : "bg-track text-muted")}>{s.hears}</span>}
            </span>
          ))}
        </span>
        <span className="text-xs text-muted">
          {ds} · {!enhanced ? "original audio throughout" : r.placement === "before" ? "enhanced before diarisation" : r.placement === "after" ? "enhanced after diarisation" : "enhanced for ASR"}
        </span>
      </div>
      <div className="flex items-baseline gap-2">
        <span className="num text-[30px] font-medium">{r.cpwer === null || r.cpwer === undefined ? pct(r.wer) : pct(r.cpwer)}</span>
        <span className="text-xs text-muted">{r.cpwer === null || r.cpwer === undefined ? "WER (speech-only, no cpWER)" : "cpWER"}</span>
        {enhanced && r.vs_original !== undefined && <span className={cx("num text-[13px]", deltaTone(r.vs_original))}>{deltaPts(r.vs_original)} pts</span>}
      </div>
      {enhanced && base && (
        <div className="flex flex-col text-[12px]">
          <div className="grid grid-cols-[1fr_auto_auto] gap-3 text-[11px] text-muted uppercase tracking-wider pb-1">
            <span>Metric</span>
            <span>Original → this</span>
            <span className="w-16 text-right">Change</span>
          </div>
          {cmp.map((c) => (
            <div key={c.k} className="grid grid-cols-[1fr_auto_auto] gap-3 border-t border-track py-1">
              <span className="num">{c.k === "cpwer" ? "cpWER" : c.k.toUpperCase()}</span>
              <span className="num">
                {pct(c.from)} → {pct(c.to)}
              </span>
              <span className={cx("num w-16 text-right", c.from === null || c.to === null ? "text-subtle" : deltaTone(c.to - c.from))}>{c.from === null || c.to === null ? "—" : deltaPts(c.to - c.from)}</span>
            </div>
          ))}
        </div>
      )}
      {r.attribution && !r.oracle && !enhanced && (
        <div className="flex flex-col gap-2">
          <span className="text-xs text-muted">Where the errors come from</span>
          <AttributionBar att={r.attribution} />
        </div>
      )}
      <div className="flex flex-col">
        <Fact k="Total RTF" v={rtf(r.rtf_total)} />
        {enhanced && <Fact k="Enhanced copy" v={copy ?? "deleted"} />}
        <Fact k="Ran on" v={hwLabel(h)} />
      </div>
      {r.run_id && (
        <span className="flex flex-col gap-1.5">
          {enhanced && (
            <Link to={`/errors/enh/${r.run_id}${qs({ normaliser })}`} className="inline-flex items-center gap-1.5 text-[13px]">
              Open enhancement error analysis <I.Arrow size={14} />
            </Link>
          )}
          <Link to={`/errors/pipeline/${r.run_id}${qs({ normaliser, collar })}`} className="inline-flex items-center gap-1.5 text-[13px]">
            Open pipeline error analysis <I.Arrow size={14} />
          </Link>
        </span>
      )}
    </Panel>
  );
}
