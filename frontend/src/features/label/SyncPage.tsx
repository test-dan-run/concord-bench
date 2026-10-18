import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, qs, type Conflict, type Dataset, type ImportPlan, type ImportStaged, type LabelFile, type SyncPackage } from "../../lib/api";
import { ago, bytes, clock, num } from "../../lib/format";
import { uploadFiles } from "../../lib/upload";
import * as I from "../../components/icons";
import { Button, Chip, Empty, ErrorNote, Fact, PageHeader, Panel, Select, Spinner, Toggle, cx } from "../../components/ui";

const OUTCOME_LABELS: Record<string, string> = {
  add: "Added",
  unchanged: "Unchanged",
  keep_local: "Kept here",
  take_incoming: "Taken from package",
  conflict: "Conflict",
  skipped: "Skipped",
  matched: "Matched by fingerprint",
  missing_audio: "Audio missing",
};
const OUTCOME_TONE: Record<string, "good" | "warn" | "bad" | "info" | "default"> = {
  add: "good",
  take_incoming: "info",
  conflict: "warn",
  skipped: "bad",
  missing_audio: "bad",
  keep_local: "default",
  unchanged: "default",
  matched: "default",
};

export function SyncPage() {
  return (
    <>
      <PageHeader
        crumbs={<Link to="/label" className="!text-muted hover:!text-fg">Label</Link>}
        title="Import / export progress"
        subtitle="Move labelling progress between installs as plain-file packages. Files are matched by audio fingerprint (SHA-256)."
      />
      <div className="grid gap-5 xl:grid-cols-2">
        <ExportPanel />
        <ImportPanel />
      </div>
      <History />
    </>
  );
}

// ---------------------------------------------------------------- export

