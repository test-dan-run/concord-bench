import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { AlignOp } from "../lib/api";
import { deltaPts, pct } from "../lib/format";
import { DiffText, diffTokens } from "./DiffText";
import { deltaInfo } from "./MetricTile";
import { clipSegments, lanesBySpeaker, niceTicks, peakColumns, tToX, viewAround, xToT } from "./timelineMath";

const OPS: AlignOp[] = [
  { op: "eq", ref: "the", hyp: "the", ri: 0, hi: 0 },
  { op: "sub", ref: "cat", hyp: "hat", ri: 1, hi: 1 },
  { op: "del", ref: "sat", hyp: null, ri: 2, hi: null },
  { op: "ins", ref: null, hyp: "down", ri: null, hi: 2 },
];

describe("diffTokens", () => {
  it("inline shows every edit with its marker", () => {
    expect(diffTokens(OPS, "inline").map((t) => t.text)).toEqual(["the", "cat → hat", "−sat", "+down"]);
  });
  it("ref side drops insertions, hyp side drops deletions", () => {
    expect(diffTokens(OPS, "ref").map((t) => `${t.kind}:${t.text}`)).toEqual(["eq:the", "sub:cat", "del:sat"]);
    expect(diffTokens(OPS, "hyp").map((t) => `${t.kind}:${t.text}`)).toEqual(["eq:the", "sub:hat", "ins:down"]);
  });
  it("renders with titles for screen readers and hover", () => {
    const { container } = render(<DiffText ops={OPS} />);
    expect(container.querySelectorAll('[title="substitution"]').length).toBe(1);
    expect(container.textContent).toContain("+down");
  });
  it("shows a placeholder for an empty alignment", () => {
    const { container } = render(<DiffText ops={[]} />);
    expect(container.textContent).toBe("(empty)");
  });
});

describe("timeline maths", () => {
  const v = { t0: 10, t1: 20, width: 500 };
  it("maps time to x and back", () => {
    expect(tToX(15, v)).toBe(250);
    expect(xToT(250, v)).toBe(15);
    expect(xToT(tToX(12.34, v), v)).toBeCloseTo(12.34);
  });
  it("clips segments to the view and drops ones outside", () => {
    const r = clipSegments([[5, 12], [14, 15], [19, 30], [25, 26]], v);
    expect(r.length).toBe(3);
    expect(r[0]).toMatchObject({ x: 0, w: 100 });
    expect(r[2]).toMatchObject({ x: 450, w: 50 });
  });
  it("gives tiny segments at least one pixel", () => {
    expect(clipSegments([[15, 15.0001]], v)[0].w).toBe(1);
  });
  it("groups turns into lanes by speaker in order of appearance", () => {
    const lanes = lanesBySpeaker([[3, 4, "B"], [0, 1, "A"], [5, 6, "A"]]);
    expect(lanes.map((l) => l.name)).toEqual(["A", "B"]);
    expect(lanes[0].segs).toEqual([[0, 1], [5, 6]]);
  });
  it("buckets peaks per pixel column", () => {
    const min = [-127, -10, 0, -64];
    const max = [127, 10, 0, 64];
    const cols = peakColumns(min, max, 2, { t0: 0, t1: 2, width: 2 });
    expect(cols).toEqual([{ lo: -1, hi: 1 }, { lo: -64 / 127, hi: 64 / 127 }]);
  });
  it("pads a view around a span and clamps to the file", () => {
    expect(viewAround(0.2, 3, 10)).toEqual({ t0: 0, t1: 3.5 });
    expect(viewAround(8, 9.9, 10)).toEqual({ t0: 7.5, t1: 10 });
  });
  it("picks readable tick steps", () => {
    expect(niceTicks(0, 10, 6)).toEqual([0, 2, 4, 6, 8, 10]);
    expect(niceTicks(0, 300, 6)[1]).toBe(60);
  });
});

describe("formatting and deltas", () => {
  it("formats percentages and signed points", () => {
    expect(pct(0.1234)).toBe("12.3%");
    expect(pct(null)).toBe("—");
    expect(deltaPts(0.012)).toBe("+1.2");
    expect(deltaPts(-0.004)).toBe("−0.4");
  });
  it("lower error is better", () => {
    expect(deltaInfo(0.1, 0.2)?.better).toBe(true);
    expect(deltaInfo(0.3, 0.2)?.better).toBe(false);
    expect(deltaInfo(0.9, 0.8, false)?.better).toBe(true);
    expect(deltaInfo(null, 0.2)).toBeNull();
  });
});

describe("planCount", () => {
  it("counts each audio version, the oracle once per enhancer", async () => {
    const { planCount, EMPTY_PLAN } = await import("../features/benchmarks/PlanPanel");
    const base = { ...EMPTY_PLAN, seg_models: ["a", "b"], asr_models: ["x", "y", "z"], oracle: true };
    expect(planCount(base, ["asr", "diar"])).toBe(9);
    expect(planCount({ ...base, enhancers: ["e1", "e2"], placements: ["before", "after"] }, ["asr", "diar"])).toBe(2 * 3 * 5 + 3 * 3);
    expect(planCount({ ...base, oracle: false, enhancers: ["e1"], include_original: false }, ["asr", "diar"])).toBe(6);
    expect(planCount({ ...base, mode: "stages", enhancers: ["e1"] }, ["asr", "diar"])).toBe(10);
  });
});
