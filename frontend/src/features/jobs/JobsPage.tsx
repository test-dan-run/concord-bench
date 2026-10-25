import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Job } from "../../lib/api";
import { ago, jobStatusText } from "../../lib/format";
import { Button, cx, Empty, ErrorNote, JobPill, PageHeader, Panel, Spinner } from "../../components/ui";

export function JobsPage() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["jobs", "all"], queryFn: () => api.get<Job[]>("/jobs?limit=100"), refetchInterval: 2000 });
  const [open, setOpen] = useState<number | null>(null);
  const log = useQuery({ queryKey: ["job", open], queryFn: () => api.get<Job>(`/jobs/${open}`), enabled: open !== null, refetchInterval: 1500 });
  const retry = useMutation({ mutationFn: (id: number) => api.post(`/jobs/${id}/retry`), onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }) });
  const stop = useMutation({ mutationFn: (id: number) => api.post(`/jobs/${id}/stop`), onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }) });
  const [confirmStop, setConfirmStop] = useState<number | null>(null);
  const j = log.data;
  const actions = !j ? undefined : ["failed", "blocked", "stopped"].includes(j.status) ? (
    <Button size="sm" onClick={() => retry.mutate(j.id)}>Retry</Button>
  ) : ["queued", "waiting", "running"].includes(j.status) && !j.stopping ? (
    confirmStop === j.id ? (
      <span className="inline-flex items-center gap-2">
        <span className="text-xs text-muted">{j.status === "running" ? "Stop after the current batch?" : "Stop before it runs?"}</span>
        <Button size="sm" variant="danger" disabled={stop.isPending} onClick={() => { stop.mutate(j.id); setConfirmStop(null); }}>Stop</Button>
        <Button size="sm" onClick={() => setConfirmStop(null)}>Keep running</Button>
      </span>
    ) : (
      <Button size="sm" onClick={() => setConfirmStop(j.id)}>Stop</Button>
    )
  ) : undefined;
  return (
    <>
      <PageHeader crumbs="Concord Bench" title="Jobs" subtitle="Ingests, runs, grid stages and rescoring. One worker runs them in order; a job that needs models leases them from the Model Catalog, and waits for compute when they do not fit yet." />
      <div className="flex flex-col xl:flex-row gap-5 items-start">
        <Panel className="flex-1 w-full">
          {q.isLoading && <Spinner />}
          <ErrorNote error={q.error ?? retry.error ?? stop.error} />
          {q.data?.length === 0 && <Empty>No jobs yet.</Empty>}
          {q.data?.map((j) => (
            <button
              key={j.id}
              type="button"
              onClick={() => setOpen(j.id)}
              className={cx("text-left flex flex-wrap items-center gap-3 bg-transparent border rounded-md px-3 py-2 cursor-pointer", open === j.id ? "border-focus" : "border-transparent hover:bg-track")}
            >
              <span className="num text-xs text-subtle w-10">#{j.id}</span>
              <JobPill status={j.status} progress={j.progress} position={j.lease?.position} stopping={j.stopping} />
              <span className="flex-1 min-w-[200px] text-[13px]">{j.label}</span>
              <span className="text-xs text-muted truncate max-w-[360px]" title={j.lease?.waiting_for ?? undefined}>
                {j.status === "done" ? "" : j.status === "waiting" ? (j.lease?.waiting_for ?? jobStatusText(j)) : (j.error ?? j.message ?? "")}
              </span>
              <span className="text-xs text-subtle">{ago(j.finished_at ?? j.started_at ?? j.created_at)}</span>
            </button>
          ))}
        </Panel>
        <Panel className="w-full xl:w-[520px] shrink-0" title={open ? `Job #${open}` : "Log"} actions={actions}>
          {!open && <span className="text-[13px] text-muted">Pick a job to see its log.</span>}
          {log.data && (
            <>
              <span className="text-xs text-muted">
                {log.data.label} · models: {log.data.required_services.join(", ") || "none"}
                {log.data.after.length ? ` · after job${log.data.after.length > 1 ? "s" : ""} ${log.data.after.map((a) => `#${a}`).join(", ")}` : ""}
              </span>
              {log.data.status === "waiting" && (
                <span role="status" className="text-xs text-warn">
                  {jobStatusText(log.data)}
                  {log.data.lease?.waiting_for ? `: ${log.data.lease.waiting_for}` : ""}
                </span>
              )}
              {log.data.stopping && (
                <span role="status" className="text-xs text-muted">
                  Stopping: the job finishes the model calls in flight, keeps what it stored, then stops. Retry picks up from there.
                </span>
              )}
              {log.data.error && <ErrorNote error={log.data.error} />}
              <pre className="m-0 max-h-[60vh] overflow-auto scroll-thin bg-canvas border border-track rounded-md p-2.5 text-[11px] text-muted num whitespace-pre-wrap">{(log.data.log ?? []).join("\n") || "(no output yet)"}</pre>
            </>
          )}
        </Panel>
      </div>
    </>
  );
}
