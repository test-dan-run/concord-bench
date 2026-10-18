import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, qs, type Dataset, type LiveEval, type LiveModel } from "../../lib/api";
import { C, MODEL_SERIES } from "../../lib/colors";
import { IfFeature, useNormalisers } from "../../lib/config";
import { ago, clock, deltaPts, num, pct } from "../../lib/format";
import * as I from "../../components/icons";
import { Button, Empty, ErrorNote, Fact, PageHeader, Panel, Select, Spinner } from "../../components/ui";
import { Crumbs, DatasetChooser, LabelTabs } from "./common";

export function LiveEvalPage() {
  const { datasetId } = useParams();
  if (!datasetId) {
    return <DatasetChooser title="Live evaluation" to={(id) => `/label/live/${id}`} subtitle="Model scores against the segments you have confirmed so far." />;
  }
  return <Live datasetId={Number(datasetId)} />;
}

/** Colour per model, by model id (never by rank); models past the palette get none and stay table-only. */
function seriesColors(models: LiveModel[]): Map<number, string> {
  const ids = [...new Set(models.map((m) => m.model_id))].sort((a, b) => a - b);
  return new Map(ids.slice(0, MODEL_SERIES.length).map((id, k) => [id, MODEL_SERIES[k]]));
}

function Live({ datasetId }: { datasetId: number }) {
  const norms = useNormalisers();
  const [normaliser, setNormaliser] = useState("basic");
  const q = useQuery({
    queryKey: ["label-live", datasetId, normaliser],
    queryFn: () => api.get<LiveEval>(`/label/datasets/${datasetId}/live${qs({ normaliser })}`),
    refetchInterval: 10_000,
  });
  const d = q.data;
  const colors = useMemo(() => seriesColors(d?.asr.models ?? []), [d]);
  return (
    <>
      <PageHeader
        crumbs={<Crumbs name={d?.dataset.name} />}
        title="Live evaluation"
        subtitle="Scores against confirmed segments only, using model outputs already saved (no new inference)."
        actions={
          <Select label="Normaliser" value={normaliser} onChange={(e) => setNormaliser(e.target.value)}>
            {(norms.data ?? [{ id: "basic", key: "basic@1", label: "Lowercase + strip punct." }]).map((n) => (
              <option key={n.id} value={n.id}>{n.label}</option>
            ))}
          </Select>
        }
      />
      <LabelTabs datasetId={datasetId} current="live" />
      <ErrorNote error={q.error} />
      {q.isLoading && <Spinner />}
      {d && (
        <>
          <div className="grid gap-5 xl:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
            <Panel
              title="WER vs confirmed segments"
              subtitle={`${num(d.asr.confirmed)} confirmed${d.asr.ranked ? "" : ` · too few to rank below ${d.asr.min_rank}`}`}
            >
              {d.asr.models.length === 0 ? (
                <Empty>
                  {d.asr.confirmed === 0 ? (
                    <>No confirmed segments yet. <Link to={`/label/annotate/${datasetId}`}>Start annotating</Link>.</>
                  ) : (
                    "No model has saved outputs for the confirmed segments. Pseudo-label or benchmark the dataset first."
                  )}
                </Empty>
              ) : (
                <WerCurve models={d.asr.models} colors={colors} total={d.asr.confirmed} minRank={d.asr.min_rank} />
              )}
            </Panel>
            <Standings d={d} colors={colors} />
          </div>
          <div className="grid gap-5 lg:grid-cols-3">
            <Panel title="Diarisation (DER)" subtitle={`${num(d.der.files)} of ${num(d.der.of)} files fully reviewed · collar ${d.der.collar} s`}>
              {d.der.files === 0 && <span className="text-[13px] text-muted">DER needs files where every segment is reviewed, so missing labels never count as misses.</span>}
              {d.der.files > 0 && d.der.models.length === 0 && <span className="text-[13px] text-muted">No diariser has saved outputs for these files.</span>}
              {d.der.models.map((m) => (
                <Fact key={m.model_id} k={m.name} v={<>{pct(m.der)} <span className="text-subtle">· JER {pct(m.jer)}</span></>} />
              ))}
            </Panel>
            <Panel title="Keyword spotting">
              <span className="text-[13px] text-muted">Not built yet (Q6). Package files keep an empty keywords.jsonl so progress stays forward-compatible.</span>
            </Panel>
            <Promote datasetId={datasetId} confirmed={d.asr.confirmed} />
          </div>
          <Panel title="Latest confirmations">
            {d.feed.length === 0 && <span className="text-[13px] text-muted">Nothing confirmed by hand yet.</span>}
            <ul className="m-0 p-0 list-none flex flex-col">
              {d.feed.map((f) => (
                <li key={f.id} className="border-b border-track last:border-0">
                  <Link to={`/label/annotate/${datasetId}?file=${f.file_id}&segment=${f.id}`} className="grid grid-cols-[110px_minmax(0,1fr)_80px] gap-3 px-1.5 py-2 no-underline !text-fg hover:bg-track rounded text-[13px]">
                    <span className="num text-xs text-muted">{f.file} · {clock(f.start, false)}</span>
                    <span className="truncate">{f.speaker && <span className="text-muted num">{f.speaker}: </span>}{f.text}</span>
                    <span className="text-xs text-muted text-right">{ago(f.confirmed_at)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </Panel>
        </>
      )}
    </>
  );
}

function Standings({ d, colors }: { d: LiveEval; colors: Map<number, string> }) {
  return (
    <Panel title="Standings" subtitle={d.asr.ranked ? "95% intervals: bootstrap over segments." : `Too few to rank: ${d.asr.min_rank} confirmed segments needed.`}>
      {d.asr.models.length === 0 && <span className="text-[13px] text-muted">No models to compare yet.</span>}
      {d.asr.models.length > 0 && (
        <div className="overflow-x-auto scroll-thin">
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
                <th scope="col" className="font-medium px-2 py-2 border-b border-line">#</th>
                <th scope="col" className="font-medium px-2 py-2 border-b border-line">Model</th>
                <th scope="col" className="font-medium px-2 py-2 border-b border-line text-right">WER</th>
                <th scope="col" className="font-medium px-2 py-2 border-b border-line text-right">95% CI</th>
                <th scope="col" className="font-medium px-2 py-2 border-b border-line text-right" title="Benchmark score on the promoted (locked) dataset">Locked</th>
              </tr>
            </thead>
            <tbody>
              {d.asr.models.map((m) => (
                <tr key={m.model_id} className="border-b border-track">
                  <td className="px-2 py-2 num text-muted">{m.rank ?? "—"}</td>
                  <td className="px-2 py-2">
                    <span className="flex items-center gap-2">
                      <span className="size-2.5 rounded-sm shrink-0" style={{ background: colors.get(m.model_id) ?? "transparent", border: colors.get(m.model_id) ? undefined : `1px solid ${C.line}` }} />
                      <span className="truncate">{m.name}</span>
                    </span>
                    {m.coverage < d.asr.confirmed && <span className="text-[11px] text-subtle num">{m.coverage}/{d.asr.confirmed} segments</span>}
                  </td>
                  <td className="px-2 py-2 text-right num">{pct(m.wer)}</td>
                  <td className="px-2 py-2 text-right num text-muted">{m.ci ? `${pct(m.ci[0])}–${pct(m.ci[1])}` : "—"}</td>
                  <td className="px-2 py-2 text-right num">
                    {m.locked ? (
                      <span title={`${m.locked.promoted ? "Promoted dataset" : "This dataset"} · run ${m.locked.run_id} · ${m.locked.normaliser}`}>
                        {pct(m.locked.wer)}
                        {m.wer !== null && m.locked.wer !== null && <span className="text-[11px] text-muted"> ({deltaPts(m.wer - m.locked.wer)})</span>}
                      </span>
                    ) : (
                      <span className="text-subtle">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

const W = 640;
const H = 240;
const PAD = { l: 44, r: 112, t: 12, b: 28 };

/** One 2px line per model; the zone below `minRank` is shaded "too few to rank"; crosshair tooltip. */
function WerCurve({ models, colors, total, minRank }: { models: LiveModel[]; colors: Map<number, string>; total: number; minRank: number }) {
  const shown = models.filter((m) => colors.has(m.model_id));
  const box = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const ymax = Math.max(0.05, ...shown.flatMap((m) => m.curve.map((p) => p.wer ?? 0))) * 1.1;
  const x = (k: number) => PAD.l + ((k - 1) / Math.max(1, total - 1)) * (W - PAD.l - PAD.r);
  const y = (v: number) => PAD.t + (1 - v / ymax) * (H - PAD.t - PAD.b);
  const yTicks = [0, ymax / 3, (2 * ymax) / 3].map((v) => Math.round(v * 1000) / 1000);
  const at = (m: LiveModel, k: number) => {
    let best = m.curve[0];
    for (const p of m.curve) if (p.confirmed <= k) best = p;
    return best;
  };
  const onMove = (e: React.MouseEvent) => {
    const r = box.current?.getBoundingClientRect();
    if (!r) return;
    const px = ((e.clientX - r.left) / r.width) * W;
    const k = Math.round(1 + ((px - PAD.l) / (W - PAD.l - PAD.r)) * Math.max(1, total - 1));
    setHover(Math.max(1, Math.min(total, k)));
  };
  return (
    <div className="flex flex-col gap-2">
      <span className="flex flex-wrap gap-3 text-[11px] text-muted" aria-hidden>
        {shown.map((m) => (
          <span key={m.model_id} className="flex items-center gap-1.5">
            <span className="w-3 h-0.5 rounded" style={{ background: colors.get(m.model_id) }} />
            {m.name}
          </span>
        ))}
        {models.length > shown.length && <span>{models.length - shown.length} more in the table</span>}
      </span>
      <div className="relative">
        <svg ref={box} viewBox={`0 0 ${W} ${H}`} className="w-full h-auto block" role="img" aria-label={`WER of ${shown.length} models against ${total} confirmed segments`} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
          {total >= 1 && minRank > 1 && (
            <g>
              <rect x={PAD.l} y={PAD.t} width={Math.max(0, Math.min(x(Math.min(total, minRank)), W - PAD.r) - PAD.l)} height={H - PAD.t - PAD.b} fill={C.track} opacity={0.6} />
              <text x={PAD.l + 6} y={PAD.t + 14} fontSize={10} fill={C.subtle}>too few to rank</text>
            </g>
          )}
          {yTicks.map((v) => (
            <g key={v}>
              <line x1={PAD.l} x2={W - PAD.r} y1={y(v)} y2={y(v)} stroke={C.track} />
              <text x={PAD.l - 6} y={y(v) + 3} fontSize={10} textAnchor="end" fill={C.subtle} fontFamily="JetBrains Mono Variable, monospace">{`${(v * 100).toFixed(0)}%`}</text>
            </g>
          ))}
          <line x1={PAD.l} x2={W - PAD.r} y1={y(0)} y2={y(0)} stroke={C.line} />
          {[1, total].filter((v, i, a) => a.indexOf(v) === i).map((k) => (
            <text key={k} x={x(k)} y={H - 8} fontSize={10} textAnchor="middle" fill={C.subtle} fontFamily="JetBrains Mono Variable, monospace">{k}</text>
          ))}
          <text x={(PAD.l + W - PAD.r) / 2} y={H - 8} fontSize={10} textAnchor="middle" fill={C.subtle}>confirmed segments</text>
          {shown.map((m) => {
            const pts = m.curve.filter((p) => p.wer !== null);
            const color = colors.get(m.model_id)!;
            const last = pts[pts.length - 1];
            return (
              <g key={m.model_id}>
                <polyline fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" points={pts.map((p) => `${x(p.confirmed)},${y(p.wer!)}`).join(" ")} />
                {last && (
                  <>
                    <circle cx={x(last.confirmed)} cy={y(last.wer!)} r={4} fill={color} stroke={C.panel} strokeWidth={2} />
                    <text x={x(last.confirmed) + 8} y={y(last.wer!) + 3} fontSize={10} fill={C.muted}>{m.name.length > 16 ? `${m.name.slice(0, 15)}…` : m.name}</text>
                  </>
                )}
              </g>
            );
          })}
          {hover !== null && <line x1={x(hover)} x2={x(hover)} y1={PAD.t} y2={H - PAD.b} stroke={C.focus} strokeDasharray="3 3" />}
        </svg>
        {hover !== null && (
          <div className="absolute top-2 pointer-events-none bg-panel border border-line rounded-md px-2.5 py-1.5 text-xs shadow-lg" style={{ left: `${Math.min(70, (x(hover) / W) * 100 + 2)}%` }}>
            <div className="text-muted num mb-1">{hover} confirmed</div>
            {shown.map((m) => (
              <div key={m.model_id} className="flex items-center gap-2">
                <span className="w-2.5 h-0.5 rounded" style={{ background: colors.get(m.model_id) }} />
                <span className="text-muted">{m.name}</span>
                <span className="num ml-auto text-fg">{pct(at(m, hover)?.wer)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      <details className="text-xs text-muted">
        <summary className="cursor-pointer">Table view</summary>
        <table className="mt-2 border-collapse num">
          <thead>
            <tr>
              <th scope="col" className="px-2 py-1 text-left font-medium">Confirmed</th>
              {shown.map((m) => <th key={m.model_id} scope="col" className="px-2 py-1 text-right font-medium">{m.name}</th>)}
            </tr>
          </thead>
          <tbody>
            {(shown[0]?.curve ?? []).map((p) => (
              <tr key={p.confirmed}>
                <td className="px-2 py-0.5">{p.confirmed}</td>
                {shown.map((m) => <td key={m.model_id} className="px-2 py-0.5 text-right">{pct(at(m, p.confirmed)?.wer)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}

function Promote({ datasetId, confirmed }: { datasetId: number; confirmed: number }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [scope, setScope] = useState("complete");
  const go = useMutation({
    mutationFn: () => api.post<Dataset>(`/label/datasets/${datasetId}/promote`, { name: name.trim(), scope }),
    onSuccess: (d) => {
      void qc.invalidateQueries({ queryKey: ["datasets"] });
      void qc.invalidateQueries({ queryKey: ["label-console"] });
      navigate(`/datasets/${d.id}`);
    },
  });
  return (
    <Panel title="Promote to benchmark dataset" subtitle="Copies confirmed segments only into a new dataset you can benchmark.">
      <label className="flex flex-col gap-1 text-xs text-muted">
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Calls · reviewed" className="bg-canvas border border-line rounded px-2 min-h-9 text-fg text-[13px]" />
      </label>
      <Select label="Files" value={scope} onChange={(e) => setScope(e.target.value)}>
        <option value="complete">Fully reviewed files (ASR + DER)</option>
        <option value="any">Any file with confirmed segments (ASR only)</option>
      </Select>
      <ErrorNote error={go.error} />
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" disabled={!name.trim() || !confirmed || go.isPending} onClick={() => go.mutate()}>
          <I.Database size={14} /> Promote
        </Button>
        <IfFeature name="improve">
          <Button disabled title="Training (Phase 3) is not scoped yet">
            <I.Flask size={14} /> Use for retraining
          </Button>
        </IfFeature>
      </div>
    </Panel>
  );
}
