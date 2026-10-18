// Pure timeline maths: time <-> x, clipping segments to a view, lane packing, peak bucketing.

export interface View {
  t0: number;
  t1: number;
  width: number;
}

export function tToX(t: number, v: View): number {
  return ((t - v.t0) / Math.max(1e-9, v.t1 - v.t0)) * v.width;
}

export function xToT(x: number, v: View): number {
  return v.t0 + (x / Math.max(1e-9, v.width)) * (v.t1 - v.t0);
}

/** Segments clipped to the view, as pixel rects; segments entirely outside are dropped. */
export function clipSegments(segs: [number, number][], v: View): { x: number; w: number; s: number; e: number }[] {
  const out = [];
  for (const [s, e] of segs) {
    if (e <= v.t0 || s >= v.t1) continue;
    const a = Math.max(s, v.t0);
    const b = Math.min(e, v.t1);
    const x = tToX(a, v);
    out.push({ x, w: Math.max(1, tToX(b, v) - x), s, e });
  }
  return out;
}

/** Group [start, end, speaker] turns into one lane per speaker, in first-appearance order. */
export function lanesBySpeaker(turns: [number, number, string, ...unknown[]][]): { name: string; segs: [number, number][] }[] {
  const order: string[] = [];
  const by = new Map<string, [number, number][]>();
  for (const [s, e, spk] of [...turns].sort((a, b) => a[0] - b[0])) {
    if (!by.has(spk)) {
      by.set(spk, []);
      order.push(spk);
    }
    by.get(spk)!.push([s, e]);
  }
  return order.map((name) => ({ name, segs: by.get(name)! }));
}

/** Min/max peaks for each pixel column of the view. Peaks are int8-scaled (-127..127). */
export function peakColumns(min: number[], max: number[], perSecond: number, v: View): { lo: number; hi: number }[] {
  const cols = Math.max(1, Math.floor(v.width));
  const out = [];
  for (let c = 0; c < cols; c++) {
    const a = Math.floor(xToT(c, v) * perSecond);
    const b = Math.max(a + 1, Math.floor(xToT(c + 1, v) * perSecond));
    let lo = 0;
    let hi = 0;
    for (let k = Math.max(0, a); k < Math.min(min.length, b); k++) {
      if (min[k] < lo) lo = min[k];
      if (max[k] > hi) hi = max[k];
    }
    out.push({ lo: lo / 127, hi: hi / 127 });
  }
  return out;
}

/** A view of `span` seconds around [s, e], clamped to the file. */
export function viewAround(s: number, e: number, duration: number, pad = 0.5): { t0: number; t1: number } {
  const t0 = Math.max(0, s - pad);
  const t1 = Math.min(duration || e + pad, e + pad);
  return { t0, t1: Math.max(t1, t0 + 0.1) };
}

export function niceTicks(t0: number, t1: number, target = 8): number[] {
  const span = t1 - t0;
  const steps = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];
  const step = steps.find((s) => span / s <= target) ?? 600;
  const out = [];
  for (let t = Math.ceil(t0 / step) * step; t <= t1 + 1e-9; t += step) out.push(Math.round(t * 1000) / 1000);
  return out;
}
