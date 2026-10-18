// Benchmark plan: models grouped by stage, ranked by tag overlap and pre-ticked when tags match; the
// oracle under VAD / diarisation when speaker labels exist; "Run as" grid or each stage alone.
// Shared by the Upload page (before the dataset exists) and the Benchmarks "Run evaluation" panel.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, qs } from "../../lib/api";
import { capLabel } from "../../lib/format";
import { Chip, cx, Toggle } from "../../components/ui";

export interface PlanValue {
  seg_model_ids: number[];
  asr_model_ids: number[];
  oracle: boolean;
  mode: "grid" | "stages";
}

interface Suggested {
  model_id: number;
  display_name: string;
  shared_tags: string[];
  preselect: boolean;
  capabilities: string[];
  has_speakers: boolean;
  timing_level: string | null;
}

interface Suggestions {
  seg: Suggested[];
  asr: Suggested[];
  oracle_available: boolean;
  labels: string[];
}

export function planCount(p: PlanValue, labels: string[]): number {
  const hasAsr = labels.includes("asr");
  const hasDiar = labels.includes("diar");
  if (p.mode === "grid") return hasAsr ? (p.seg_model_ids.length + (p.oracle && hasDiar ? 1 : 0)) * p.asr_model_ids.length : 0;
  return (hasDiar ? p.seg_model_ids.length : 0) + (hasAsr ? p.asr_model_ids.length : 0);
}

export function PlanPanel({
  datasetId,
  tags,
  labelTypes,
  language,
  value,
  onChange,
}: {
  datasetId?: number;
  tags?: string[];
  labelTypes?: string[];
  language?: string;
  value: PlanValue;
  onChange: (v: PlanValue) => void;
}) {
  const query = datasetId
    ? qs({ dataset_id: datasetId })
    : qs({ tags: (tags ?? []).join(","), label_types: (labelTypes ?? []).join(","), language });
  const { data } = useQuery({ queryKey: ["suggest-models", query], queryFn: () => api.get<Suggestions>(`/suggestions/models${query}`) });
  const [touched, setTouched] = useState(false);

  // Pre-tick on tag match until the user changes something.
  useEffect(() => {
    if (!data || touched) return;
    onChange({
      ...value,
      seg_model_ids: data.seg.filter((m) => m.preselect).map((m) => m.model_id),
      asr_model_ids: data.asr.filter((m) => m.preselect).map((m) => m.model_id),
      oracle: data.oracle_available,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, touched]);

  const set = (v: Partial<PlanValue>) => {
    setTouched(true);
    onChange({ ...value, ...v });
  };
  const toggle = (key: "seg_model_ids" | "asr_model_ids", id: number) =>
    set({ [key]: value[key].includes(id) ? value[key].filter((x) => x !== id) : [...value[key], id] } as Partial<PlanValue>);

  if (!data) return <span className="text-xs text-muted">Loading models…</span>;
  const labels = data.labels.length ? data.labels : (labelTypes ?? []);

  const group = (title: string, key: "seg_model_ids" | "asr_model_ids", items: Suggested[], extra?: React.ReactNode) => (
    <fieldset className="border-0 p-0 m-0 flex flex-col gap-1.5">
      <legend className="text-[11px] text-muted uppercase tracking-wider mb-1">{title}</legend>
      {extra}
      {items.map((m) => (
        <label key={m.model_id} className={cx("flex items-center gap-2.5 min-h-9 px-2 rounded-md cursor-pointer hover:bg-track", value[key].includes(m.model_id) && "bg-active/60")}>
          <input type="checkbox" checked={value[key].includes(m.model_id)} onChange={() => toggle(key, m.model_id)} className="accent-[#06B6D4]" />
          <span className="text-[13px] flex-1 truncate">{m.display_name}</span>
          <span className="flex gap-1">
            {m.capabilities.filter((c) => (key === "seg_model_ids" ? c !== "asr" : c === "asr")).map((c) => (
              <Chip key={c}>{capLabel(c)}</Chip>
            ))}
            {m.shared_tags.slice(0, 2).map((t) => (
              <Chip key={t} tone="info">
                #{t}
              </Chip>
            ))}
          </span>
        </label>
      ))}
      {items.length === 0 && !extra && <span className="text-xs text-subtle px-2">No registered models for this stage.</span>}
    </fieldset>
  );

  return (
    <div className="flex flex-col gap-4">
      {group(
        "VAD / diarisation",
        "seg_model_ids",
        data.seg,
        data.oracle_available ? (
          <label className={cx("flex items-center gap-2.5 min-h-9 px-2 rounded-md cursor-pointer hover:bg-track", value.oracle && "bg-active/60")}>
            <input type="checkbox" checked={value.oracle} onChange={() => set({ oracle: !value.oracle })} className="accent-[#06B6D4]" />
            <span className="text-[13px] flex-1">Reference segments + speakers</span>
            <Chip>oracle</Chip>
          </label>
        ) : undefined,
      )}
      {group("ASR", "asr_model_ids", data.asr)}
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
      <span className="num text-xs text-muted">
        {planCount(value, labels)} run{planCount(value, labels) === 1 ? "" : "s"} · services start automatically
      </span>
    </div>
  );
}
