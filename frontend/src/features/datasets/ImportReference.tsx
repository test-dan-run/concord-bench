// Datasets → Import reference package (SPLIT_PLAN.md §4.3, CONTRACTS.md §2): upload a package (zip or
// folder) or find one Concord Label pushed, check the preview, then accept it into a new dataset
// (a new version when it comes from the same source as an earlier one) or reject it.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type ReferenceImport } from "../../lib/api";
import { ago, bytes, hours, num } from "../../lib/format";
import { skippedSummary, sortImports, sourceLine } from "../../lib/peer";
import { pickedFromDrop, pickedFromInput, uploadFiles, type PickedFile } from "../../lib/upload";
import * as I from "../../components/icons";
import { TagInput } from "../../components/TagInput";
import { Button, Chip, cx, Empty, ErrorNote, Fact, PageHeader, Panel, Spinner } from "../../components/ui";

const TEMPLATE = "reference_package.zip";

function labelChips(types: string[]) {
  return types.map((l) => <Chip key={l}>{l === "asr" ? "ASR" : l === "diar" ? "Diarisation" : l}</Chip>);
}

export function ImportReference() {
  const qc = useQueryClient();
  const [drag, setDrag] = useState(false);
  const [progress, setProgress] = useState<[number, number] | null>(null);
  const imports = useQuery({
    queryKey: ["reference-imports"],
    queryFn: () => api.get<ReferenceImport[]>("/reference-imports"),
    refetchInterval: (q) => (q.state.data?.some((i) => i.status === "accepting") ? 1500 : 10_000),
  });
  const upload = useMutation({
    mutationFn: async (files: PickedFile[]) => {
      setProgress([0, files.reduce((a, f) => a + f.file.size, 0)]);
      const upload_id = await uploadFiles(files, (s, t) => setProgress([s, t]));
      return api.post<{ id: number }>("/reference-imports", { upload_id });
    },
    onSettled: () => {
      setProgress(null);
      void qc.invalidateQueries({ queryKey: ["reference-imports"] });
    },
  });
  const onFiles = (files: PickedFile[]) => files.length && upload.mutate(files);
  const { waiting, decided } = sortImports(imports.data ?? []);

  return (
    <>
      <PageHeader
        crumbs={<><Link to="/datasets">Datasets</Link> / Import reference package</>}
        title="Import reference package"
        subtitle="Reviewed references from Concord Label, another tool or by hand. Each accepted package becomes a new dataset; a package from the same source becomes a new version, so scored references never change."
      />
      <div className="flex flex-col xl:flex-row gap-5 items-start">
        <div className="flex-1 w-full flex flex-col gap-5 min-w-0">
          <Panel title="Waiting for a decision" subtitle="Packages you uploaded and packages Concord Label pushed. Nothing is stored as a dataset until you accept.">
            {imports.isLoading && <Spinner />}
            <ErrorNote error={imports.error} />
            {imports.data && waiting.length === 0 && <Empty>No staged packages.</Empty>}
            {waiting.map((imp) => (
              <StagedImport key={imp.id} imp={imp} />
            ))}
          </Panel>
          <Panel title="History">
            {decided.length === 0 && <span className="text-[13px] text-muted">Nothing imported or rejected yet.</span>}
            {decided.map((imp) => (
              <div key={imp.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px] border-b border-track last:border-0 py-1.5">
                <Chip tone={imp.status === "accepted" ? "good" : imp.status === "failed" ? "bad" : "default"}>{imp.status}</Chip>
                <span className="flex-1 min-w-[180px]">
                  {imp.dataset_id && imp.status === "accepted" ? <Link to={`/datasets/${imp.dataset_id}`}>{imp.preview.dataset_name}</Link> : imp.name}
                </span>
                <span className="text-xs text-muted">{sourceLine(imp.preview)}</span>
                <span className="text-xs text-subtle">{ago(imp.decided_at ?? imp.created_at)}</span>
                {imp.result.outputs && (imp.result.outputs.imported > 0 || imp.result.outputs.lines > 0) && (
                  <span className="w-full text-xs text-muted num">
                    Seg-model outputs: {imp.result.outputs.imported} imported into the cache
                    {imp.result.outputs.already_cached ? `, ${imp.result.outputs.already_cached} already cached` : ""}
                    {skippedSummary(imp.result.outputs.skipped) && ` · skipped: ${skippedSummary(imp.result.outputs.skipped)}`}
                  </span>
                )}
                {imp.error && <span className="w-full text-xs text-bad">{imp.error}</span>}
              </div>
            ))}
          </Panel>
        </div>
        <aside className="w-full xl:w-[380px] shrink-0 flex flex-col gap-4 order-first xl:order-last">
          <Panel title="Upload a package">
            <div
              onDragOver={(e) => {
                e.preventDefault();
                setDrag(true);
              }}
              onDragLeave={() => setDrag(false)}
              onDrop={async (e) => {
                e.preventDefault();
                setDrag(false);
                onFiles(await pickedFromDrop(e));
              }}
              className={cx("border-2 border-dashed rounded-lg p-5 flex flex-col items-center gap-2 text-center", drag ? "border-primary bg-primary/5" : "border-line")}
            >
              <span className="text-muted"><I.Upload size={22} /></span>
              <span className="text-[13px]">Drop a package folder or its .zip</span>
              <span className="text-xs text-subtle">manifest.json, files.csv, audio/ and one file under refs/ (CONTRACTS.md §2). A folder without a manifest is read like an annotated upload.</span>
              <span className="flex flex-wrap justify-center gap-2 mt-1">
                <label className="inline-flex items-center gap-2 min-h-9 px-3 rounded-md border border-line text-[13px] cursor-pointer hover:bg-track">
                  Choose folder
                  <input aria-label="Package folder" type="file" className="sr-only" multiple {...({ webkitdirectory: "" } as object)} onChange={(e) => onFiles(pickedFromInput(e.target.files))} />
                </label>
                <label className="inline-flex items-center gap-2 min-h-9 px-3 rounded-md border border-line text-[13px] cursor-pointer hover:bg-track">
                  Choose zip
                  <input aria-label="Package zip" type="file" className="sr-only" accept=".zip" onChange={(e) => onFiles(pickedFromInput(e.target.files))} />
                </label>
              </span>
            </div>
            {upload.isPending && progress && (
              <span className="text-xs text-muted num">
                {progress[0] < progress[1] ? `Uploading · ${bytes(progress[0])} of ${bytes(progress[1])}` : "Checking the package…"}
              </span>
            )}
            <ErrorNote error={upload.error} />
            <a href={`/api/templates/${TEMPLATE}`} download className="inline-flex items-center gap-1.5 text-xs self-start">
              <I.Download size={12} /> Template package ({TEMPLATE})
            </a>
          </Panel>
          <Panel title="Pushed from Concord Label">
            <span className="text-xs text-muted">
              Concord Label's “Send to Bench” uploads packages here when its BENCH_URL points at this app. They wait above until you accept them.
            </span>
          </Panel>
        </aside>
      </div>
    </>
  );
}

