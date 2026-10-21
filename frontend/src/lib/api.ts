// Thin fetch wrapper + the response shapes the pages use. The backend is the source of truth for
// field names (backend/concord/api/*.py).

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
  version: string;
  features: { improve: boolean };
  variant: "lite" | "full";
  tasks: TaskDef[];
  default_normaliser: string;
  default_collar: number;
  collars: number[];
  max_duration_s: number;
  replay: boolean;
  no_evict: boolean;
}

export interface Job {
  id: number;
  type: string;
  status: "queued" | "running" | "done" | "failed" | "blocked";
  progress: number;
  message: string | null;
  error: string | null;
  required_services: string[];
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
  mode: string;
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
  max_duration_s: number;
}

export interface CheckLine {
  name: string;
  status: "pass" | "warn" | "fail" | "info";
  reason: string;
}

export interface CheckReport {
  service: string;
  usable_as: string | null;
  capabilities: string[];
  timing_level: string | null;
  lines: CheckLine[];
  sample: { transcript?: string; wer?: number; der?: number; segments?: number[][]; speakers?: string[] };
  ok: boolean;
}

export interface RegistryEntry {
  name: string;
  alias: string | null;
  aliases: string[];
  contract: string;
  api: string;
  engine: string;
  kind: string;
  image: string;
  labels: Record<string, string>;
  signature?: { inputs: { name: string; datatype: string; shape: number[] }[]; outputs: { name: string; datatype: string; shape: number[] }[] };
  description: string;
  tasks: string[];
  gpu_fraction: number;
  host_port: number;
  base_url: string;
  url: string;
  usable_as: string | null;
  relevance: number;
  not_usable: string | null;
  model_id: number | null;
  display_name: string | null;
}

