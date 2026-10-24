// Benchmark plan: catalog models grouped by stage, ranked by tag overlap and pre-ticked when tags match;
// the oracle under VAD / diarisation when speaker labels exist; "Run as" grid or each stage alone.
// Each model shows its state in the catalog (running, stopped, starting, queued), what the verifier found
// and its cold start. With a dataset, the catalog's plan is shown before anything is queued: stages, the
// model loads it expects, outputs already in the index, and the estimated load time.
// Shared by the Upload page (before the dataset exists) and the Benchmarks "Run evaluation" panel.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, qs, type CatalogInfo, type CatalogModel, type PlanPreview } from "../../lib/api";
import { capLabel, coldStart, modelStateLabel } from "../../lib/format";
import { Chip, cx, ErrorNote, StatusDot, Toggle } from "../../components/ui";

export interface PlanValue {
  seg_models: string[];
  asr_models: string[];
  oracle: boolean;
  mode: "grid" | "stages";
}

export const EMPTY_PLAN: PlanValue = { seg_models: [], asr_models: [], oracle: false, mode: "grid" };

type Suggested = CatalogModel & { shared_tags: string[]; preselect: boolean };

interface Suggestions {
  seg: Suggested[];
  asr: Suggested[];
  oracle_available: boolean;
  labels: string[];
  catalog: CatalogInfo;
}

type Key = "seg_models" | "asr_models";

export function planCount(p: PlanValue, labels: string[]): number {
  const hasAsr = labels.includes("asr");
  const hasDiar = labels.includes("diar");
  if (p.mode === "grid") return hasAsr ? (p.seg_models.length + (p.oracle && hasDiar ? 1 : 0)) * p.asr_models.length : 0;
  return (hasDiar ? p.seg_models.length : 0) + (hasAsr ? p.asr_models.length : 0);
}

/** A preset plan limited to the models the catalog still offers (and has verified) for this dataset. */
export function presetFor(preset: PlanValue, data: Pick<Suggestions, "seg" | "asr" | "oracle_available">): PlanValue {
  const seg = new Set(data.seg.filter((m) => m.usable).map((m) => m.catalog_id));
  const asr = new Set(data.asr.filter((m) => m.usable).map((m) => m.catalog_id));
  return {
    ...preset,
    seg_models: preset.seg_models.filter((i) => seg.has(i)),
    asr_models: preset.asr_models.filter((i) => asr.has(i)),
    oracle: preset.oracle && data.oracle_available,
  };
}

function ModelRow({ m, checked, onToggle, stage }: { m: Suggested; checked: boolean; onToggle: () => void; stage: Key }) {
  const caps = m.capabilities.filter((c) => (stage === "seg_models" ? c !== "asr" : c === "asr"));
  return (
    <label
      title={m.not_usable ?? m.state_detail ?? undefined}
      className={cx("flex items-center gap-2.5 min-h-9 px-2 rounded-md", m.usable ? "cursor-pointer hover:bg-track" : "opacity-60 cursor-not-allowed", checked && "bg-active/60")}
    >
      <input type="checkbox" checked={checked} disabled={!m.usable && !checked} onChange={onToggle} className="accent-[#06B6D4]" />
      <span className="text-[13px] flex-1 truncate">{m.display_name}</span>
      <span className="flex flex-wrap items-center justify-end gap-1">
        <span className="inline-flex items-center gap-1 text-[11px] text-muted">
          <StatusDot state={m.state ?? "unknown"} />
          {modelStateLabel(m.state)}
        </span>
        {m.usable ? (
          caps.map((c) => <Chip key={c}>{capLabel(c)}</Chip>)
        ) : (
          <Chip tone="warn">{m.capability_status === "failed" ? "verification failed" : "not verified"}</Chip>
        )}
        <span className="num text-[11px] text-subtle" title="cold start">
          {coldStart(m.cold_start_s, m.cold_start_measured)}
        </span>
        {m.shared_tags.slice(0, 2).map((t) => (
          <Chip key={t} tone="info">
            #{t}
          </Chip>
        ))}
      </span>
    </label>
  );
}

/** The catalog's plan for this selection (POST /benchmarks/plan/preview). */
function CatalogPlan({ datasetId, value }: { datasetId: number; value: PlanValue }) {
  const body = { dataset_id: datasetId, ...value };
  const empty = !value.seg_models.length && !value.asr_models.length;
  const { data, error, isFetching } = useQuery({
    queryKey: ["plan-preview", body],
    queryFn: () => api.post<PlanPreview>("/benchmarks/plan/preview", body),
    enabled: !empty,
  });
  if (empty) return null;
  if (error) return <ErrorNote error={error} />;
  if (!data) return <span className="text-xs text-muted">{isFetching ? "Asking the model catalog for a plan…" : ""}</span>;
  if (data.error) return <span className="text-xs text-warn">No plan from the model catalog: {data.error}</span>;
  if (!data.plan) return null;
  const p = data.plan;
  return (
    <div className="flex flex-col gap-1.5" aria-label="Plan">
      <span className="text-[11px] text-muted uppercase tracking-wider">
        Plan {value.mode === "grid" ? "· stage by stage, each model loads once" : ""}
      </span>
      <ol className="m-0 p-0 list-none flex flex-col gap-1">
        {p.steps.map((s) => (
          <li key={`${s.stage}-${s.model}`} className="flex flex-wrap items-center gap-2 text-xs">
            <Chip>{s.stage === "seg" ? "VAD / diarisation" : "ASR"}</Chip>
            <span className="flex-1 min-w-[140px] num">{s.model}</span>
            <span className="text-muted">
              {s.skip ? "all stored: skipped" : s.load ? `loads (${coldStart(s.cold_start_s)})` : "already running"}
            </span>
            {s.items !== null && (
              <span className="num text-subtle">
                {s.hits ?? 0}/{s.items} in the index
              </span>
            )}
          </li>
        ))}
      </ol>
      <span className="num text-xs text-muted">
        {p.loads} model load{p.loads === 1 ? "" : "s"} · about {coldStart(p.est_load_s)} loading · {p.hits} output{p.hits === 1 ? "" : "s"} already in the index
      </span>
    </div>
  );
}

