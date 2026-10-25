// Number and time formatting. Every value rendered with these goes inside a `.num` element.

import type { Job } from "./api";

export const DASH = "—";

export function pct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return DASH;
  return `${(v * 100).toFixed(digits)}%`;
}

/** Signed percentage-point delta, e.g. "+1.2" / "−0.4". */
export function deltaPts(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "";
  const x = v * 100;
  if (Math.abs(x) < 0.05) return "±0.0";
  return `${x > 0 ? "+" : "−"}${Math.abs(x).toFixed(digits)}`;
}

/** Colour class for a change in a lower-is-better rate: good when it fell, bad when it rose, muted when it
 * rounds to ±0.0 (as deltaPts prints it) or is missing. */
export function deltaTone(v: number | null | undefined, lowerIsBetter = true): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "text-subtle";
  if (Math.abs(v * 100) < 0.05) return "text-muted";
  return (v < 0) === lowerIsBetter ? "text-good" : "text-bad";
}

export function num(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return DASH;
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function secs(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined) return DASH;
  return `${v.toFixed(digits)} s`;
}

/** mm:ss.mmm */
export function clock(t: number | null | undefined, ms = true): string {
  if (t === null || t === undefined || Number.isNaN(t)) return DASH;
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  const ss = ms ? s.toFixed(3).padStart(6, "0") : Math.floor(s).toString().padStart(2, "0");
  return `${m.toString().padStart(2, "0")}:${ss}`;
}

export function hours(h: number | null | undefined): string {
  if (h === null || h === undefined) return DASH;
  if (h < 1) return `${(h * 60).toFixed(h * 60 < 10 ? 1 : 0)} min`;
  return `${h.toFixed(1)} h`;
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const t = new Date(iso.endsWith("Z") ? iso : `${iso}Z`).getTime();
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function rtf(v: number | null | undefined): string {
  if (v === null || v === undefined) return DASH;
  return v < 0.01 ? v.toFixed(4) : v.toFixed(3);
}

export function capLabel(c: string): string {
  return { asr: "ASR", vad: "VAD", diar: "Diarisation", kws: "KWS", enhance: "Enhancement" }[c] ?? c;
}

/** "4 min 17 s", "38 s", "1 h 02 min". */
export function duration(s: number | null | undefined): string {
  if (s === null || s === undefined || Number.isNaN(s)) return DASH;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${String(Math.round(s % 60)).padStart(2, "0")} s`;
  return `${Math.floor(s / 3600)} h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")} min`;
}

/** "×12 real time" from an RTF. */
export function speed(r: number | null | undefined): string {
  if (!r) return DASH;
  const x = 1 / r;
  return `${x >= 10 ? Math.round(x) : x.toFixed(1)}× real time`;
}

type Hw = { device?: string; gpu_name?: string; precision?: string; batch_size?: number; hosted_at?: string; vram_gb?: number; cpu_model?: string };

/** A short label for where a model ran: "GPU · RTX 5090 · fp16 · bs 16", "CPU", "hosted · api.example.com". */
export function hwLabel(h: Hw | null | undefined): string {
  if (!h || !h.device) return DASH;
  if (h.device === "hosted") return h.hosted_at ? `hosted · ${h.hosted_at}` : "hosted";
  const parts = [h.device === "gpu" ? (h.gpu_name ? h.gpu_name.replace(/^NVIDIA (GeForce )?/, "") : "GPU") : "CPU"];
  if (h.precision) parts.push(h.precision);
  if (h.batch_size) parts.push(`bs ${h.batch_size}`);
  return parts.join(" · ");
}

/** An ASR metric's name, formula and direction (EVALUATE_PLAN.md §3.1). */
export const ASR_METRIC_INFO: Record<string, { label: string; name: string; formula: string; text: string; low: boolean }> = {
  wer: { label: "WER", name: "Word error rate", formula: "(S + D + I) / N", text: "Can exceed 100% when a model inserts many words. Lower is better.", low: true },
  cer: { label: "CER", name: "Character error rate", formula: "character edits / reference characters", text: "Kinder to spelling variants and names. Lower is better.", low: true },
  mer: { label: "MER", name: "Match error rate", formula: "(S + D + I) / (H + S + D + I)", text: "Share of aligned word pairs that are wrong, always 0 to 100%. Lower is better.", low: true },
  wil: { label: "WIL", name: "Word information lost", formula: "1 − WIP", text: "Penalises errors against both the reference and the hypothesis length. Lower is better.", low: true },
  wip: { label: "WIP", name: "Word information preserved", formula: "(H / N) × (H / P)", text: "H hits, N reference words, P hypothesis words. Higher is better.", low: false },
};

export function timingLabel(t: string | null | undefined): string {
  return { word: "word times", segment: "segment times", text: "text only" }[t ?? ""] ?? DASH;
}

/** "74 s" / "3.1 min"; `~` when the catalog has not measured a load of this build yet. */
export function coldStart(s: number | null | undefined, measured = true): string {
  if (s === null || s === undefined) return DASH;
  const v = s < 90 ? `${Math.round(s)} s` : `${(s / 60).toFixed(1)} min`;
  return measured ? v : `~${v}`;
}

export function modelStateLabel(state: string | null | undefined): string {
  return { running: "running", stopped: "stopped", starting: "starting", stopping: "stopping", queued: "queued", failed: "failed to start" }[state ?? ""] ?? "unknown";
}

/** "running 40%", or "waiting for compute #2" while the job's lease is queued in the model catalog. */
export function jobStatusText(j: Pick<Job, "status" | "progress" | "lease" | "stopping">): string {
  if (j.stopping) return "stopping";
  if (j.status === "running") return `${Math.round(j.progress * 100)}%`;
  if (j.status === "waiting") {
    if (j.lease?.state === "starting") return "starting models";
    return `waiting for compute${j.lease?.position ? ` #${j.lease.position}` : ""}`;
  }
  return j.status;
}