export interface Model {
  id: number;
  registry_name: string;
  display_name: string;
  tags: string[];
  parent_model_id: number | null;
  capabilities: string[];
  timing_level: string | null;
  check_report: CheckReport;
  registry: Partial<RegistryEntry>;
  created_at: string;
  stages: string[];
  runs: number;
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
  models: { id: number; display_name: string; registry_name: string; tags: string[]; capabilities: string[]; timing_level: string | null; kind: string }[];
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
  image_tags: Record<string, { image?: string; labels?: Record<string, string> }>;
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

// ---------------------------------------------------------------- Label (Phase 2)

export type SegStatus = "pseudo" | "auto" | "confirmed" | "rejected";
export type BarKey = "confirmed" | "auto" | "review" | "rejected" | "running" | "unlabelled";

export interface LabelProgress {
  bar: Record<BarKey, number>;
  seconds: number;
  segments: Record<"confirmed" | "auto" | "review" | "rejected", number>;
  files: { labelled: number; unlabelled: number };
  sources: string[];
  tasks: string[];
  flagged: number;
  running: boolean;
}

export interface NextAction {
  kind: "running" | "pseudo" | "review" | "live";
  label: string;
  status?: SegStatus;
}

export interface ConsoleRow {
  id: number;
  name: string;
  tags: string[];
  language: string | null;
  mode: string;
  label_types: string[];
  label_source: string;
  tasks: string[];
  hours: number | null;
  progress: LabelProgress;
  next: NextAction;
  label_run: { id: number; status: string } | null;
  created_at: string;
}

export interface PreviewMember {
  model_id: number;
  name: string;
  service: string;
  timing_level: string | null;
  cached_cuts: number;
  known_cuts: number;
  files_without_segments: number;
  state: "saved" | "partial" | "new";
}

export interface LabelPreview {
  files: number;
  seg: { model_id: number; name: string; service: string; cached_files: number; state: "saved" | "partial" | "new" };
  members: PreviewMember[];
  saved_members: number;
  new_members: number;
}

export interface Segment {
  id: number;
  uid: string;
  file_id: number;
  start: number;
  end: number;
  speaker: string | null;
  text: string | null;
  status: SegStatus;
  confidence: number | null;
  source_run_id: number | null;
  flagged: boolean;
  flag_note: string | null;
  reviewed: boolean;
  edited_at: string | null;
  updated_at: string | null;
  chosen: string | null;
  agreement: Record<string, number> | null;
  words: { w: string; start: number; end: number }[] | null;
  alternatives?: { service: string; model: string; text: string }[];
  file?: string;
}

export interface LabelSummary {
  segments: number;
  files: number;
  triage: Partial<Record<SegStatus, number>>;
  kept_reviewed: number;
  replaced: number;
  agreement: { members: string[]; cells: { ref: string; hyp: string; wer: number | null; n: number }[] };
  chosen: { member: string; n: number; share: number | null }[];
  strategy_applied: Record<string, number>;
  thresholds: { high: number; low: number };
}

export interface LabelResults {
  dataset: Dataset;
  run: Run | null;
  job: Job | null;
  summary: LabelSummary | null;
  model_names: Record<string, string>;
  segmenter: string | null;
  progress: LabelProgress;
  next: NextAction;
  queue: Segment[];
}

export interface LabelFile {
  id: number;
  path: string;
  stem: string;
  duration: number | null;
  status: string;
  usable: boolean;
  counts: { confirmed: number; auto: number; review: number; rejected: number; flagged: number };
}

export interface AnnotateView {
  file: { id: number; path: string; stem: string; duration: number; status: string; dataset_id: number; meta: Record<string, unknown> };
  dataset: { id: number; name: string; language: string | null };
  segments: Segment[];
  speakers: string[];
  keywords: unknown[];
  thresholds: { high: number; low: number };
  prev_file_id: number | null;
  next_file_id: number | null;
  position: { index: number; of: number };
}

export interface LiveModel {
  model_id: number;
  name: string;
  service: string;
  coverage: number;
  words: number;
  wer: number | null;
  ci: [number, number] | null;
  curve: { confirmed: number; wer: number | null }[];
  locked: { wer: number | null; dataset_id: number; run_id: number; normaliser: string; promoted: boolean } | null;
  rank: number | null;
}

export interface LiveEval {
  dataset: { id: number; name: string };
  asr: { normaliser: string; confirmed: number; min_rank: number; ranked: boolean; models: LiveModel[] };
  der: { files: number; of: number; collar: number; models: { model_id: number; name: string; der: number | null; jer: number | null; files: number }[] };
  kws: { available: boolean };
  feed: { id: number; file_id: number; file: string; start: number; end: number; speaker: string | null; text: string | null; confirmed_at: string }[];
}

export interface SyncPackage {
  id: number;
  direction: "export" | "import";
  status: "built" | "staged" | "applied" | "undone";
  dataset_id: number | null;
  dataset_name: string | null;
  scope: Record<string, unknown>;
  counts: Record<string, unknown> & { files?: number; segments?: number; by_status?: Record<string, number>; outcome?: Record<string, number> };
  include: { pseudo_labels: boolean; model_outputs: boolean; audio: boolean } | null;
  source_install: string | null;
  created_at: string;
  applied_at: string | null;
  undone_at: string | null;
  conflicts_open: number;
  conflicts: number;
  file: string | null;
}

export interface PkgSegment {
  uid?: string;
  file_sha256: string;
  start: number;
  end: number;
  speaker: string | null;
  text: string | null;
  status: string;
  confidence: number | null;
  source: string;
  reviewed?: boolean;
  updated_at: string | null;
}

export interface Conflict {
  key: string;
  file_sha256: string;
  local: PkgSegment & { id: number };
  incoming: PkgSegment;
  resolution: "incoming" | "local" | null;
}

export interface ImportStaged extends SyncPackage {
  manifest: Record<string, unknown> & { dataset?: { name: string; language: string | null; tags: string[] }; created_at?: string; install_id?: string };
  suggestions?: { dataset_id: number; name: string; matched_files: number; package_files: number; same_name: boolean }[];
  conflict_list?: Conflict[];
  rules?: { outcome: string; rule: string }[];
}

export interface ImportPlan {
  target: { id: number; name: string } | null;
  files: { path: string; sha256: string; duration: number | null; sample_rate: number | null; outcome: string }[];
  file_counts: Record<string, number>;
  counts: Record<string, number>;
  rows: { outcome: string; reason?: string; incoming: PkgSegment; local?: PkgSegment & { id: number } }[];
  truncated: boolean;
  conflicts: Conflict[];
  rules: { outcome: string; rule: string }[];
}

// ---------------------------------------------------------------- Export for offline

export interface OfflineService {
  name: string;
  alias: string | null;
  kind: string;
  engine: string;
  contract: string;
  api: string;
  tasks: string[];
  description: string | null;
  port: number;
  gpu: boolean;
  gpu_fraction: number;
  image: string;
  image_present: boolean | null;
  image_id: string | null;
  image_bytes: number | null;
  image_problem: string | null;
  weights: { kind: string; path: string; files: number; bytes: number; revision: string | null; problems: string[] } | null;
  weights_bytes: number;
  problems: string[];
  model_id?: number | null;
  display_name?: string | null;
}

export interface OfflineServices {
  services: OfflineService[];
  platform: { gpu_total_gb: number; util_limit: number; network: string; compose_project: string };
  warnings: string[];
  concord_images: { image: string; present: boolean; bytes: number | null }[];
  default_image: string;
  default_dest: string;
}

export interface OfflinePreview {
  kind: "full" | "models";
  services: OfflineService[];
  budget: { total_fraction: number; util_limit: number; ok: boolean };
  images: { refs: string[]; bytes: number };
  weights_bytes: number;
  concord: { image: string; present: boolean; id: string | null; bytes: number | null } | null;
  bytes: number;
  free_bytes: number | null;
  dest: string;
  kit: string | null;
  problems: string[];
  warnings: string[];
}

export interface OfflineExport {
  id: number;
  kind: "full" | "models";
  services: string[];
  concord_image: string | null;
  dest: string;
  name: string | null;
  status: "queued" | "running" | "done" | "failed";
  path: string | null;
  bytes: number | null;
  error: string | null;
  job_id: number | null;
  job: Job | null;
  created_at: string;
  finished_at: string | null;
}
