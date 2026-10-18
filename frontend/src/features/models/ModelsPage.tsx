import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type Job, type Model } from "../../lib/api";
import { ago, capLabel, timingLabel } from "../../lib/format";
import * as I from "../../components/icons";
import { Button, Chip, Empty, ErrorNote, LinkButton, PageHeader, Panel, Spinner } from "../../components/ui";

export function ModelsPage() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["models"], queryFn: () => api.get<Model[]>("/models") });
  const recheck = useMutation({ mutationFn: (id: number) => api.post<Job>(`/models/${id}/recheck`), onSuccess: () => qc.invalidateQueries({ queryKey: ["models"] }) });
  const del = useMutation({ mutationFn: (id: number) => api.del(`/models/${id}`), onSuccess: () => qc.invalidateQueries() });
  const byId = Object.fromEntries((q.data ?? []).map((m) => [m.id, m]));
  return (
    <>
      <PageHeader
        crumbs="Bring your own"
        title="Models"
        subtitle="Registered from the platform registry. Capabilities and timing come from the compatibility check."
        actions={<LinkButton to="/models/add" variant="primary"><I.Plus /> Add model</LinkButton>}
      />
      <Panel>
        {q.isLoading && <Spinner />}
        <ErrorNote error={q.error ?? recheck.error ?? del.error} />
        {q.data?.length === 0 && <Empty>No models yet. <Link to="/models/add">Add one from the registry</Link>.</Empty>}
        {!!q.data?.length && (
          <div className="overflow-x-auto scroll-thin">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Model</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Detected</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Timing</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Image</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Tags</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">Runs</th>
                  <th scope="col" className="px-3 py-2 border-b border-line"><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {q.data?.map((m) => (
                  <tr key={m.id} className="border-b border-track align-top">
                    <td className="px-3 py-2.5">
                      <span className="flex flex-col">
                        <span>{m.display_name}</span>
                        <span className="text-[11px] text-subtle num">{m.registry_name} · {m.registry.contract} / {m.registry.api}</span>
                        {m.parent_model_id && <span className="text-[11px] text-subtle">derived from {byId[m.parent_model_id]?.display_name ?? `#${m.parent_model_id}`}</span>}
                        <span className="text-[11px] text-subtle">added {ago(m.created_at)}</span>
                      </span>
                    </td>
                    <td className="px-3 py-2.5"><span className="flex flex-wrap gap-1">{m.capabilities.map((c) => <Chip key={c} tone="good">{capLabel(c)}</Chip>)}</span></td>
                    <td className="px-3 py-2.5 text-xs text-muted">{timingLabel(m.timing_level)}</td>
                    <td className="px-3 py-2.5 text-[11px] text-muted num break-all max-w-[220px]">{m.registry.image}</td>
                    <td className="px-3 py-2.5"><span className="flex flex-wrap gap-1">{m.tags.map((t) => <Chip key={t}>#{t}</Chip>)}</span></td>
                    <td className="px-3 py-2.5 text-right num">{m.runs}</td>
                    <td className="px-3 py-2.5">
                      <span className="flex gap-1.5 justify-end">
                        <Button size="sm" onClick={() => recheck.mutate(m.id)} title="Run the compatibility check again and update capabilities">Re-check</Button>
                        <Button size="sm" variant="danger" onClick={() => confirm(`Remove ${m.display_name} and its runs?`) && del.mutate(m.id)}>Remove</Button>
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}
