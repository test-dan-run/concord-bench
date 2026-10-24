// Shared pieces of the error-analysis pages: run lists, pickers, the type switcher (ASR, VAD / diarisation,
// enhancement, pipeline) and the ASR view tabs (utterances, label).

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { api, type Run } from "../../lib/api";
import { cx, Select } from "../../components/ui";

export function useDoneRuns(task: "asr" | "seg" | "pipeline") {
  return useQuery({ queryKey: ["done-runs", task], queryFn: () => api.get<Run[]>(`/errors/runs?task=${task}`), staleTime: 10_000 });
}

/** Latest done run per (model or pipeline, dataset, enhancer, placement). */
export function latestRuns(runs: Run[]): Run[] {
  const seen = new Set<string>();
  const out: Run[] = [];
  for (const r of runs) {
    const k = `${r.model_id ?? r.pipeline_id}-${r.dataset_id}-${r.enh_model_id ?? ""}-${r.enh_placement ?? ""}`;
    if (!seen.has(k)) {
      seen.add(k);
      out.push(r);
    }
  }
  return out;
}

type Kind = "asr" | "diar" | "enh" | "pipeline";

export function Switcher({ current, links }: { current: Kind; links: Partial<Record<Kind, string | null>> }) {
  const items: [Kind, string][] = [
    ["asr", "ASR"],
    ["diar", "VAD / diarisation"],
    ["enh", "Enhancement"],
    ["pipeline", "Pipeline"],
  ];
  return (
    <nav aria-label="Error analysis type" className="flex gap-1 border-b border-track">
      {items.map(([k, label]) => {
        const to = links[k];
        const cls = cx("px-3 pb-2.5 pt-1 text-[13px] border-b-2 no-underline", k === current ? "border-primary !text-fg font-medium" : "border-transparent !text-muted hover:!text-fg");
        return to ? (
          <NavLink key={k} to={to} className={cls} aria-current={k === current ? "page" : undefined}>
            {label}
          </NavLink>
        ) : (
          <span key={k} className={cx(cls, "opacity-50 cursor-not-allowed")} title="No runs of this kind yet">
            {label}
          </span>
        );
      })}
    </nav>
  );
}

/** Model / pipeline + dataset pickers that navigate to the matching latest run. */
export function RunPickers({ task, run, path, extra }: { task: "asr" | "seg" | "pipeline"; run: Run; path: (r: Run) => string; extra?: ReactNode }) {
  const nav = useNavigate();
  const { data } = useDoneRuns(task);
  const runs = latestRuns(data ?? []);
  const key = (r: Run) => `${r.model_id ?? r.pipeline_id}-${r.enh_model_id ?? ""}-${r.enh_placement ?? ""}`;
  const subjects = [...new Map(runs.map((r) => [key(r), r])).values()];
  const datasets = runs.filter((r) => key(r) === key(run));
  return (
    <div className="flex flex-wrap gap-3">
      <Select
        label={task === "pipeline" ? "Pipeline" : "Model"}
        value={key(run)}
        onChange={(e) => {
          const r = runs.find((x) => key(x) === e.target.value && x.dataset_id === run.dataset_id) ?? runs.find((x) => key(x) === e.target.value);
          if (r) nav(path(r));
        }}
      >
        {subjects.map((r) => (
          <option key={key(r)} value={key(r)}>
            {r.label}
          </option>
        ))}
      </Select>
      <Select
        label="Dataset"
        value={run.dataset_id}
        onChange={(e) => {
          const r = datasets.find((x) => x.dataset_id === Number(e.target.value));
          if (r) nav(path(r));
        }}
      >
        {datasets.map((r) => (
          <option key={r.dataset_id} value={r.dataset_id}>
            {r.dataset}
          </option>
        ))}
      </Select>
      {extra}
    </div>
  );
}

/** Where the switcher's other tabs should go: the latest run of that kind on the same dataset, else any. */
export function useSwitchLinks(datasetId: number | undefined, search: string) {
  const asr = useDoneRuns("asr").data ?? [];
  const seg = useDoneRuns("seg").data ?? [];
  const pipe = useDoneRuns("pipeline").data ?? [];
  const pick = (rs: Run[]) => rs.find((r) => r.dataset_id === datasetId) ?? rs[0];
  const a = pick(asr);
  const s = pick(seg);
  const p = pick(pipe);
  const e = pick(pipe.filter((r) => r.enh_model_id !== null));
  return {
    asr: a ? `/errors/asr/${a.id}${search}` : null,
    diar: s ? `/errors/diar/${s.id}${search}` : null,
    enh: e ? `/errors/enh/${e.id}${search}` : null,
    pipeline: p ? `/errors/pipeline/${p.id}${search}` : null,
  };
}

/** The two ASR error views: every utterance, or scores by label. */
export function AsrViews({ runId, current }: { runId: number; current: "utterances" | "label" }) {
  const cls = (on: boolean) => cx("px-3 py-1.5 text-[13px] rounded-md no-underline", on ? "bg-active !text-fg" : "!text-muted hover:!text-fg");
  return (
    <nav aria-label="ASR view" className="flex gap-1">
      <NavLink to={`/errors/asr/${runId}`} end className={cls(current === "utterances")} aria-current={current === "utterances" ? "page" : undefined}>
        Utterances
      </NavLink>
      <NavLink to={`/errors/asr/${runId}/labels`} className={cls(current === "label")} aria-current={current === "label" ? "page" : undefined}>
        Label
      </NavLink>
    </nav>
  );
}

export function Crumbs({ parts }: { parts: (string | { to: string; label: string })[] }) {
  return (
    <>
      {parts.map((p, i) => (
        <span key={i}>
          {i > 0 && " / "}
          {typeof p === "string" ? p : <Link to={p.to}>{p.label}</Link>}
        </span>
      ))}
    </>
  );
}
