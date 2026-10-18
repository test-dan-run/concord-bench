import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, type Dataset, type Run } from "../../lib/api";
import { ago, clock, hours, num } from "../../lib/format";
import { TagInput } from "../../components/TagInput";
import { Button, Chip, Empty, ErrorNote, Fact, JobPill, PageHeader, Panel, Spinner, StatusDot } from "../../components/ui";
import { PlanPanel, planCount, type PlanValue } from "../benchmarks/PlanPanel";

interface FileRow {
  id: number;
  path: string;
  duration: number | null;
  sample_rate: number | null;
  channels: number | null;
  status: string;
  segments: number;
}

const FLAG_TITLES: Record<string, string> = { clipped: "Clipped", undecodable: "Undecodable", too_long: "Over the length cap (excluded from runs)" };

export function DatasetDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["dataset", id],
    queryFn: () => api.get<Dataset>(`/datasets/${id}`),
    refetchInterval: (query) => (query.state.data?.status === "ingesting" ? 1500 : false),
  });
  const files = useQuery({ queryKey: ["dataset-files", id, q.data?.status], queryFn: () => api.get<FileRow[]>(`/datasets/${id}/files`), enabled: q.data?.status === "ready" });
  const runs = useQuery({ queryKey: ["dataset-runs", id], queryFn: () => api.get<Run[]>(`/runs?dataset_id=${id}`), refetchInterval: 4000, enabled: q.data?.status === "ready" });
  const [plan, setPlan] = useState<PlanValue>({ seg_model_ids: [], asr_model_ids: [], oracle: false, mode: "grid" });
  const [editTags, setEditTags] = useState<string[] | null>(null);
  const runPlan = useMutation({ mutationFn: () => api.post("/benchmarks/plan", { dataset_id: Number(id), ...plan }), onSuccess: () => qc.invalidateQueries() });
  const saveTags = useMutation({
    mutationFn: (tags: string[]) => api.patch(`/datasets/${id}`, { tags }),
    onSuccess: () => {
      setEditTags(null);
      void qc.invalidateQueries({ queryKey: ["dataset", id] });
    },
  });
  const del = useMutation({ mutationFn: () => api.del(`/datasets/${id}`), onSuccess: () => nav("/datasets") });

  if (q.isLoading) return <Spinner />;
  if (q.error || !q.data) return <ErrorNote error={q.error} />;
  const d = q.data;
  const rep = d.report;
  const job = d.ingest_job;
  const n = planCount(plan, d.label_types);
  const maxSr = Math.max(1, ...Object.values(rep?.sample_rates ?? {}));

  return (
    <>
      <PageHeader
        crumbs={<><Link to="/datasets">Datasets</Link> / {d.name}</>}
        title={d.name}
        subtitle={d.description ?? undefined}
        actions={
          <Button variant="danger" size="sm" onClick={() => confirm(`Delete dataset “${d.name}” and its runs? Original audio files stay on disk.`) && del.mutate()}>
            Delete
          </Button>
        }
      />
      {d.status === "ingesting" && job && (
        <Panel title="Ingesting">
          <div className="flex items-center gap-3">
            <JobPill status={job.status} progress={job.progress} />
            <span className="text-xs text-muted num truncate">{job.message}</span>
          </div>
          <span className="h-1.5 rounded bg-track overflow-hidden">
            <span className="block h-full bg-primary transition-all" style={{ width: `${job.progress * 100}%` }} />
          </span>
        </Panel>
      )}
      {d.status === "failed" && <ErrorNote error={`Ingest failed: ${job?.error ?? "see the job log"}`} />}
      {d.status === "ready" && rep && (
        <div className="flex flex-col xl:flex-row gap-5 items-start">
          <div className="flex-1 w-full flex flex-col gap-5 min-w-0">
            <div className="grid gap-5 lg:grid-cols-2">
              <Panel title="Report">
                <Fact k="Files" v={`${num(rep.usable_files)} usable of ${num(rep.files)}`} />
                <Fact k="Audio" v={`${hours(rep.usable_hours)} usable of ${hours(rep.hours)}`} />
                <Fact k="Labels" v={d.label_types.length ? d.label_types.map((l) => (l === "asr" ? "ASR" : "Diarisation")).join(" + ") : "audio only"} />
                <Fact k="cpWER-ready" v={rep.has_speaker_text ? "yes (speaker-attributed transcripts)" : "no"} />
                <Fact k="Language" v={d.language ?? "—"} />
                <div className="flex flex-col gap-1.5 pt-1">
                  <span className="text-[11px] text-muted uppercase tracking-wider">Sample rates</span>
                  {Object.entries(rep.sample_rates).map(([sr, c]) => (
                    <div key={sr} className="grid grid-cols-[80px_1fr_40px] gap-2 items-center text-xs">
                      <span className="num">{(Number(sr) / 1000).toFixed(Number(sr) % 1000 ? 1 : 0)} kHz</span>
                      <span className="h-1.5 rounded bg-track"><span className="block h-full rounded bg-secondary" style={{ width: `${(c / maxSr) * 100}%` }} /></span>
                      <span className="num text-right">{c}</span>
                    </div>
                  ))}
                </div>
              </Panel>
              <Panel title="Flagged files">
                {Object.values(rep.flagged).every((l) => !l.length) && <span className="text-[13px] text-muted">Nothing flagged.</span>}
                {Object.entries(rep.flagged).map(([k, list]) =>
                  list.length ? (
                    <div key={k} className="flex flex-col gap-1">
                      <span className="text-xs text-warn">{FLAG_TITLES[k] ?? k} · {list.length}</span>
                      {list.slice(0, 20).map((f) => (
                        <span key={f.file} className="text-xs num text-muted truncate" title={f.error}>
                          {f.file}
                          {f.clipped_pct !== undefined && ` · ${f.clipped_pct}% samples at full scale`}
                          {f.duration !== undefined && ` · ${clock(f.duration, false)}`}
                          {f.error && ` · ${f.error}`}
                        </span>
                      ))}
                    </div>
                  ) : null,
                )}
                {rep.unmatched.length > 0 && <span className="text-xs text-muted">Annotations with no audio: {rep.unmatched.join(", ")}</span>}
                {rep.audio_without_labels_count > 0 && d.label_types.length > 0 && <span className="text-xs text-muted">{rep.audio_without_labels_count} audio files without labels.</span>}
              </Panel>
            </div>
            <Panel title="Files" subtitle={`${files.data?.length ?? 0} files · labels are the reference segments matched at ingest`}>
              <div className="overflow-auto scroll-thin max-h-[420px]">
                <table className="w-full border-collapse text-[13px]">
                  <thead className="sticky top-0 bg-panel">
                    <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
                      <th scope="col" className="font-medium px-3 py-2 border-b border-line">File</th>
                      <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">Duration</th>
                      <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">Rate</th>
                      <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">Ch</th>
                      <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">Segments</th>
                      <th scope="col" className="font-medium px-3 py-2 border-b border-line">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(files.data ?? []).map((f) => (
                      <tr key={f.id} className="border-b border-track">
                        <td className="px-3 py-1.5 num text-xs">{f.path}</td>
                        <td className="px-3 py-1.5 num text-xs text-right">{clock(f.duration, false)}</td>
                        <td className="px-3 py-1.5 num text-xs text-right">{f.sample_rate ? `${f.sample_rate / 1000}k` : "—"}</td>
                        <td className="px-3 py-1.5 num text-xs text-right">{f.channels ?? "—"}</td>
                        <td className="px-3 py-1.5 num text-xs text-right">{f.segments}</td>
                        <td className="px-3 py-1.5"><Chip tone={f.status === "ok" ? "good" : f.status === "clipped" ? "warn" : "bad"}>{f.status.replace("_", " ")}</Chip></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
            <Panel title="Runs on this dataset">
              {(runs.data ?? []).length === 0 && <Empty>No runs yet.</Empty>}
              {(runs.data ?? []).map((r) => (
                <div key={r.id} className="flex flex-wrap items-center gap-3 text-[13px] border-b border-track last:border-0 py-1.5">
                  <StatusDot state={r.status} />
                  <span className="flex-1">{r.label}</span>
                  <Chip>{r.task}</Chip>
                  <span className="text-xs text-muted">{r.status} · {ago(r.finished_at ?? r.created_at)}</span>
                  {r.status === "done" && (
                    <Link className="text-xs" to={r.task === "asr" ? `/errors/asr/${r.id}` : r.task === "seg" ? `/errors/diar/${r.id}` : `/errors/pipeline/${r.id}`}>
                      Errors
                    </Link>
                  )}
                  {r.error && <span className="w-full text-xs text-bad">{r.error}</span>}
                </div>
              ))}
            </Panel>
          </div>
          <aside className="w-full xl:w-[360px] shrink-0 flex flex-col gap-4">
            <Panel title="Tags" actions={editTags === null ? <Button size="sm" onClick={() => setEditTags(d.tags)}>Edit</Button> : undefined}>
              {editTags === null ? (
                <span className="flex flex-wrap gap-1">{d.tags.length ? d.tags.map((t) => <Chip key={t}>#{t}</Chip>) : <span className="text-xs text-muted">No tags.</span>}</span>
              ) : (
                <>
                  <TagInput value={editTags} onChange={setEditTags} />
                  <span className="flex gap-2">
                    <Button size="sm" variant="primary" onClick={() => saveTags.mutate(editTags)}>Save</Button>
                    <Button size="sm" onClick={() => setEditTags(null)}>Cancel</Button>
                  </span>
                </>
              )}
            </Panel>
            {d.label_types.length > 0 && (
              <Panel title="Run evaluation" subtitle="Models ranked by tags shared with this dataset">
                <PlanPanel datasetId={d.id} value={plan} onChange={setPlan} />
                <ErrorNote error={runPlan.error} />
                <Button variant="primary" disabled={!n || runPlan.isPending} onClick={() => runPlan.mutate()}>
                  {runPlan.isSuccess ? "Queued" : `Run ${n} ${plan.mode === "grid" ? `pipeline${n === 1 ? "" : "s"}` : `benchmark${n === 1 ? "" : "s"}`}`}
                </Button>
              </Panel>
            )}
          </aside>
        </div>
      )}
    </>
  );
}
