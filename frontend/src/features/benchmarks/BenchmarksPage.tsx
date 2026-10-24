import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, type Dataset } from "../../lib/api";
import { useConfig, useNormaliser } from "../../lib/config";
import { hours } from "../../lib/format";
import * as I from "../../components/icons";
import { NormaliserButtons } from "../../components/NormaliserButtons";
import { Button, cx, Dialog, Empty, ErrorNote, LinkButton, PageHeader, Select, Spinner } from "../../components/ui";
import { Leaderboard } from "./Leaderboard";
import { PipelinesTab } from "./PipelinesTab";
import { EMPTY_PLAN, PlanPanel, planCount, type PlanValue } from "./PlanPanel";

export function BenchmarksPage() {
  const { tab = "asr" } = useParams();
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const { data: cfg } = useConfig();
  const [normaliser] = useNormaliser();
  const collar = Number(params.get("collar") ?? cfg?.default_collar ?? 0.25);
  const skipOverlap = params.get("skip_overlap") === "1";
  const [showPlan, setShowPlan] = useState(false);
  const tasks = (cfg?.tasks ?? []).filter((t) => t.enabled);
  const setParam = (k: string, v: string | null) => {
    const p = new URLSearchParams(params);
    if (v === null) p.delete(k);
    else p.set(k, v);
    setParams(p, { replace: true });
  };

  return (
    <>
      <PageHeader
        crumbs="Evaluate"
        title="Benchmarks"
        subtitle="Every model you have tried, scored on the datasets you uploaded."
        actions={
          <>
            <LinkButton to="/datasets/upload">
              <I.Upload /> Upload dataset
            </LinkButton>
            <LinkButton to="/models">
              <I.Cpu /> Models
            </LinkButton>
            <Button variant="primary" onClick={() => setShowPlan(true)} aria-haspopup="dialog">
              <I.Play size={12} /> {tab === "pipeline" ? "New pipeline run" : "Run evaluation"}
            </Button>
          </>
        }
      />
      {showPlan && <RunEvaluation tab={tab} onClose={() => setShowPlan(false)} />}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-track">
        <div role="tablist" aria-label="Speech task" className="flex gap-1">
          {tasks.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              onClick={() => nav(`/benchmarks/${t.id}${params.toString() ? `?${params}` : ""}`)}
              className={cx(
                "bg-transparent border-0 border-b-2 px-3 pb-2.5 pt-1 text-[13px] cursor-pointer",
                tab === t.id ? "border-primary text-fg font-medium" : "border-transparent text-muted hover:text-fg",
              )}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-4 pb-2">
          {(tab === "asr" || tab === "pipeline") && <NormaliserButtons />}
          {(tab === "diar" || tab === "pipeline") && (
            <>
              <Select label="Collar" value={String(collar)} onChange={(e) => setParam("collar", e.target.value)}>
                {(cfg?.collars ?? [0, 0.25]).map((c) => (
                  <option key={c} value={c}>
                    ±{c} s
                  </option>
                ))}
              </Select>
              <label className="flex items-center gap-2 text-xs text-muted cursor-pointer" title="Leave overlapped speech out of DER and JER">
                <input type="checkbox" checked={!skipOverlap} onChange={(e) => setParam("skip_overlap", e.target.checked ? null : "1")} className="accent-[#06B6D4]" />
                Score overlapping speech
              </label>
            </>
          )}
        </div>
      </div>
      {tab === "pipeline" ? (
        <PipelinesTab normaliser={normaliser} collar={collar} skipOverlap={skipOverlap} onNewRun={() => setShowPlan(true)} />
      ) : (
        <Leaderboard tab={tab === "diar" ? "diar" : "asr"} normaliser={normaliser} collar={collar} skipOverlap={skipOverlap} />
      )}
    </>
  );
}

/** Run evaluation (ASR, diarisation) or a new pipeline run, in a popup: a dataset, catalog models per stage,
 * the enhancement ablation, and the catalog's plan before anything is queued. */
function RunEvaluation({ tab, onClose }: { tab: string; onClose: () => void }) {
  const qc = useQueryClient();
  const { data: datasets } = useQuery({ queryKey: ["datasets"], queryFn: () => api.get<Dataset[]>("/datasets") });
  const ready = (datasets ?? []).filter((d) => d.status === "ready" && d.label_types.length && (tab !== "pipeline" || d.label_types.includes("asr")));
  const [dsId, setDsId] = useState<number | null>(null);
  const ds = ready.find((d) => d.id === dsId) ?? ready[0];
  const [plan, setPlan] = useState<PlanValue>({ ...EMPTY_PLAN, mode: tab === "pipeline" ? "grid" : "stages" });
  const run = useMutation({
    mutationFn: () => api.post<{ queued: number }>("/benchmarks/plan", { dataset_id: ds!.id, ...plan }),
    onSuccess: () => {
      void qc.invalidateQueries();
      onClose();
    },
  });
  const n = ds ? planCount(plan, ds.label_types) : 0;
  const title = tab === "pipeline" ? "New pipeline run" : tab === "diar" ? "Run diarisation evaluation" : "Run ASR evaluation";
  return (
    <Dialog
      title={title}
      width={720}
      onClose={onClose}
      subtitle={
        tab === "pipeline"
          ? "Pick models for each stage. Every combination runs; pipelines already scored are reused, not run again."
          : "Pick a dataset and catalog models to run on it. The Model Catalog starts each model when its turn comes."
      }
      footer={
        <>
          <span className="num text-xs text-muted flex-1">
            {n} {plan.mode === "grid" ? `pipeline${n === 1 ? "" : "s"}` : `run${n === 1 ? "" : "s"}`}
          </span>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!n || run.isPending || !ds} onClick={() => run.mutate()}>
            {n ? `Run ${n} ${plan.mode === "grid" ? `pipeline${n === 1 ? "" : "s"}` : `evaluation${n === 1 ? "" : "s"}`}` : "Nothing to run"}
          </Button>
        </>
      }
    >
      {!datasets ? (
        <Spinner />
      ) : !ds ? (
        <Empty>
          Upload a labelled dataset first: <Link to="/datasets/upload">Upload dataset</Link>.
        </Empty>
      ) : (
        <>
          <Select label="Dataset" value={ds.id} onChange={(e) => setDsId(Number(e.target.value))}>
            {ready.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name} · {d.usable_files ?? 0} files · {hours(d.usable_hours)}
              </option>
            ))}
          </Select>
          <PlanPanel key={ds.id} datasetId={ds.id} value={plan} onChange={setPlan} />
          {tab !== "pipeline" && (
            <span className="text-xs text-muted">
              Want diarisation and ASR chained together, or enhancement placed before or after diarisation? Use{" "}
              <Link to="/benchmarks/pipeline" onClick={onClose}>
                Pipelines
              </Link>
              .
            </span>
          )}
          <ErrorNote error={run.error} />
        </>
      )}
    </Dialog>
  );
}
