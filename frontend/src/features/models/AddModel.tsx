import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, qs, type CheckReport, type Job, type Model, type RegistryEntry } from "../../lib/api";
import { capLabel, pct, timingLabel } from "../../lib/format";
import * as I from "../../components/icons";
import { TagInput } from "../../components/TagInput";
import { Button, Chip, cx, Empty, ErrorNote, Fact, PageHeader, Panel, Select, Spinner, StatusDot } from "../../components/ui";

const GROUPS = [
  { r: 0, title: "Speech models", note: "Trained for speech recognition, VAD or diarisation" },
  { r: 1, title: "Might take audio", note: "Chat models: the check finds out whether they accept an audio part" },
  { r: 2, title: "Not usable as a model", note: "No audio input for transcription or segmentation" },
];

const GLYPH: Record<string, { c: string; t: string }> = {
  pass: { c: "text-good", t: "✓" },
  warn: { c: "text-warn", t: "!" },
  fail: { c: "text-bad", t: "✕" },
  info: { c: "text-muted", t: "i" },
};

export function AddModel() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const name = params.get("name");
  const reg = useQuery({ queryKey: ["registry"], queryFn: () => api.get<RegistryEntry[]>("/registry") });
  const status = useQuery({ queryKey: ["registry-status"], queryFn: () => api.get<Record<string, { state: string }>>("/registry/status"), refetchInterval: 5000 });
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.get<Model[]>("/models") });
  const [q, setQ] = useState("");
  const entry = reg.data?.find((e) => e.name === name) ?? null;

  return (
    <>
      <PageHeader
        crumbs={<><Link to="/benchmarks">Benchmarks</Link> / Add model</>}
        title="Add model"
        subtitle={<>Pick a service from the platform registry. Protocol, endpoint and image come from the platform; capabilities are detected by a check. See the <Link to="/contract">model contract</Link>.</>}
      />
      <div className="flex flex-col xl:flex-row gap-5 items-start">
        <Panel className="w-full xl:w-[340px] shrink-0" title="Platform registry" subtitle="From the aicore endpoint map">
          <label className="relative flex">
            <span className="sr-only">Search the registry</span>
            <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-subtle"><I.Search size={15} /></span>
            <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search services" className="w-full min-h-9 bg-canvas border border-line rounded-md pl-8 pr-2 text-[13px] text-fg" />
          </label>
          {reg.isLoading && <Spinner />}
          <ErrorNote error={reg.error} />
          {GROUPS.map((g) => {
            const items = (reg.data ?? []).filter((e) => e.relevance === g.r && (!q || `${e.name} ${e.aliases.join(" ")} ${e.description}`.toLowerCase().includes(q.toLowerCase())));
            if (!items.length) return null;
            return (
              <div key={g.r} className="flex flex-col gap-1">
                <span className="text-[11px] text-muted uppercase tracking-wider" title={g.note}>{g.title}</span>
                {items.map((e) => (
                  <button
                    key={e.name}
                    type="button"
                    aria-pressed={e.name === name}
                    disabled={g.r === 2}
                    onClick={() => setParams({ name: e.name })}
                    title={e.not_usable ?? e.description}
                    className={cx(
                      "text-left flex items-center gap-2.5 min-h-10 px-2.5 rounded-md border cursor-pointer disabled:cursor-not-allowed disabled:opacity-50",
                      e.name === name ? "bg-active border-focus/60" : "bg-transparent border-transparent hover:bg-track",
                    )}
                  >
                    <StatusDot state={status.data?.[e.name]?.state ?? "unknown"} />
                    <span className="flex flex-col min-w-0 flex-1">
                      <span className="text-[13px] truncate">{e.name}</span>
                      <span className="text-[11px] text-subtle truncate num">{e.contract} / {e.api}</span>
                    </span>
                    {e.model_id && <Chip tone="good">added</Chip>}
                  </button>
                ))}
              </div>
            );
          })}
          <span className="text-[11px] text-subtle flex items-center gap-3">
            <span className="flex items-center gap-1"><StatusDot state="up" />up</span>
            <span className="flex items-center gap-1"><StatusDot state="loading" />loading</span>
            <span className="flex items-center gap-1"><StatusDot state="down" />down (started on check)</span>
          </span>
        </Panel>
        <div className="flex-1 w-full min-w-0">
          {!entry ? (
            <Empty>Pick a registry entry to see what the platform says about it and run the compatibility check.</Empty>
          ) : (
            <EntryForm
              key={entry.name}
              entry={entry}
              state={status.data?.[entry.name]?.state}
              models={models.data ?? []}
              onAdded={(m) => {
                void qc.invalidateQueries();
                nav(`/models?added=${m.id}`);
              }}
            />
          )}
        </div>
      </div>
    </>
  );
}

