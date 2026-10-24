// "Add enhancement models" (EVALUATE_PLAN.md §6): build enhanced copies of a dataset, one per enhancer, as
// jobs. A copy already built or building is kept; benchmarks and pipelines then offer it as an audio version.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Dataset, type EnhancedCopy, type ModelList } from "../../lib/api";
import { duration, hours } from "../../lib/format";
import { Button, cx, Dialog, EnhChip, ErrorNote, Spinner } from "../../components/ui";

export function AddEnhancement({ datasets, initial, onClose }: { datasets: Dataset[]; initial: number; onClose: () => void }) {
  const qc = useQueryClient();
  const [dsId, setDsId] = useState(initial);
  const [sel, setSel] = useState<string[]>([]);
  const ds = datasets.find((d) => d.id === dsId) ?? datasets[0];
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.get<ModelList>("/models") });
  const copies = useQuery({ queryKey: ["enhanced", ds?.id], queryFn: () => api.get<EnhancedCopy[]>(`/datasets/${ds!.id}/enhanced`), enabled: !!ds });
  const build = useMutation({
    mutationFn: () => api.post(`/datasets/${ds!.id}/enhanced`, { models: sel }),
    onSuccess: () => {
      void qc.invalidateQueries();
      onClose();
    },
  });
  const enhancers = (models.data?.models ?? []).filter((m) => m.stages.includes("enh") && m.in_catalog);
  const have = new Map((copies.data ?? []).filter((c) => c.status === "ready" || c.status === "building").map((c) => [c.model, c]));
  const audioS = (ds?.usable_hours ?? 0) * 3600;
  const picked = enhancers.filter((m) => sel.includes(m.catalog_id) && !have.has(m.catalog_id));
  const est = picked.reduce((a, m) => a + audioS * (m.rtf ?? 0), 0);
  return (
    <Dialog
      title="Add enhancement models"
      width={560}
      onClose={onClose}
      subtitle="Each enhancer builds a cleaned copy of the dataset once. Benchmarks and pipelines then offer it as an audio version; the original audio is never changed."
      footer={
        <>
          <span className="num text-xs text-muted flex-1">
            {picked.length ? `${picked.length} new cop${picked.length === 1 ? "y" : "ies"} of ${hours(ds?.usable_hours)} audio${est ? ` · about ${duration(est)}` : ""}, runs as a job` : "Pick an enhancer without a copy yet"}
          </span>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!picked.length || build.isPending} onClick={() => build.mutate()}>
            {picked.length ? `Build ${picked.length} cop${picked.length === 1 ? "y" : "ies"}` : "Nothing to build"}
          </Button>
        </>
      }
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-xs text-muted mr-1">Dataset</span>
        {datasets.map((d) => (
          <button key={d.id} type="button" aria-pressed={d.id === ds?.id} onClick={() => { setDsId(d.id); setSel([]); }} className={cx("min-h-7 px-2.5 rounded-md text-xs cursor-pointer border", d.id === ds?.id ? "border-primary bg-primary/10 text-fg" : "border-line bg-transparent text-muted")}>
            {d.name}
          </button>
        ))}
      </div>
      {(models.isLoading || copies.isLoading) && <Spinner />}
      {models.data && enhancers.length === 0 && <span className="text-[13px] text-muted">The model catalog has no speech enhancement models. Register one in the catalog first.</span>}
      <div role="group" aria-label="Enhancement models" className="flex flex-col gap-1.5">
        {enhancers.map((m) => {
          const c = have.get(m.catalog_id);
          const locked = !!c || !m.usable;
          const on = !!c || sel.includes(m.catalog_id);
          return (
            <label key={m.catalog_id} title={c ? `This dataset already has a ${m.display_name} copy` : (m.not_usable ?? undefined)} className={cx("flex items-center gap-3 min-h-12 px-2.5 rounded-md border", on && !c ? "border-primary/50 bg-primary/5" : "border-track", locked ? "cursor-default" : "cursor-pointer", !m.usable && "opacity-60")}>
              <input type="checkbox" checked={on} disabled={locked} onChange={() => setSel(sel.includes(m.catalog_id) ? sel.filter((x) => x !== m.catalog_id) : [...sel, m.catalog_id])} className="accent-[#06B6D4]" />
              <span className="flex flex-col flex-1 min-w-0">
                <span className="text-[13px] flex items-center gap-1.5">
                  {m.display_name} {m.generative && <EnhChip>generative</EnhChip>}
                </span>
                <span className="text-[11px] text-subtle truncate">{m.description ?? m.catalog_id}</span>
              </span>
              {!c && m.rtf ? <span className="num text-[11px] text-muted">~{duration(audioS * m.rtf)}</span> : null}
              {c ? <EnhChip dashed={c.status === "building"}>{c.status === "building" ? `building · ${Math.round(c.progress * 100)}%` : "copy ready"}</EnhChip> : !m.usable ? <span className="text-[11px] text-warn">not verified</span> : <span className="text-[11px] text-muted">new copy</span>}
            </label>
          );
        })}
      </div>
      <Link to="/models" className="text-xs" onClick={onClose}>
        Enhancement models in the catalog
      </Link>
      <ErrorNote error={build.error} />
    </Dialog>
  );
}
