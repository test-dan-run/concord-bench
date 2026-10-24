// Thin fetch wrapper + the response shapes the pages use. The backend is the source of truth for
// field names (backend/concord_bench/api/*.py).

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {};
  let payload: BodyInit | undefined;
  if (body instanceof FormData || body instanceof Blob) payload = body;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const r = await fetch(`/api${path}`, { method, headers, body: payload, ...init });
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* not JSON */
    }
    throw new ApiError(r.status, msg);
  }
  if (r.status === 204) return undefined as T;
  return (await r.json()) as T;
}

export const api = {
  get: <T>(p: string) => request<T>("GET", p),
  post: <T>(p: string, b?: unknown) => request<T>("POST", p, b),
  put: <T>(p: string, b?: unknown) => request<T>("PUT", p, b),
  patch: <T>(p: string, b?: unknown) => request<T>("PATCH", p, b),
  del: <T>(p: string) => request<T>("DELETE", p),
};

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  const s = u.toString();
  return s ? `?${s}` : "";
}

// ---------------------------------------------------------------- shapes

export interface TaskDef {
  id: string;
  label: string;
  label_type: string;
  metric: string;
  enabled: boolean;
}

export interface AppConfig {
  app: "concord-bench";
  version: string;
  contract_version: number;
  peer: { url: string | null; reachable: boolean; contract_version: number | null };
  tasks: TaskDef[];
  default_normaliser: string;
  default_collar: number;
  collars: number[];
  replay: boolean;
  /** The model catalog: models, leases and the gateway; `ui_url` is where models are registered and verified. */
  catalog: { url: string; ui_url: string };
}

/** A catalog lease a job waits on ("waiting for compute"). */
export interface JobLease {
  id: string;
  state: "queued" | "starting" | "active" | "released" | "expired" | "failed";
  position: number | null;
  waiting_for: string | null;
  models: string[] | null;
}

export interface Job {
  id: number;
  type: string;
  status: "queued" | "waiting" | "running" | "done" | "failed" | "blocked";
  progress: number;
  message: string | null;
  error: string | null;
  required_services: string[];
  after: number[];
  lease: JobLease | null;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  label?: string;
  log?: string[];
}

export interface Dataset {
  id: number;
  name: string;
  language: string | null;
  description: string | null;
  tags: string[];
  label_types: string[];
  status: string;
  created_at: string;
  files: number | null;
  usable_files: number | null;
  hours: number | null;
  usable_hours: number | null;
  has_speaker_text: boolean;
  flagged: Record<string, number>;
  report?: DatasetReport;
  runs?: number;
  ingest_job?: Job | null;
  provenance: Provenance | Record<string, never>;
  export_version: number | null;
  previous_version_id: number | null;
  versions?: { id: number; name: string; export_version: number | null; status: string; created_at: string }[];
  previous_plan?: PreviousPlan | null;
}

// ---------------------------------------------------------------- reference packages (CONTRACTS.md §2, §4)

export interface PackageSource {
  app: string;
  install_id: string;
  dataset_uid: string;
  dataset_name: string;
}

/** Where a dataset's references came from (an accepted reference package with a manifest). */
export interface Provenance {
  import_id: number;
  package_name: string;
  source: PackageSource | null;
  export_version: number | null;
  scope: "complete" | "any" | null;
  created_at: string | null;
  label_types: string[] | null;
  counts: { files: number; segments: number; hours: number } | null;
  audio_included: string | null;
  review: Record<string, unknown> | null;
  models: string[] | null;
  normaliser: string | null;
}

export interface PreviousPlan {
  previous: { id: number; name: string; export_version: number | null };
  plan: { seg_models: string[]; asr_models: string[]; oracle: boolean; mode: "grid" | "stages" };
  models: number;
  already_run: boolean;
}

export interface ImportPreview {
  name: string;
  source: PackageSource | null;
  export_version: number | null;
  files: number;
  new_files: number;
  hours: number;
  segments: number;
  label_types: string[];
  previous_version: { dataset_id: number; name: string; export_version: number | null } | null;
  has_manifest: boolean;
  dataset_name: string;
  scope: string | null;
  language: string | null;
  description: string | null;
  tags: string[];
  created_at: string | null;
  review: Record<string, unknown> | null;
  models: string[] | null;
  normaliser: string | null;
  audio_included: string | null;
  refs_file: string | null;
  uids: number;
  outputs: number;
  missing_files: string[];
  changes?: { files_added: number; files_removed: number; segments_before: number; segments_now: number };
}