function EntryForm({ entry, state, models, onAdded }: { entry: RegistryEntry; state?: string; models: Model[]; onAdded: (m: Model) => void }) {
  const [jobId, setJobId] = useState<number | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [tags, setTags] = useState<string[]>(entry.tasks ?? []);
  const [parent, setParent] = useState<string>("");
  const [benchOn, setBenchOn] = useState<number[] | null>(null);
  const start = useMutation({ mutationFn: () => api.post<Job>(`/registry/${entry.name}/check`), onSuccess: (j) => setJobId(j.id) });
  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.get<Job>(`/jobs/${jobId}`),
    enabled: jobId !== null,
    refetchInterval: (query) => (query.state.data && ["queued", "running"].includes(query.state.data.status) ? 1000 : false),
  });
  const report = (job.data?.status === "done" ? (job.data.result as { report?: CheckReport }).report : undefined) ?? null;
  const caps = report?.capabilities ?? [];
  const sugg = useQuery({
    queryKey: ["suggest-datasets", caps.join(","), tags.join(",")],
    queryFn: () => api.get<{ dataset_id: number; name: string; shared_tags: string[]; label_types: string[]; preselect: boolean }[]>(`/suggestions/datasets${qs({ capabilities: caps.join(","), tags: tags.join(",") })}`),
    enabled: caps.length > 0,
  });
  useEffect(() => {
    if (sugg.data && benchOn === null) setBenchOn(sugg.data.filter((d) => d.preselect).map((d) => d.dataset_id));
  }, [sugg.data, benchOn]);
  const add = useMutation({
    mutationFn: async () => {
      const m = await api.post<Model>("/models", {
        registry_name: entry.name,
        check_job_id: jobId,
        display_name: displayName || null,
        tags,
        parent_model_id: parent ? Number(parent) : null,
      });
      for (const ds of benchOn ?? []) {
        if (m.capabilities.includes("asr")) await api.post("/runs", { task: "asr", model_id: m.id, dataset_id: ds });
        if (m.capabilities.includes("vad") || m.capabilities.includes("diar")) {
          const d = sugg.data?.find((x) => x.dataset_id === ds);
          if (d?.label_types.includes("diar")) await api.post("/runs", { task: "seg", model_id: m.id, dataset_id: ds });
        }
      }
      return m;
    },
    onSuccess: onAdded,
  });
  const running = job.data && ["queued", "running"].includes(job.data.status);
  const nBench = benchOn?.length ?? 0;

  return (
    <div className="flex flex-col xl:flex-row gap-5 items-start">
      <div className="flex-1 w-full flex flex-col gap-5 min-w-0">
        <Panel title={entry.name} subtitle={entry.description} actions={<span className="flex items-center gap-1.5 text-xs text-muted"><StatusDot state={state ?? "unknown"} />{state ?? "unknown"}</span>}>
          <div className="grid gap-x-6 sm:grid-cols-2">
            <Fact k="Contract / API" v={`${entry.contract} / ${entry.api}`} />
            <Fact k="Engine" v={entry.engine} />
            <Fact k="Base URL" v={entry.base_url} />
            <Fact k="Aliases" v={entry.aliases.join(", ") || "—"} />
            <Fact k="Image" v={<span className="text-[11px] break-all">{entry.image}</span>} />
            <Fact k="VRAM share" v={entry.gpu_fraction ? pct(entry.gpu_fraction) : "CPU"} />
          </div>
          {entry.signature && (
            <div className="flex flex-col gap-1">
              <span className="text-[11px] text-muted uppercase tracking-wider">OIP signature</span>
              <table className="text-xs border-collapse">
                <tbody>
                  {[...entry.signature.inputs.map((t) => ["in", t] as const), ...entry.signature.outputs.map((t) => ["out", t] as const)].map(([dir, t]) => (
                    <tr key={`${dir}${t.name}`} className="border-t border-track">
                      <td className="py-1 pr-3 text-muted">{dir}</td>
                      <td className="py-1 pr-3 num">{t.name}</td>
                      <td className="py-1 pr-3 num text-muted">{t.datatype}</td>
                      <td className="py-1 num text-muted">[{t.shape.join(", ")}]</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {entry.tasks?.length > 0 && <span className="flex flex-wrap gap-1">{entry.tasks.map((t) => <Chip key={t}>{t}</Chip>)}</span>}
        </Panel>
        <Panel
          title="Compatibility check"
          subtitle="10 s two-speaker synthesised clip with a known transcript and turns. The service is started if it is down."
          actions={
            <Button variant={report ? "outline" : "primary"} disabled={start.isPending || !!running} onClick={() => start.mutate()}>
              {report || job.data?.status === "failed" || job.data?.status === "blocked" ? "Run again" : "Run check"}
            </Button>
          }
        >
          <ErrorNote error={start.error} />
          {running && (
            <div className="flex flex-col gap-2">
              <Spinner label={job.data?.message ?? "Queued"} />
              <pre className="m-0 max-h-48 overflow-auto scroll-thin bg-canvas border border-track rounded-md p-2 text-[11px] text-muted num whitespace-pre-wrap">{(job.data?.log ?? []).slice(-14).join("\n")}</pre>
            </div>
          )}
          {job.data && (job.data.status === "failed" || job.data.status === "blocked") && (
            <div className="flex flex-col gap-2">
              <ErrorNote error={`${job.data.status === "blocked" ? "The platform could not start the service" : "The check failed"}: ${job.data.error}`} />
              <pre className="m-0 max-h-48 overflow-auto scroll-thin bg-canvas border border-track rounded-md p-2 text-[11px] text-muted num whitespace-pre-wrap">{(job.data.log ?? []).slice(-14).join("\n")}</pre>
            </div>
          )}
          {report && (
            <div className="flex flex-col gap-1.5">
              {report.lines.map((l, k) => (
                <div key={k} className="flex items-start gap-2.5 text-[13px]">
                  <span className={cx("num w-4 text-center font-semibold", GLYPH[l.status].c)} aria-label={l.status}>{GLYPH[l.status].t}</span>
                  <span className="w-24 shrink-0 text-muted">{l.name}</span>
                  <span>{l.reason}</span>
                </div>
              ))}
              <div className="flex flex-wrap items-center gap-2 pt-2">
                <span className="text-xs text-muted">Detected:</span>
                {report.capabilities.length ? report.capabilities.map((c) => <Chip key={c} tone="good">{capLabel(c)}</Chip>) : <Chip tone="bad">nothing usable</Chip>}
                {report.timing_level && <Chip>{timingLabel(report.timing_level)}</Chip>}
              </div>
              {report.sample.transcript && <span className="text-xs text-muted">Transcript of the clip: <span className="text-fg">“{report.sample.transcript}”</span></span>}
            </div>
          )}
        </Panel>
        <Panel title="Details" subtitle="Optional. Nothing here is required to add the model.">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="flex flex-col gap-1.5 text-xs text-muted">
              Display name
              <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder={entry.name} className="min-h-10 bg-canvas border border-line rounded-md px-3 text-[13px] text-fg" />
            </label>
            <Select label="Derived from" value={parent} onChange={(e) => setParent(e.target.value)} className="!min-h-10 flex-1">
              <option value="">None</option>
              {models.map((m) => (
                <option key={m.id} value={m.id}>{m.display_name}</option>
              ))}
            </Select>
          </div>
          <TagInput value={tags} onChange={setTags} label="Tags · default from the platform's tasks; used to pick matching datasets" />
        </Panel>
      </div>
      <aside className="w-full xl:w-[300px] shrink-0 flex flex-col gap-4">
        <Panel title="Benchmark on" subtitle="Datasets with labels for this model's tasks, ranked by shared tags">
          {!caps.length && <span className="text-[13px] text-muted">Shown after a successful check.</span>}
          {caps.length > 0 && (sugg.data ?? []).length === 0 && <span className="text-[13px] text-muted">No datasets with matching labels yet.</span>}
          {(sugg.data ?? []).map((d) => (
            <label key={d.dataset_id} className="flex items-center gap-2.5 text-[13px] cursor-pointer">
              <input type="checkbox" className="accent-[#06B6D4]" checked={benchOn?.includes(d.dataset_id) ?? false} onChange={() => setBenchOn((b) => (b ?? []).includes(d.dataset_id) ? (b ?? []).filter((x) => x !== d.dataset_id) : [...(b ?? []), d.dataset_id])} />
              <span className="flex-1">{d.name}</span>
              {d.shared_tags.slice(0, 2).map((t) => <Chip key={t} tone="info">#{t}</Chip>)}
            </label>
          ))}
        </Panel>
        <ErrorNote error={add.error} />
        <Button variant="primary" disabled={!report?.ok || add.isPending || !!entry.model_id} onClick={() => add.mutate()}>
          {entry.model_id ? "Already added" : nBench ? `Add model and run ${nBench} benchmark${nBench > 1 ? "s" : ""}` : "Add model"}
        </Button>
        {report && !report.ok && <span className="text-xs text-bad">Nothing usable was detected, so this model can't be added.</span>}
        {!report && <span className="text-[11px] text-subtle">“Add” unlocks once the check detects at least one capability.</span>}
      </aside>
    </div>
  );
}

export function useRegistryEntry(name: string | null) {
  const reg = useQuery({ queryKey: ["registry"], queryFn: () => api.get<RegistryEntry[]>("/registry") });
  return useMemo(() => reg.data?.find((e) => e.name === name) ?? null, [reg.data, name]);
}
