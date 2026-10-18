// AeroAudio colours used from code (canvas, SVG). CSS uses the Tailwind theme tokens instead.

export const C = {
  canvas: "#0A0F1D",
  panel: "#111827",
  track: "#1E293B",
  active: "#283548",
  line: "#334155",
  fg: "#F8FAFC",
  muted: "#94A3B8",
  subtle: "#64748B",
  focus: "#38BDF8",
  primary: "#06B6D4",
  secondary: "#6366F1",
  good: "#10B981",
  warn: "#F59E0B",
  bad: "#F43F5E",
  confusion: "#A855F7",
  overlap: "#DC143C",
};

export const SPEAKERS = ["#14B8A6", "#F97316", "#A855F7", "#FACC15", "#38BDF8", "#EC4899", "#84CC16", "#8B5CF6"];

export const ERROR_COLORS: Record<string, string> = { missed: C.bad, false_alarm: C.warn, confusion: C.confusion };
export const ERROR_LABELS: Record<string, string> = {
  missed: "Missed speech",
  false_alarm: "False alarm",
  confusion: "Speaker confusion",
};

export const STAGE_COLORS: Record<string, string> = { asr: "#38BDF8", segmentation: "#10B981", speaker: "#A855F7" };
export const STAGE_LABELS: Record<string, string> = {
  asr: "ASR",
  segmentation: "Segmentation",
  speaker: "Speaker attribution",
};

/** Stable colour per speaker name within one view: index in the sorted name list. */
export function speakerColors(names: string[]): Record<string, string> {
  const sorted = [...new Set(names)].sort();
  return Object.fromEntries(sorted.map((n, i) => [n, SPEAKERS[i % SPEAKERS.length]]));
}