function StagedImport({ imp }: { imp: ReferenceImport }) {
  const qc = useQueryClient();
  const nav = useNavigate();
  const p = imp.preview;
  const [name, setName] = useState(p.dataset_name);
  const [tags, setTags] = useState<string[]>(p.tags);
  const [language, setLanguage] = useState(p.language ?? "");
  const accept = useMutation({
    mutationFn: () => api.post<{ dataset: { id: number } }>(`/reference-imports/${imp.id}/accept`, { name, tags, language }),
    onSuccess: (r) => {
      void qc.invalidateQueries();
      nav(`/datasets/${r.dataset.id}`);
    },
  });
  const reject = useMutation({
    mutationFn: () => api.post(`/reference-imports/${imp.id}/reject`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["reference-imports"] }),
  });
  const review = p.review ? Object.entries(p.review).map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`).join(" · ") : null;
  return (
    <article aria-label={`Staged package ${p.name}`} className="flex flex-col gap-3 border border-track rounded-md p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-[14px]">{p.name}</span>
        {p.export_version != null && <Chip tone="info">v{p.export_version}</Chip>}
        {labelChips(p.label_types)}
        {p.scope && <Chip>scope {p.scope}</Chip>}
        <span className="text-xs text-subtle ml-auto">{ago(imp.created_at)}</span>
      </div>
      <span className="text-xs text-muted">{sourceLine(p)}</span>
      <div className="grid gap-x-6 sm:grid-cols-2">
        <Fact k="Files" v={`${num(p.files)} · ${num(p.new_files)} new to Bench`} />
        <Fact k="Audio" v={hours(p.hours)} />
        <Fact k="Segments" v={num(p.segments)} />
        <Fact k="References" v={p.refs_file ?? "annotation files in the folder"} />
        <Fact k="Labels for slicing" v={p.labels.length ? p.labels.join(", ") : "none"} />
        {review && <Fact k="Review" v={review} />}
        {p.models && p.models.length > 0 && <Fact k="Pseudo-label models" v={p.models.join(", ")} />}
        {p.outputs > 0 && <Fact k="Seg-model outputs" v={`${p.outputs} (imported when the image matches)`} />}
        {p.normaliser && <Fact k="Suggested normaliser" v={p.normaliser} />}
      </div>
      {p.previous_version && (
        <span className="text-xs">
          New version of <Link to={`/datasets/${p.previous_version.dataset_id}`}>{p.previous_version.name}</Link>
          {p.changes && (
            <span className="text-muted num">
              {" "}· {p.changes.files_added} files added, {p.changes.files_removed} removed · segments {p.changes.segments_before} → {p.changes.segments_now}
            </span>
          )}
        </span>
      )}
      <div className="grid gap-3 sm:grid-cols-[2fr_1fr]">
        <label className="flex flex-col gap-1.5 text-xs text-muted">
          Dataset name
          <input value={name} onChange={(e) => setName(e.target.value)} className="min-h-10 bg-canvas border border-line rounded-md px-3 text-[13px] text-fg" />
        </label>
        <label className="flex flex-col gap-1.5 text-xs text-muted">
          Language
          <input value={language} onChange={(e) => setLanguage(e.target.value)} className="min-h-10 bg-canvas border border-line rounded-md px-3 text-[13px] text-fg num" />
        </label>
      </div>
      <TagInput value={tags} onChange={setTags} label="Tags" />
      <ErrorNote error={accept.error ?? reject.error} />
      <span className="flex flex-wrap gap-2">
        <Button variant="primary" disabled={!name.trim() || accept.isPending || imp.status !== "staged"} onClick={() => accept.mutate()}>
          <I.Check size={14} /> {imp.status === "accepting" ? "Importing…" : "Accept"}
        </Button>
        <Button variant="danger" disabled={reject.isPending || imp.status !== "staged"} onClick={() => confirm(`Reject “${p.name}”? The package is deleted.`) && reject.mutate()}>
          Reject
        </Button>
      </span>
    </article>
  );
}
