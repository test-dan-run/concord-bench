import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type Inspection, type TaskMatch } from "../../lib/api";
import { bytes, num } from "../../lib/format";
import { pickedFromDrop, pickedFromInput, uploadFiles, type PickedFile } from "../../lib/upload";
import * as I from "../../components/icons";
import { TagInput } from "../../components/TagInput";
import { Button, Chip, cx, ErrorNote, Fact, PageHeader, Panel, Spinner } from "../../components/ui";
import { EMPTY_PLAN, PlanPanel, planCount, type PlanValue } from "../benchmarks/PlanPanel";

const TASK_INFO: Record<string, { title: string; feeds: string }> = {
  asr: { title: "ASR transcripts", feeds: "WER" },
  diar: { title: "Speaker labels", feeds: "DER, VAD error" },
  spk_asr: { title: "Speaker-attributed transcripts", feeds: "WER, DER and cpWER" },
};

const LANGS = ["en", "en-SG", "en-GB", "en-US", "zh", "ms", "ta", "mixed"];

export function labelTypesOf(ins: Inspection | null): string[] {
  if (!ins) return [];
  const t = Object.keys(ins.tasks);
  const out = [];
  if (t.includes("asr") || t.includes("spk_asr")) out.push("asr");
  if (t.includes("diar") || t.includes("spk_asr")) out.push("diar");
  return out;
}

/** Concord Bench benchmarks against references: at least one audio file must have matched annotations. */
export function hasReferences(ins: Inspection | null): boolean {
  return !!ins && ins.audio_files > 0 && ins.audio_without_labels.length < ins.audio_files && Object.keys(ins.tasks).length > 0;
}

export const NO_REFERENCES =
  "No references matched. Concord Bench benchmarks against references: add transcript or speaker-label files named like the audio files (see the templates below). To label audio, use Concord Label.";