export interface ReferenceImport {
  id: number;
  status: "staged" | "accepting" | "accepted" | "rejected" | "failed";
  name: string;
  preview: ImportPreview;
  result: {
    uids?: { matched: number; unmatched: number };
    outputs?: { lines: number; imported: number; already_cached: number; skipped: Record<string, number> };
  };
  error: string | null;
  dataset_id: number | null;
  job_id: number | null;
  created_at: string;
  decided_at: string | null;
}

export interface TaskMatch {
  records: number;
  files: number;
  formats: string[];
  preview: { file: string; start: number | null; end: number | null; speaker: string | null; text: string | null }[];
}

export interface Inspection {
  audio_files: number;
  audio_bytes: number;
  annotation_files: number;
  ignored: string[];
  tasks: Record<string, TaskMatch>;
  unmatched: string[];
  audio_without_labels: string[];
  duplicate_stems: string[];
  parse_errors: { file: string; format: string; errors: string[]; count: number }[];
  files_by_format: { file: string; format: string; records: number; tasks: string[] }[];
}

export interface DatasetReport {
  files: number;
  usable_files: number;
  hours: number;
  usable_hours: number;
  sample_rates: Record<string, number>;
  flagged: Record<string, { file: string; error?: string; clipped_pct?: number; duration?: number }[]>;
  tasks: Record<string, TaskMatch>;
  unmatched: string[];
  audio_without_labels: string[];
  audio_without_labels_count: number;
  duplicate_stems: string[];
  parse_errors: Inspection["parse_errors"];
  has_speaker_text: boolean;
}

export type ModelState = "running" | "stopped" | "starting" | "stopping" | "queued" | "failed";

/** A model from the model catalog (GET /models), joined with Bench's row once it has been used. */
export interface CatalogModel {
  catalog_id: string;
  model_id: number | null;
  in_catalog: boolean;
  display_name: string;
  description: string | null;
  tags: string[];
  parent_id: string | null;
  contract: string | null;
  api: string | null;
  engine: string | null;
  image: string | null;
  build_key: string | null;
  state: ModelState | null;
  state_detail: string | null;
  leased_by: string[];
  capabilities: string[];
  capability_status: "pending" | "running" | "verified" | "failed" | "stale" | "unknown" | null;
  usable_as: string | null;
  timing_level: string | null;
  output_detail: string[];
  rtf: number | null;
  verified_at: string | null;
  has_speakers: boolean;
  stages: string[];
  cold_start_s: number | null;
  cold_start_measured: boolean;
  gpu: { fraction?: number; gb?: number } | null;
  usable: boolean;
  not_usable: string | null;
  runs: number;
}

export interface CatalogInfo {
  url: string;
  ui_url: string;
  available: boolean;
  error: string | null;
}

export interface ModelList {
  models: CatalogModel[];
  catalog: CatalogInfo;
}

/** POST /benchmarks/plan/preview: the catalog's plan for a benchmark plan, before it is queued. */
export interface PlanStep {
  stage: string;
  model: string;
  load: boolean;
  cold_start_s: number;
  items: number | null;
  hits: number | null;
  missing: number | null;
  skip: boolean;
}

export interface PlanPreview {
  runs: number;
  mode: "grid" | "stages";
  plan: { pipeline: string; steps: PlanStep[]; loads: number; est_load_s: number; hits: number; fits_together: boolean } | null;
  error: string | null;
}

export interface Normaliser {
  id: string;
  key: string;
  label: string;
  description: string;
  default: boolean;
  service: string | null;
}

export interface Breakdown {
  parts: { key: string; label: string; count?: number; seconds?: number; pct: number | null }[];
  facts: Record<string, number | null>;
}

export interface BenchCell {
  model_id: number;
  dataset_id: number;
  status: string | null;
  run_id: number | null;
  value: number | null;
  prev: number | null;
  delta: number | null;
  metric?: string;
  score_id?: number;
  breakdown?: Breakdown;
  rtf?: number | null;
  finished_at?: string | null;
  image?: string | null;
  build_key?: string | null;
  error?: string | null;
  rerunning?: boolean;
  pending_score_job?: number;
}