function ExportPanel() {
  const qc = useQueryClient();
  const datasets = useQuery({ queryKey: ["datasets"], queryFn: () => api.get<Dataset[]>("/datasets") });
  const ready = (datasets.data ?? []).filter((d) => d.status === "ready");
  const [dsId, setDsId] = useState<number | null>(null);
  useEffect(() => {
    if (dsId === null && ready[0]) setDsId(ready[0].id);
  }, [ready, dsId]);
  const [scope, setScope] = useState("changed");
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [pseudo, setPseudo] = useState(true);
  const [outputs, setOutputs] = useState(false);
  const [audio, setAudio] = useState(false);
  const pv = useQuery({
    queryKey: ["export-preview", dsId],
    queryFn: () => api.get<{ files: number; changed_files: number | null; last_export: SyncPackage | null }>(`/sync/exports/preview${qs({ dataset_id: dsId })}`),
    enabled: dsId !== null,
  });
  const files = useQuery({ queryKey: ["label-files", dsId], queryFn: () => api.get<LabelFile[]>(`/label/datasets/${dsId}/files`), enabled: dsId !== null && scope === "files" });
  const go = useMutation({
    mutationFn: () => api.post<SyncPackage>("/sync/exports", { dataset_id: dsId, scope, file_ids: [...picked], include_pseudo: pseudo, include_outputs: outputs, include_audio: audio }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["sync-history"] });
      void qc.invalidateQueries({ queryKey: ["export-preview", dsId] });
    },
  });
  const changedNote = pv.data ? (pv.data.last_export ? `${num(pv.data.changed_files)} files changed since the last export (${ago(pv.data.last_export.created_at)})` : "No earlier export: everything is included") : "";
  return (
    <Panel title="Export">
      <Select label="Dataset" value={dsId ?? ""} onChange={(e) => { setDsId(Number(e.target.value)); setPicked(new Set()); go.reset(); }}>
        {ready.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
      </Select>
      <div className="flex flex-col gap-1.5">
        <Toggle
          label="Files to export"
          value={scope}
          onChange={setScope}
          options={[
            { value: "changed", label: "Changed since last export" },
            { value: "all", label: "All files" },
            { value: "files", label: "Pick files" },
          ]}
        />
        {scope === "changed" && <span className="text-xs text-muted">{changedNote}</span>}
        {scope === "files" && (
          <div className="max-h-48 overflow-auto scroll-thin border border-track rounded-md p-2 flex flex-col gap-1">
            {files.isLoading && <Spinner />}
            {(files.data ?? []).map((f) => (
              <label key={f.id} className="flex items-center gap-2 text-[13px]">
                <input
                  type="checkbox"
                  checked={picked.has(f.id)}
                  onChange={() => setPicked((s) => {
                    const n = new Set(s);
                    if (n.has(f.id)) n.delete(f.id);
                    else n.add(f.id);
                    return n;
                  })}
                />
                <span className="truncate flex-1">{f.path}</span>
                <span className="num text-[11px] text-muted">{f.counts.confirmed} confirmed</span>
              </label>
            ))}
          </div>
        )}
      </div>
      <fieldset className="border-0 p-0 m-0 flex flex-col gap-1.5 text-[13px]">
        <legend className="text-[11px] text-muted uppercase tracking-wider mb-1.5">Include</legend>
        <label className="flex items-center gap-2"><input type="checkbox" checked={pseudo} onChange={(e) => setPseudo(e.target.checked)} /> Pseudo-labels (unreviewed segments)</label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={outputs} onChange={(e) => setOutputs(e.target.checked)} /> Raw model outputs (lets the other install re-fuse without inference)</label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={audio} onChange={(e) => setAudio(e.target.checked)} /> Audio (needed where the other install lacks these files)</label>
      </fieldset>
      <ErrorNote error={go.error} />
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="primary" disabled={dsId === null || go.isPending || (scope === "files" && !picked.size)} onClick={() => go.mutate()}>
          <I.Download size={14} /> {go.isPending ? "Building…" : "Build package"}
        </Button>
        {go.data && (
          <a href={`/api/sync/exports/${go.data.id}/download`} className="text-[13px]">
            Download {go.data.file} <span className="num text-muted">({bytes(Number(go.data.counts.bytes ?? 0))}, {num(go.data.counts.files)} files, {num(go.data.counts.segments)} segments)</span>
          </a>
        )}
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------- import

function ImportPanel() {
  const qc = useQueryClient();
  const [staged, setStaged] = useState<ImportStaged | null>(null);
  const [target, setTarget] = useState<string>("");
  const [newName, setNewName] = useState("");
  const [resolutions, setResolutions] = useState<Record<string, "incoming" | "local">>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const datasets = useQuery({ queryKey: ["datasets"], queryFn: () => api.get<Dataset[]>("/datasets") });

  const onFile = async (f: File | undefined) => {
    if (!f) return;
    setErr(null);
    setStaged(null);
    setResolutions({});
    try {
      setBusy("Uploading…");
      const uid = await uploadFiles([{ file: f, path: f.name }], () => undefined);
      setBusy("Reading package…");
      const s = await api.post<ImportStaged>("/sync/imports", { upload_id: uid });
      setStaged(s);
      setTarget(s.suggestions?.[0] ? String(s.suggestions[0].dataset_id) : "new");
      setNewName(s.manifest.dataset?.name ?? "");
      void qc.invalidateQueries({ queryKey: ["sync-history"] });
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(null);
    }
  };
  const plan = useQuery({
    queryKey: ["import-plan", staged?.id, target],
    queryFn: () => api.get<ImportPlan>(`/sync/imports/${staged!.id}/plan${qs({ target_dataset_id: target === "new" ? undefined : target })}`),
    enabled: !!staged && staged.status === "staged" && target !== "",
  });
  const apply = useMutation({
    mutationFn: () => api.post<ImportStaged>(`/sync/imports/${staged!.id}/apply`, { target_dataset_id: target === "new" ? null : Number(target), new_name: target === "new" ? newName : null, resolutions }),
    onSuccess: (p) => {
      setStaged({ ...staged!, ...p });
      void qc.invalidateQueries({ queryKey: ["sync-history"] });
      void qc.invalidateQueries({ queryKey: ["label-console"] });
      void qc.invalidateQueries({ queryKey: ["datasets"] });
    },
  });
  const p = plan.data;
  const counts = staged?.counts as { files?: number; segments?: number; by_status?: Record<string, number>; audio_files?: number; model_outputs?: number } | undefined;
  return (
    <Panel title="Import">
      <label className="flex flex-col gap-1.5 text-xs text-muted">
        Label package (.zip)
        <input type="file" accept=".zip,application/zip" onChange={(e) => void onFile(e.target.files?.[0])} className="text-[13px] text-fg" />
      </label>
      {busy && <Spinner label={busy} />}
      <ErrorNote error={err} />
      {staged && (
        <>
          <div className="grid sm:grid-cols-2 gap-x-6">
            <Fact k="Dataset" v={staged.manifest.dataset?.name ?? "—"} />
            <Fact k="Exported" v={ago(staged.manifest.created_at as string)} />
            <Fact k="From install" v={staged.source_install ?? "—"} />
            <Fact k="Files" v={num(counts?.files)} />
            <Fact k="Segments" v={num(counts?.segments)} />
            <Fact k="With audio / model outputs" v={`${num(counts?.audio_files)} / ${num(counts?.model_outputs)}`} />
          </div>
          {staged.status === "staged" && (
            <>
              <div className="flex flex-wrap items-end gap-3">
                <Select label="Merge into" value={target} onChange={(e) => { setTarget(e.target.value); setResolutions({}); }}>
                  {(staged.suggestions ?? []).map((s) => (
                    <option key={s.dataset_id} value={s.dataset_id}>
                      {s.name} · {s.matched_files}/{s.package_files} files match{s.same_name ? " · same name" : ""}
                    </option>
                  ))}
                  {(datasets.data ?? []).filter((d) => !(staged.suggestions ?? []).some((s) => s.dataset_id === d.id)).map((d) => (
                    <option key={d.id} value={d.id}>{d.name} · no files match</option>
                  ))}
                  <option value="new">New dataset</option>
                </Select>
                {target === "new" && (
                  <label className="flex items-center gap-2 text-xs text-muted">
                    Name
                    <input value={newName} onChange={(e) => setNewName(e.target.value)} className="bg-canvas border border-line rounded px-2 min-h-[34px] text-fg text-[13px]" />
                  </label>
                )}
              </div>
              {plan.isLoading && <Spinner label="Working out the merge" />}
              <ErrorNote error={plan.error} />
              {p && <PlanView plan={p} resolutions={resolutions} onResolve={(k, c) => setResolutions((r) => ({ ...r, [k]: c }))} />}
              <ErrorNote error={apply.error} />
              <div className="flex flex-wrap items-center gap-3">
                <Button variant="primary" disabled={!p || apply.isPending || (target === "new" && !newName.trim())} onClick={() => apply.mutate()}>
                  <I.Check size={14} /> Apply merge
                </Button>
                <span className="text-xs text-muted">A snapshot of the target's labels is taken first, so the merge can be undone.</span>
              </div>
            </>
          )}
          {staged.status === "applied" && (
            <div className="flex flex-col gap-2 text-[13px]">
              <span className="text-good">Merged into dataset {staged.dataset_id}. {staged.conflicts_open > 0 && <span className="text-warn">{staged.conflicts_open} conflicts still open: resolve them below in History.</span>}</span>
              <span className="flex gap-3">
                <Link to={`/label/annotate/${staged.dataset_id}`}>Open in annotator</Link>
                <Link to="/label">Review console</Link>
              </span>
            </div>
          )}
        </>
      )}
      {!staged && !busy && <span className="text-xs text-muted">The package is read and compared first; nothing changes until you apply.</span>}
    </Panel>
  );
}

function PlanView({ plan, resolutions, onResolve }: { plan: ImportPlan; resolutions: Record<string, "incoming" | "local">; onResolve: (key: string, c: "incoming" | "local") => void }) {
  const [show, setShow] = useState<string | null>(null);
  const rows = show ? plan.rows.filter((r) => r.outcome === show) : plan.rows;
  return (
    <div className="flex flex-col gap-3">
      <span className="flex flex-wrap gap-1.5 text-xs">
        {Object.entries(plan.file_counts).map(([k, n]) => <Chip key={k} tone={OUTCOME_TONE[k]}>{num(n)} files · {OUTCOME_LABELS[k] ?? k}</Chip>)}
      </span>
      <table className="w-full border-collapse text-[13px]">
        <caption className="sr-only">Segment outcomes and the rule behind each</caption>
        <thead>
          <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
            <th scope="col" className="font-medium px-2 py-1.5 border-b border-line">Outcome</th>
            <th scope="col" className="font-medium px-2 py-1.5 border-b border-line text-right">Segments</th>
            <th scope="col" className="font-medium px-2 py-1.5 border-b border-line">Rule</th>
          </tr>
        </thead>
        <tbody>
          {plan.rules.filter((r) => plan.counts[r.outcome]).map((r) => (
            <tr key={r.outcome} className={cx("border-b border-track cursor-pointer hover:bg-track/60", show === r.outcome && "bg-active")} onClick={() => setShow(show === r.outcome ? null : r.outcome)}>
              <td className="px-2 py-1.5"><Chip tone={OUTCOME_TONE[r.outcome]}>{OUTCOME_LABELS[r.outcome]}</Chip></td>
              <td className="px-2 py-1.5 text-right num">{num(plan.counts[r.outcome])}</td>
              <td className="px-2 py-1.5 text-xs text-muted">{r.rule}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {show && show !== "conflict" && (
        <div className="max-h-56 overflow-auto scroll-thin border border-track rounded-md">
          {rows.slice(0, 200).map((r, k) => (
            <div key={k} className="grid grid-cols-[96px_minmax(0,1fr)] gap-2 px-2 py-1.5 border-b border-track last:border-0 text-xs">
              <span className="num text-muted">{clock(r.incoming.start)}</span>
              <span className="truncate">{r.incoming.speaker && <span className="text-muted">{r.incoming.speaker}: </span>}{r.incoming.text ?? <span className="text-subtle">(no text)</span>}</span>
            </div>
          ))}
        </div>
      )}
      {plan.conflicts.length > 0 && (
        <ConflictList conflicts={plan.conflicts} choice={(c) => resolutions[c.key] ?? null} onChoose={(c, v) => onResolve(c.key, v)} note="Unresolved conflicts keep this install's version and stay open after the merge." />
      )}
    </div>
  );
}

function ConflictList({ conflicts, choice, onChoose, note }: { conflicts: Conflict[]; choice: (c: Conflict) => "incoming" | "local" | null; onChoose: (c: Conflict, v: "incoming" | "local") => void; note: string }) {
  return (
    <div className="flex flex-col gap-2">
      <span className="text-[11px] text-muted uppercase tracking-wider">Conflicts · {conflicts.length}</span>
      <span className="text-xs text-muted">{note}</span>
      {conflicts.map((c) => {
        const ch = choice(c);
        return (
          <div key={c.key} className="grid gap-2 sm:grid-cols-2 border border-warn/40 rounded-md p-2">
            {(["local", "incoming"] as const).map((side) => {
              const s = side === "local" ? c.local : c.incoming;
              return (
                <button
                  key={side}
                  type="button"
                  aria-pressed={ch === side}
                  onClick={() => onChoose(c, side)}
                  className={cx("text-left flex flex-col gap-1 rounded-md border p-2 cursor-pointer bg-transparent text-fg", ch === side ? "border-primary bg-active" : "border-line hover:bg-track")}
                >
                  <span className="text-[11px] text-muted">{side === "local" ? "This install" : "Package"} · <span className="num">{clock(s.start)}–{clock(s.end)}</span> · {s.speaker ?? "—"} · {s.status}</span>
                  <span className="text-[13px]">{s.text ?? <span className="text-subtle">(no text)</span>}</span>
                  <span className="text-[11px] text-subtle">{s.updated_at ? ago(s.updated_at) : ""}</span>
                </button>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------- history

function History() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["sync-history"], queryFn: () => api.get<SyncPackage[]>("/sync/packages") });
  const [open, setOpen] = useState<number | null>(null);
  const undo = useMutation({
    mutationFn: (id: number) => api.post<SyncPackage>(`/sync/imports/${id}/undo`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["sync-history"] });
      void qc.invalidateQueries({ queryKey: ["label-console"] });
    },
  });
  return (
    <Panel title="History">
      {q.isLoading && <Spinner />}
      <ErrorNote error={q.error ?? undo.error} />
      {q.data && q.data.length === 0 && <Empty>No packages exported or imported yet.</Empty>}
      {q.data && q.data.length > 0 && (
        <div className="overflow-x-auto scroll-thin">
          <table className="w-full border-collapse text-[13px]" style={{ minWidth: 720 }}>
            <thead>
              <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
                <th scope="col" className="font-medium px-2 py-2 border-b border-line">When</th>
                <th scope="col" className="font-medium px-2 py-2 border-b border-line">Direction</th>
                <th scope="col" className="font-medium px-2 py-2 border-b border-line">Dataset</th>
                <th scope="col" className="font-medium px-2 py-2 border-b border-line">Counts</th>
                <th scope="col" className="font-medium px-2 py-2 border-b border-line">Status</th>
                <th scope="col" className="font-medium px-2 py-2 border-b border-line" />
              </tr>
            </thead>
            <tbody>
              {q.data.map((p) => {
                const out = p.counts.outcome;
                return (
                  <Fragment key={p.id}>
                    <tr className="border-b border-track">
                      <td className="px-2 py-2 text-xs text-muted">{ago(p.created_at)}</td>
                      <td className="px-2 py-2">{p.direction === "export" ? "Export" : "Import"}</td>
                      <td className="px-2 py-2">{p.dataset_id ? <Link to={`/datasets/${p.dataset_id}`}>{p.dataset_name ?? `dataset ${p.dataset_id}`}</Link> : p.dataset_name ?? "—"}</td>
                      <td className="px-2 py-2 text-xs text-muted num">
                        {num(p.counts.files)} files · {num(p.counts.segments)} segments
                        {out && ` · ${Object.entries(out).map(([k, n]) => `${n} ${(OUTCOME_LABELS[k] ?? k).toLowerCase()}`).join(", ")}`}
                      </td>
                      <td className="px-2 py-2">
                        <Chip tone={p.status === "applied" ? "good" : p.status === "undone" ? "default" : "info"}>{p.status}</Chip>
                        {p.conflicts_open > 0 && <span className="ml-1"><Chip tone="warn">{p.conflicts_open} open</Chip></span>}
                      </td>
                      <td className="px-2 py-2 whitespace-nowrap">
                        <span className="flex gap-2 justify-end">
                          {p.direction === "export" && p.file && <a href={`/api/sync/exports/${p.id}/download`} className="text-xs">Download</a>}
                          {p.direction === "import" && p.conflicts > 0 && p.status === "applied" && (
                            <Button size="sm" onClick={() => setOpen(open === p.id ? null : p.id)}>Conflicts</Button>
                          )}
                          {p.direction === "import" && p.status === "applied" && (
                            <Button size="sm" variant="danger" disabled={undo.isPending} onClick={() => { if (window.confirm("Restore the labels as they were before this import? Edits made since are lost.")) undo.mutate(p.id); }}>
                              Undo
                            </Button>
                          )}
                        </span>
                      </td>
                    </tr>
                    {open === p.id && (
                      <tr>
                        <td colSpan={6} className="p-3 bg-canvas/60"><Resolver id={p.id} /></td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function Resolver({ id }: { id: number }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["import", id], queryFn: () => api.get<ImportStaged>(`/sync/imports/${id}`) });
  const resolve = useMutation({
    mutationFn: ({ key, choice }: { key: string; choice: string }) => api.post(`/sync/imports/${id}/resolve`, { key, choice }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["import", id] });
      void qc.invalidateQueries({ queryKey: ["sync-history"] });
    },
  });
  if (!q.data) return <Spinner />;
  return (
    <>
      <ErrorNote error={resolve.error} />
      <ConflictList conflicts={q.data.conflict_list ?? []} choice={(c) => c.resolution} onChoose={(c, v) => resolve.mutate({ key: c.key, choice: v })} note="Pick the version to keep; the choice is applied straight away." />
    </>
  );
}
