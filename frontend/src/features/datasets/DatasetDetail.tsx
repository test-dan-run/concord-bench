import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, type Dataset, type EnhancedCopy, type FlaggedReference, type Run } from "../../lib/api";
import { useConfig } from "../../lib/config";
import { ago, bytes, clock, hours, num, rtf } from "../../lib/format";
import { flagsPushBlocker, fromLabel, provenanceOf } from "../../lib/peer";
import * as I from "../../components/icons";
import { LabelsTable } from "../../components/LabelsTable";
import { TagInput } from "../../components/TagInput";
import { Button, Chip, Empty, EnhChip, ErrorNote, Fact, JobPill, PageHeader, Panel, Spinner, StatusDot } from "../../components/ui";
import { AddEnhancement } from "./AddEnhancement";
import { EMPTY_PLAN, PlanPanel, planCount, type PlanValue } from "../benchmarks/PlanPanel";

interface FileRow {
  id: number;
  path: string;
  duration: number | null;
  sample_rate: number | null;
  channels: number | null;
  status: string;
  segments: number;
}

const FLAG_TITLES: Record<string, string> = { clipped: "Clipped", undecodable: "Undecodable" };

export function DatasetDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["dataset", id],
    queryFn: () => api.get<Dataset>(`/datasets/${id}`),
    refetchInterval: (query) =>
      query.state.data?.status === "ingesting" || ((query.state.data?.enhanced ?? []) as EnhancedCopy[]).some((c) => c.status === "building") ? 1500 : false,
  });
  const [addEnh, setAddEnh] = useState(false);
  const [confirmCopy, setConfirmCopy] = useState<number | null>(null);
  const dropCopy = useMutation({
    mutationFn: (copyId: number) => api.del(`/enhanced/${copyId}`),
    onSuccess: () => {
      setConfirmCopy(null);
      void qc.invalidateQueries();
    },
  });
  const dropSlice = useMutation({ mutationFn: (sliceId: number) => api.del(`/slices/${sliceId}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["dataset", id] }) });
  const files = useQuery({ queryKey: ["dataset-files", id, q.data?.status], queryFn: () => api.get<FileRow[]>(`/datasets/${id}/files`), enabled: q.data?.status === "ready" });
  const flags = useQuery({ queryKey: ["dataset-flags", id], queryFn: () => api.get<FlaggedReference[]>(`/datasets/${id}/flags`), enabled: q.data?.status === "ready" });
  const unflag = useMutation({
    mutationFn: (segmentId: number) => api.post(`/segments/${segmentId}/flag`, { flagged: false }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["dataset-flags", id] }),
  });
  const runs = useQuery({ queryKey: ["dataset-runs", id], queryFn: () => api.get<Run[]>(`/runs?dataset_id=${id}`), refetchInterval: 4000, enabled: q.data?.status === "ready" });
  const [plan, setPlan] = useState<PlanValue>(EMPTY_PLAN);
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
  const { data: cfg } = useConfig();
  const [rerun, setRerun] = useState<PlanValue>({ ...EMPTY_PLAN, mode: "stages" });
  const runRerun = useMutation({ mutationFn: () => api.post("/benchmarks/plan", { dataset_id: Number(id), ...rerun }), onSuccess: () => qc.invalidateQueries() });
  const sendFlags = useMutation({ mutationFn: () => api.post<{ matched: number; unmatched: number; sent: number }>(`/datasets/${id}/flags/send`) });

  if (q.isLoading) return <Spinner />;
  if (q.error || !q.data) return <ErrorNote error={q.error} />;
  const d = q.data;
  const rep = d.report;
  const job = d.ingest_job;
  const n = planCount(plan, d.label_types);
  const maxSr = Math.max(1, ...Object.values(rep?.sample_rates ?? {}));
  const prov = provenanceOf(d);
  const prevPlan = d.previous_plan;
  const nRerun = planCount(rerun, d.label_types);
  const flagBlocker = flagsPushBlocker(cfg, d);
  const nFlags = (flags.data ?? []).length;
  const copies = (d.enhanced ?? []) as EnhancedCopy[];
  const live = copies.filter((c) => c.status === "ready" || c.status === "building");
  const usableLabels = (rep?.labels ?? []).filter((l) => l.kind !== "unused");

  return (
    <>
      {addEnh && <AddEnhancement datasets={[d]} initial={d.id} onClose={() => setAddEnh(false)} />}
      <PageHeader
        crumbs={<><Link to="/datasets">Datasets</Link> / {d.name}</>}
        title={d.name}
        subtitle={d.description ?? undefined}
        actions={
          <>
            <Button variant="danger" size="sm" onClick={() => confirm(`Delete dataset “${d.name}” and its runs? Original audio files stay on disk.`) && del.mutate()}>
              Delete
            </Button>
          </>
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
                <Fact k="Labels" v={d.label_types.length ? d.label_types.map((l) => (l === "asr" ? "ASR" : "Diarisation")).join(" + ") : "—"} />
                <Fact k="cpWER-ready" v={rep.has_speaker_text ? "yes (speaker-attributed transcripts)" : "no"} />
                <Fact k="Enhanced copies" v={<a href="#enh-h">{live.length ? `${live.filter((c) => c.status === "ready").length} ready${live.some((c) => c.status === "building") ? ` · ${live.filter((c) => c.status === "building").length} building` : ""}` : "none"}</a>} />
                <Fact k="Labels for slicing" v={<a href="#lbl-h">{usableLabels.length ? usableLabels.map((l) => l.key).join(", ") : "none"}</a>} />
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
            <Panel id="lbl-h" title="Labels for slicing" subtitle="Attributes that came with the references. Error analysis breaks scores down by each one.">
              <LabelsTable labels={rep.labels ?? []} />
              <span className="text-xs text-subtle">Labels are read from the references when the dataset is added and can't be edited here. A file or utterance without a value is left out of that label's slices.</span>
              {(d.slices ?? []).length > 0 && (
                <div className="flex flex-col gap-1.5 pt-1">
                  <span className="text-[11px] text-muted uppercase tracking-wider">Saved slices</span>
                  {(d.slices ?? []).map((s) => (
                    <div key={s.id} className="flex flex-wrap items-center gap-2 text-[13px]">
                      <span className="flex-1">{s.name}</span>
                      <span className="num text-xs text-muted">{s.filters.map((f) => ("value" in f ? `${f.key} = ${String(f.value)}` : `${f.key} ${f.min ?? ""}–${f.max ?? ""}`)).join(", ")}</span>
                      <Button size="sm" onClick={() => dropSlice.mutate(s.id)} aria-label={`Delete slice ${s.name}`}>
                        Delete
                      </Button>
                    </div>
                  ))}
                  <span className="text-xs text-subtle">Each saved slice is a column next to this dataset on Benchmarks.</span>
                </div>
              )}
            </Panel>
            <Panel
              id="enh-h"
              title="Enhanced copies"
              subtitle="One cleaned copy of the audio per enhancer, with the same file names and length as the originals, which are never changed. Benchmarks and pipelines offer each ready copy as an audio version."
              actions={
                <Button size="sm" onClick={() => setAddEnh(true)} aria-haspopup="dialog">
                  <span className="text-enh">
                    <I.Pulse size={14} />
                  </span>
                  Add enhancement
                </Button>
              }
            >
              {copies.length === 0 ? (
                <span className="text-[13px] text-muted">No enhanced copies. Add an enhancer to compare models on cleaned audio.</span>
              ) : (
                <div className="overflow-x-auto scroll-thin">
                  <table className="w-full border-collapse text-[13px]">
                    <thead>
                      <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
                        <th scope="col" className="font-medium px-3 py-2 border-b border-line">Enhancer</th>
                        <th scope="col" className="font-medium px-3 py-2 border-b border-line">Status</th>
                        <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">Size</th>
                        <th scope="col" className="font-medium px-3 py-2 border-b border-line">Length check</th>
                        <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">RTF</th>
                        <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">Used by</th>
                        <th scope="col" className="px-3 py-2 border-b border-line">
                          <span className="sr-only">Actions</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {copies.map((c) => {
                        const building = c.status === "building";
                        const asking = confirmCopy === c.id;
                        return (
                          <tr key={c.id} className="border-b border-track align-middle">
                            <td className="px-3 py-2">
                              <span className="flex flex-col">
                                <span className="flex items-center gap-1.5">
                                  {c.display_name} {c.generative && <EnhChip>generative</EnhChip>}
                                </span>
                                <span className="num text-[11px] text-subtle">{building ? `started ${ago(c.created_at)}` : c.finished_at ? `built ${ago(c.finished_at)}` : ""}</span>
                              </span>
                            </td>
                            <td className="px-3 py-2">
                              {c.status === "failed" ? <Chip tone="bad" title={c.error ?? undefined}>failed</Chip> : c.status === "cancelled" ? <Chip>cancelled</Chip> : <EnhChip dashed={building}>{building ? `building · ${Math.round(c.progress * 100)}%` : "ready"}</EnhChip>}
                            </td>
                            <td className="px-3 py-2 text-right num text-xs">{c.size_bytes ? bytes(c.size_bytes) : "—"}</td>
                            <td className="px-3 py-2">
                              {c.length_check === "ok" ? (
                                <Chip tone="good" title="Every file matches its original to the sample">same length</Chip>
                              ) : c.length_check === "trimmed" ? (
                                <Chip tone="warn" title="Some files were off by up to 10 ms and were padded or trimmed to the original length">fixed, up to {c.max_shift_ms?.toFixed(1)} ms</Chip>
                              ) : c.length_check === "failed" ? (
                                <Chip tone="bad" title={c.error ?? undefined}>failed</Chip>
                              ) : (
                                <Chip title="Checked once every file is enhanced">after the build</Chip>
                              )}
                            </td>
                            <td className="px-3 py-2 text-right num text-xs">{rtf(c.rtf)}</td>
                            <td className="px-3 py-2 text-right num text-xs">{c.runs ? `${c.runs} run${c.runs === 1 ? "" : "s"}` : "—"}</td>
                            <td className="px-3 py-2 text-right whitespace-nowrap">
                              {c.status === "failed" || c.status === "cancelled" ? null : asking ? (
                                <span className="inline-flex flex-wrap items-center justify-end gap-2">
                                  <span role="status" className="text-xs">{building ? `Stop building the ${c.display_name} copy?` : `Delete the ${c.size_bytes ? bytes(c.size_bytes) : ""} ${c.display_name} copy?`}</span>
                                  <Button size="sm" onClick={() => setConfirmCopy(null)}>Keep</Button>
                                  <Button size="sm" variant="danger" disabled={dropCopy.isPending} onClick={() => dropCopy.mutate(c.id)}>{building ? "Cancel build" : "Delete"}</Button>
                                </span>
                              ) : (
                                <Button size="sm" onClick={() => setConfirmCopy(c.id)} aria-label={`${building ? "Cancel the" : "Delete the"} ${c.display_name} copy`}>
                                  {building ? "Cancel build" : "Delete"}
                                </Button>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
              {copies.some((c) => c.size_bytes) && (
                <span className="flex flex-wrap items-baseline justify-between gap-2 text-xs">
                  <span className="num text-muted">{bytes(copies.reduce((a, c) => a + (c.size_bytes ?? 0), 0))} on disk</span>
                  <span className="text-subtle">Deleting a copy frees its disk space. Scores from runs that used it stay; a new run that needs it builds the copy again.</span>
                </span>
              )}
              <ErrorNote error={dropCopy.error} />
            </Panel>
            <Panel
              title="Flagged references"
              subtitle="Marked wrong with “Flag reference as wrong” on the ASR error pages. Scores still use them until the references are corrected."
            >
              {prov?.source && nFlags > 0 && (
                <div className="flex flex-col gap-1.5 pb-1">
                  <span className="flex flex-wrap items-center gap-2">
                    {fromLabel(d) && (
                      <Button size="sm" variant="primary" disabled={!!flagBlocker || sendFlags.isPending} title={flagBlocker ?? undefined} onClick={() => sendFlags.mutate()}>
                        <I.Flag size={13} /> Send flags to Label
                      </Button>
                    )}
                    <a href={`/api/datasets/${d.id}/flags.json`} download className="inline-flex items-center gap-1.5 text-xs">
                      <I.Download size={12} /> Download flags file
                    </a>
                  </span>
                  {fromLabel(d) && flagBlocker && <span className="text-xs text-muted">{flagBlocker}</span>}
                  {sendFlags.data && (
                    <span role="status" className="text-xs text-good">
                      Sent {sendFlags.data.sent} flag{sendFlags.data.sent === 1 ? "" : "s"}: {sendFlags.data.matched} matched in Concord Label, {sendFlags.data.unmatched} unmatched.
                    </span>
                  )}
                  <ErrorNote error={sendFlags.error} />
                </div>
              )}
              {nFlags === 0 && <span className="text-[13px] text-muted">No references flagged.</span>}
              {(flags.data ?? []).map((f) => (
                <div key={f.id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[13px] border-b border-track last:border-0 py-1.5">
                  <span className="num text-xs text-muted">{f.path} · {clock(f.start)}–{clock(f.end)}</span>
                  {f.speaker && <Chip>{f.speaker}</Chip>}
                  <span className="flex-1 min-w-[200px]">{f.text ?? <span className="text-muted">(no text)</span>}</span>
                  {f.note && <span className="text-xs text-warn">{f.note}</span>}
                  <Button size="sm" disabled={unflag.isPending} onClick={() => unflag.mutate(f.id)}>Unflag</Button>
                </div>
              ))}
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
            {prov && (
              <Panel title="Where the references came from" subtitle="Imported from a reference package. References are never changed; a newer package is a new version.">
                {prov.source ? (
                  <>
                    <Fact k="Source dataset" v={prov.source.dataset_name} />
                    <Fact k="From" v={prov.source.app === "concord-label" ? "Concord Label" : prov.source.app} />
                    <Fact k="Install" v={<span title={prov.source.install_id}>{prov.source.install_id.slice(0, 12)}</span>} />
                    <Fact k="Version" v={`v${d.export_version ?? prov.export_version ?? "?"}`} />
                  </>
                ) : (
                  <Fact k="Source" v="hand-made or another tool" />
                )}
                {prov.scope && <Fact k="Scope" v={prov.scope === "complete" ? "complete (fully reviewed files)" : "any confirmed segment"} />}
                {prov.normaliser && <Fact k="Suggested normaliser" v={prov.normaliser} />}
                {prov.models && prov.models.length > 0 && <Fact k="Pseudo-label models" v={prov.models.join(", ")} />}
                {prov.review && <Fact k="Review" v={Object.entries(prov.review).map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`).join(" · ")} />}
                {(d.versions ?? []).length > 1 && (
                  <div className="flex flex-col gap-1 pt-1">
                    <span className="text-[11px] text-muted uppercase tracking-wider">Versions</span>
                    {(d.versions ?? []).map((v) =>
                      v.id === d.id ? (
                        <span key={v.id} className="text-xs">v{v.export_version} · {v.name} (this one)</span>
                      ) : (
                        <Link key={v.id} to={`/datasets/${v.id}`} className="text-xs">v{v.export_version} · {v.name}</Link>
                      ),
                    )}
                  </div>
                )}
              </Panel>
            )}
            {prevPlan && prevPlan.models > 0 && d.label_types.length > 0 && (
              <Panel title={`Run the models benchmarked on ${prevPlan.previous.name}`} subtitle="Pre-ticked with the models that have finished runs on the previous version.">
                <PlanPanel datasetId={d.id} value={rerun} onChange={setRerun} preset={prevPlan.plan} />
                <ErrorNote error={runRerun.error} />
                <Button variant="primary" disabled={!nRerun || runRerun.isPending} onClick={() => runRerun.mutate()}>
                  {runRerun.isSuccess ? "Queued" : `Run ${nRerun} on this version`}
                </Button>
              </Panel>
            )}
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