export function PlanPanel({
  datasetId,
  tags,
  labelTypes,
  language,
  value,
  onChange,
  preset,
}: {
  datasetId?: number;
  tags?: string[];
  labelTypes?: string[];
  language?: string;
  value: PlanValue;
  onChange: (v: PlanValue) => void;
  /** Pre-tick these instead of the tag matches (e.g. the models run on a dataset's previous version). */
  preset?: PlanValue;
}) {
  const query = datasetId
    ? qs({ dataset_id: datasetId })
    : qs({ tags: (tags ?? []).join(","), label_types: (labelTypes ?? []).join(","), language });
  const { data } = useQuery({ queryKey: ["suggest-models", query], queryFn: () => api.get<Suggestions>(`/suggestions/models${query}`), refetchInterval: 10000 });
  const [touched, setTouched] = useState(false);

  // Pre-tick on tag match (or the preset) until the user changes something.
  useEffect(() => {
    if (!data || touched) return;
    if (preset) {
      onChange(presetFor(preset, data));
      return;
    }
    onChange({
      ...value,
      seg_models: data.seg.filter((m) => m.preselect).map((m) => m.catalog_id),
      asr_models: data.asr.filter((m) => m.preselect).map((m) => m.catalog_id),
      oracle: data.oracle_available,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, touched]);

  const set = (v: Partial<PlanValue>) => {
    setTouched(true);
    onChange({ ...value, ...v });
  };
  const toggle = (key: Key, id: string) => set({ [key]: value[key].includes(id) ? value[key].filter((x) => x !== id) : [...value[key], id] } as Partial<PlanValue>);

  if (!data) return <span className="text-xs text-muted">Loading models…</span>;
  const labels = data.labels.length ? data.labels : (labelTypes ?? []);

  const group = (title: string, key: Key, items: Suggested[], extra?: React.ReactNode) => (
    <fieldset className="border-0 p-0 m-0 flex flex-col gap-1.5">
      <legend className="text-[11px] text-muted uppercase tracking-wider mb-1">{title}</legend>
      {extra}
      {items.map((m) => (
        <ModelRow key={m.catalog_id} m={m} stage={key} checked={value[key].includes(m.catalog_id)} onToggle={() => toggle(key, m.catalog_id)} />
      ))}
      {items.length === 0 && !extra && (
        <span className="text-xs text-subtle px-2">
          No catalog models for this stage. Register one in the{" "}
          <a href={data.catalog.ui_url} target="_blank" rel="noreferrer">
            model catalog
          </a>
          .
        </span>
      )}
    </fieldset>
  );

  return (
    <div className="flex flex-col gap-4">
      {!data.catalog.available && <ErrorNote error={`The model catalog is not reachable (${data.catalog.error}).`} />}
      {group(
        "VAD / diarisation",
        "seg_models",
        data.seg,
        data.oracle_available ? (
          <label className={cx("flex items-center gap-2.5 min-h-9 px-2 rounded-md cursor-pointer hover:bg-track", value.oracle && "bg-active/60")}>
            <input type="checkbox" checked={value.oracle} onChange={() => set({ oracle: !value.oracle })} className="accent-[#06B6D4]" />
            <span className="text-[13px] flex-1">Reference segments + speakers</span>
            <Chip>oracle</Chip>
          </label>
        ) : undefined,
      )}
      {group("ASR", "asr_models", data.asr)}
      <div className="flex flex-col gap-1.5">
        <span className="text-[11px] text-muted uppercase tracking-wider">Run as</span>
        <Toggle
          label="Run as"
          value={value.mode}
          onChange={(m) => set({ mode: m as PlanValue["mode"] })}
          options={[
            { value: "grid", label: "Full grid of pipelines" },
            { value: "stages", label: "Each stage alone" },
          ]}
        />
        {value.mode === "grid" && !labels.includes("asr") && <span className="text-xs text-warn">Pipelines need transcripts; this dataset has none.</span>}
      </div>
      {datasetId !== undefined && planCount(value, labels) > 0 && <CatalogPlan datasetId={datasetId} value={value} />}
      <span className="num text-xs text-muted">
        {planCount(value, labels)} run{planCount(value, labels) === 1 ? "" : "s"} · the model catalog starts models as their turn comes
      </span>
    </div>
  );
}