export function UploadDataset() {
  const nav = useNavigate();
  const [name, setName] = useState("");
  const [language, setLanguage] = useState("en");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [source, setSource] = useState<"files" | "path">("files");
  const [localPath, setLocalPath] = useState("");
  const [picked, setPicked] = useState<PickedFile[]>([]);
  const [uploadId, setUploadId] = useState<string | null>(null);
  const [progress, setProgress] = useState<[number, number] | null>(null);
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [plan, setPlan] = useState<PlanValue>(EMPTY_PLAN);
  const [drag, setDrag] = useState(false);
  const templates = useQuery({ queryKey: ["templates"], queryFn: () => api.get<{ name: string; size: number }[]>("/templates") });

  const upload = useMutation({
    mutationFn: async (files: PickedFile[]) => {
      setInspection(null);
      setProgress([0, files.reduce((a, f) => a + f.file.size, 0)]);
      const id = await uploadFiles(files, (s, t) => setProgress([s, t]));
      setUploadId(id);
      const ins = await api.get<Inspection>(`/uploads/${id}/inspect`);
      setInspection(ins);
      return ins;
    },
  });
  const inspectPath = useMutation({
    mutationFn: () => api.post<Inspection>("/uploads/inspect-path", { path: localPath }),
    onSuccess: (ins) => {
      setInspection(ins);
      setUploadId(null);
    },
  });
  const create = useMutation({
    mutationFn: () =>
      api.post<{ dataset: { id: number } }>("/datasets", {
        name: name.trim(),
        language,
        description: description || null,
        tags,
        upload_id: source === "files" ? uploadId : null,
        local_path: source === "path" ? localPath : null,
        plan: planCount(plan, labelTypesOf(inspection)) > 0 ? plan : null,
      }),
    onSuccess: (r) => nav(`/datasets/${r.dataset.id}`),
  });

  const onFiles = (files: PickedFile[]) => {
    if (!files.length) return;
    setPicked(files);
    if (!name) {
      const top = files[0].path.split("/")[0];
      setName(top.replace(/\.zip$/i, "").replace(/[_-]+/g, " "));
    }
    upload.mutate(files);
  };

  const labels = labelTypesOf(inspection);
  const n = planCount(plan, labels);
  const ready = hasReferences(inspection) && !!name.trim() && (source === "path" || !!uploadId);
  const cta = !inspection ? "Upload" : n > 0 ? `Upload and run ${n} ${plan.mode === "grid" ? `pipeline${n === 1 ? "" : "s"}` : `benchmark${n === 1 ? "" : "s"}`}` : "Upload dataset";

  return (
    <>
      <PageHeader
        crumbs={<><Link to="/benchmarks">Benchmarks</Link> / Upload dataset</>}
        title="Upload dataset"
        subtitle="Audio with its references (transcripts and/or speaker labels). Each upload is its own dataset and becomes a benchmark straight away."
      />
      <div className="flex flex-col xl:flex-row gap-5 items-start">
        <div className="flex-1 w-full flex flex-col gap-5 min-w-0">
          <Panel title="Details">
            <div className="grid gap-3 sm:grid-cols-[2fr_1fr]">
              <label className="flex flex-col gap-1.5 text-xs text-muted">
                Name
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Call Centre · Oct 2026" className="min-h-10 bg-canvas border border-line rounded-md px-3 text-[13px] text-fg" />
              </label>
              <label className="flex flex-col gap-1.5 text-xs text-muted">
                Language
                <input list="langs" value={language} onChange={(e) => setLanguage(e.target.value)} className="min-h-10 bg-canvas border border-line rounded-md px-3 text-[13px] text-fg num" />
                <datalist id="langs">
                  {LANGS.map((l) => (
                    <option key={l} value={l} />
                  ))}
                </datalist>
              </label>
            </div>
            <label className="flex flex-col gap-1.5 text-xs text-muted">
              Description
              <textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} className="bg-canvas border border-line rounded-md px-3 py-2 text-[13px] text-fg" />
            </label>
            <TagInput value={tags} onChange={setTags} label="Tags · matched against model tags to suggest what to benchmark" />
          </Panel>
          <Panel title="Audio" actions={
            <span role="group" aria-label="Audio source" className="inline-flex border border-line rounded-md overflow-hidden">
              {(["files", "path"] as const).map((s) => (
                <button key={s} type="button" aria-pressed={source === s} onClick={() => { setSource(s); setInspection(null); }} className={cx("min-h-8 px-3 text-xs border-0 cursor-pointer", source === s ? "bg-active text-fg" : "bg-transparent text-muted")}>
                  {s === "files" ? "Upload files" : "Local path"}
                </button>
              ))}
            </span>
          }>
            {source === "files" ? (
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
                className={cx("border-2 border-dashed rounded-lg p-6 flex flex-col items-center gap-2 text-center", drag ? "border-primary bg-primary/5" : "border-line")}
              >
                <span className="text-muted"><I.Upload size={22} /></span>
                <span className="text-[13px]">Drop a folder or .zip of audio and annotations</span>
                <span className="text-xs text-subtle">WAV, FLAC, MP3, OGG, M4A · any sample rate · originals are kept, models get 16 kHz mono · any length</span>
                <span className="flex gap-2 mt-1">
                  <label className="inline-flex items-center gap-2 min-h-9 px-3 rounded-md border border-line text-[13px] cursor-pointer hover:bg-track">
                    Choose folder
                    <input type="file" className="sr-only" multiple {...({ webkitdirectory: "" } as object)} onChange={(e) => onFiles(pickedFromInput(e.target.files))} />
                  </label>
                  <label className="inline-flex items-center gap-2 min-h-9 px-3 rounded-md border border-line text-[13px] cursor-pointer hover:bg-track">
                    Choose files / zip
                    <input type="file" className="sr-only" multiple accept=".zip,audio/*,.jsonl,.json,.csv,.tsv,.txt,.rttm,.stm" onChange={(e) => onFiles(pickedFromInput(e.target.files))} />
                  </label>
                </span>
              </div>
            ) : (
              <div className="flex flex-wrap items-end gap-2">
                <label className="flex flex-col gap-1.5 text-xs text-muted flex-1 min-w-[280px]">
                  Folder on this machine (copied in; the folder can move afterwards)
                  <input value={localPath} onChange={(e) => setLocalPath(e.target.value)} placeholder="/data/recordings/call-centre-oct" className="min-h-10 bg-canvas border border-line rounded-md px-3 text-[13px] text-fg num" />
                </label>
                <Button onClick={() => inspectPath.mutate()} disabled={!localPath || inspectPath.isPending}>
                  Inspect
                </Button>
              </div>
            )}
            {upload.isPending && progress && (
              <div className="flex flex-col gap-1">
                <span className="text-xs text-muted num">
                  Uploading {picked.length} files · {bytes(progress[0])} of {bytes(progress[1])}
                </span>
                <span className="h-1.5 rounded bg-track overflow-hidden">
                  <span className="block h-full bg-primary" style={{ width: `${(progress[0] / Math.max(1, progress[1])) * 100}%` }} />
                </span>
              </div>
            )}
            {(inspectPath.isPending || (upload.isPending && progress && progress[0] >= progress[1])) && <Spinner label="Reading annotations" />}
            <ErrorNote error={upload.error ?? inspectPath.error} />
            {inspection && <AudioSummary ins={inspection} />}
          </Panel>
          <Panel title="Annotations" subtitle="Detected from the files you added and matched to audio by file name (without extension).">
            {!inspection && <span className="text-[13px] text-muted">Add audio and annotation files above to see what was matched.</span>}
            {inspection && <Annotations ins={inspection} />}
            <div className="flex flex-col gap-1.5">
              <span className="text-[11px] text-muted uppercase tracking-wider">Templates</span>
              <span className="flex flex-wrap gap-2">
                {(templates.data ?? []).map((t) => (
                  <a key={t.name} href={`/api/templates/${t.name}`} download className="inline-flex items-center gap-1.5 text-xs border border-line rounded-md px-2 py-1 !text-muted hover:!text-fg no-underline num">
                    <I.Download size={12} /> {t.name}
                  </a>
                ))}
              </span>
            </div>
          </Panel>
        </div>
        <aside className="w-full xl:w-[360px] shrink-0 flex flex-col gap-4">
          <Panel title="Summary">
            <Fact k="Name" v={name || "—"} />
            <Fact k="Audio" v={inspection ? `${num(inspection.audio_files)} files · ${bytes(inspection.audio_bytes)}` : "—"} />
            <Fact k="Labels" v={labels.length ? labels.map((l) => (l === "asr" ? "ASR" : "Diarisation")).join(" + ") : "—"} />
            {tags.length > 0 && <span className="flex flex-wrap gap-1">{tags.map((t) => <Chip key={t}>#{t}</Chip>)}</span>}
          </Panel>
          <Panel title="Benchmark plan" subtitle="Ranked by tags shared with this dataset">
            {hasReferences(inspection) ? <PlanPanel tags={tags} labelTypes={labels} language={language} value={plan} onChange={setPlan} /> : <span className="text-[13px] text-muted">Shown once annotations are matched.</span>}
            <Link to="/models" className="text-xs">Models in the catalog</Link>
          </Panel>
          {inspection && !hasReferences(inspection) && <span role="alert" className="text-xs text-warn">{NO_REFERENCES}</span>}
          <ErrorNote error={create.error} />
          <Button variant="primary" disabled={!ready || create.isPending} onClick={() => create.mutate()}>
            {create.isPending ? "Starting…" : cta}
          </Button>
          <span className="text-[11px] text-subtle">Ingest runs as a job: originals are copied, 16 kHz mono and waveform peaks are derived, and the services the plan needs are started.</span>
        </aside>
      </div>
    </>
  );
}

function AudioSummary({ ins }: { ins: Inspection }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-4 text-[13px]">
        <span><span className="num">{num(ins.audio_files)}</span> <span className="text-muted">audio files</span></span>
        <span><span className="num">{bytes(ins.audio_bytes)}</span></span>
        <span><span className="num">{ins.annotation_files}</span> <span className="text-muted">annotation files</span></span>
      </div>
      {ins.duplicate_stems.length > 0 && <span className="text-xs text-warn">Duplicate file names (only the first gets labels): {ins.duplicate_stems.join(", ")}</span>}
      {ins.ignored.length > 0 && <span className="text-xs text-subtle">Ignored: {ins.ignored.slice(0, 6).join(", ")}{ins.ignored.length > 6 ? "…" : ""}</span>}
      <span className="text-xs text-subtle">Sample rates, clipping and length are checked during ingest.</span>
    </div>
  );
}

function Annotations({ ins }: { ins: Inspection }) {
  const tasks = Object.entries(ins.tasks) as [string, TaskMatch][];
  return (
    <div className="flex flex-col gap-4">
      {tasks.length === 0 && <span className="text-[13px] text-warn">No annotations matched. Add a transcript or speaker-label file named like the audio files.</span>}
      {tasks.map(([task, m]) => (
        <div key={task} className="flex flex-col gap-2 border border-track rounded-md p-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-[13px]">{TASK_INFO[task]?.title ?? task}</span>
            <Chip tone={m.files === ins.audio_files ? "good" : "warn"}>
              <span className="num">{m.files} of {ins.audio_files} files matched</span>
            </Chip>
            {m.formats.map((f) => (
              <Chip key={f}>{f}</Chip>
            ))}
            <span className="ml-auto text-[11px] text-subtle">feeds {TASK_INFO[task]?.feeds}</span>
          </div>
          <div className="overflow-x-auto scroll-thin">
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="text-muted text-left">
                  <th scope="col" className="font-medium py-1 pr-3">File</th>
                  <th scope="col" className="font-medium py-1 pr-3">Span</th>
                  <th scope="col" className="font-medium py-1 pr-3">Speaker</th>
                  <th scope="col" className="font-medium py-1">Transcript</th>
                </tr>
              </thead>
              <tbody>
                {m.preview.map((p, i) => (
                  <tr key={i} className="border-t border-track">
                    <td className="py-1 pr-3 num">{p.file}</td>
                    <td className="py-1 pr-3 num text-muted">{p.start === null ? "whole file" : `${p.start.toFixed(2)}–${p.end?.toFixed(2)}`}</td>
                    <td className="py-1 pr-3 num">{p.speaker ?? "—"}</td>
                    <td className="py-1 truncate max-w-[360px]">{p.text ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
      {ins.unmatched.length > 0 && (
        <span className="text-xs text-warn">
          {ins.unmatched.length} annotation file name{ins.unmatched.length > 1 ? "s" : ""} match no audio: {ins.unmatched.slice(0, 8).join(", ")}
          {ins.unmatched.length > 8 ? "…" : ""}
        </span>
      )}
      {ins.audio_without_labels.length > 0 && tasks.length > 0 && <span className="text-xs text-muted">{ins.audio_without_labels.length} audio files have no labels.</span>}
      {ins.parse_errors.map((e) => (
        <div key={e.file} className="text-xs text-bad">
          {e.file} ({e.format}): {e.count} problem{e.count > 1 ? "s" : ""} — {e.errors.slice(0, 3).join("; ")}
        </div>
      ))}
    </div>
  );
}