export interface BenchDataset {
  id: number;
  name: string;
  tags: string[];
  label_types: string[];
  hours: number | null;
  files: number | null;
  has_labels: boolean;
  status: string;
  has_speaker_text: boolean;
  language: string | null;
}

export interface BenchTable {
  tab: string;
  normaliser: string;
  collar: number;
  datasets: BenchDataset[];
  models: { id: number; display_name: string; catalog_id: string; tags: string[]; capabilities: string[]; timing_level: string | null; kind: string }[];
  cells: BenchCell[];
  recent_runs: { id: number; label: string; dataset: string; status: string; finished_at: string | null }[];
}

export interface Attribution {
  n: number;
  a_errors: number;
  b_errors: number;
  c_errors: number;
  asr: number | null;
  segmentation: number | null;
  speaker: number | null;
  cpwer: number | null;
  with_reference_segments: number | null;
  asr_errors: number;
  segmentation_errors: number;
  speaker_errors: number;
}

export interface PipeCell {
  seg_model_id: number | null;
  asr_model_id: number;
  pipeline_id: number | null;
  status: string | null;
  run_id: number | null;
  has_speakers: boolean;
  oracle: boolean;
  score_id?: number;
  wer?: number | null;
  cpwer?: number | null;
  der?: number | null;
  rtf?: number | null;
  rtf_by_stage?: Record<string, number | null>;
  attribution?: Attribution | null;
  wrong_speaker_pairs?: { word: string; move: string; n: number }[];
  label?: string;
  prev_cpwer?: number;
  error?: string | null;
}

export interface PipeGrid {
  normaliser: string;
  collar: number;
  dataset_id: number | null;
  dataset?: BenchDataset;
  datasets: BenchDataset[];
  rows: { seg_model_id: number | null; name: string; oracle: boolean; has_speakers: boolean; available: boolean }[];
  cols: { asr_model_id: number; name: string; timing_level: string | null }[];
  cells: PipeCell[];
  runs: PipeCell[];
}

export interface Run {
  id: number;
  task: string;
  label: string;
  model_id: number | null;
  pipeline_id: number | null;
  dataset_id: number;
  dataset: string;
  status: string;
  error: string | null;
  /** The builds behind the run's outputs, per catalog model; runs from before the catalog have only `image`. */
  builds: Record<string, { build_key?: string | null; image?: string | null; labels?: Record<string, string> }>;
  params: Record<string, unknown>;
  rtf: Record<string, number | null>;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  seg_model_id?: number | null;
  asr_model_id?: number;
}

export interface AlignOp {
  op: "eq" | "sub" | "del" | "ins";
  ref: string | null;
  hyp: string | null;
  ri: number | null;
  hi: number | null;
  t0?: number;
  t1?: number;
}

export interface Utterance {
  id: number;
  key: string;
  file_id: number;
  segment_id: number | null;
  start: number;
  end: number;
  duration: number;
  speaker: string | null;
  ref: string;
  hyp: string;
  ops: AlignOp[];
  n: number;
  s: number;
  d: number;
  i: number;
  errors: number;
  wer: number | null;
  cer: number | null;
  flagged: boolean | null;
}

export interface UtteranceDetail extends Utterance {
  timed: boolean;
  segments: { start: number; end: number; text: string }[] | null;
  flag_note: string | null;
  label_url: string | null;
  others: { run_id: number; model: string; hyp: string; wer: number | null; ops: AlignOp[] }[];
}

export interface Page<T> {
  total: number;
  offset: number;
  rows: T[];
}

export type Lane = [number, number, string, string?];

export interface ErrorRegion {
  type: "missed" | "false_alarm" | "confusion";
  start: number;
  end: number;
  dur: number;
}

export interface Peaks {
  per_second: number;
  duration: number;
  min: number[];
  max: number[];
}

// ---------------------------------------------------------------- flagged references

/** A reference segment flagged as wrong from an error page (GET /datasets/{id}/flags). */
export interface FlaggedReference {
  id: number;
  uid: string;
  file_id: number;
  path: string;
  sha256: string;
  start: number;
  end: number;
  speaker: string | null;
  text: string | null;
  note: string | null;
  updated_at: string | null;
}
