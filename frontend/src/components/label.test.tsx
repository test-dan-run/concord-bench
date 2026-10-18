import { describe, expect, it } from "vitest";
import type { Segment } from "../lib/api";
import { confidenceColor, cycleSpeaker, labelButton, nextLowConfidence, overlapRegions, selectRange } from "../lib/label";
import { C } from "../lib/colors";

function seg(id: number, over: Partial<Segment> = {}): Segment {
  return {
    id, uid: `u${id}`, file_id: 1, start: id, end: id + 1, speaker: "spk0", text: "x", status: "pseudo", confidence: 0.6,
    source_run_id: 1, flagged: false, flag_note: null, reviewed: false, edited_at: null, updated_at: null, chosen: null,
    agreement: null, words: null, ...over,
  };
}

describe("selectRange", () => {
  const order = [1, 2, 3, 4, 5];
  it("toggles a single row", () => {
    expect([...selectRange(order, new Set(), 3, null, false)]).toEqual([3]);
    expect([...selectRange(order, new Set([3]), 3, 3, false)]).toEqual([]);
  });
  it("shift-click selects the range from the anchor, either direction", () => {
    expect([...selectRange(order, new Set([2]), 4, 2, true)].sort()).toEqual([2, 3, 4]);
    expect([...selectRange(order, new Set([4]), 1, 4, true)].sort()).toEqual([1, 2, 3, 4]);
  });
  it("shift-click from an unselected anchor clears the range", () => {
    expect([...selectRange(order, new Set([1, 2, 3, 4, 5]), 4, 2, true)].length).toBe(5);
    expect([...selectRange(order, new Set([1, 3, 4, 5]), 4, 2, true)].sort()).toEqual([1, 5]);
  });
});

describe("cycleSpeaker", () => {
  it("cycles and wraps", () => {
    expect(cycleSpeaker(["a", "b", "c"], "a")).toBe("b");
    expect(cycleSpeaker(["a", "b", "c"], "c")).toBe("a");
    expect(cycleSpeaker(["a", "b"], null)).toBe("a");
    expect(cycleSpeaker([], "z")).toBe("z");
  });
});

describe("overlapRegions", () => {
  it("finds where two speakers talk at once and ignores rejected segments", () => {
    const segs = [
      seg(1, { start: 0, end: 5, speaker: "a" }),
      seg(2, { start: 4, end: 8, speaker: "b" }),
      seg(3, { start: 6, end: 7, speaker: "a" }),
      seg(4, { start: 1, end: 2, speaker: "c", status: "rejected" }),
    ];
    expect(overlapRegions(segs)).toEqual([{ start: 4, end: 5 }, { start: 6, end: 7 }]);
  });
  it("a speaker overlapping itself is not an overlap", () => {
    expect(overlapRegions([seg(1, { start: 0, end: 3, speaker: "a" }), seg(2, { start: 2, end: 4, speaker: "a" })])).toEqual([]);
  });
});

describe("nextLowConfidence", () => {
  const segs = [seg(1, { confidence: 0.9, status: "auto" }), seg(2), seg(3, { reviewed: true }), seg(4, { confidence: null })];
  it("skips high-confidence and reviewed segments, wrapping around", () => {
    expect(nextLowConfidence(segs, null)).toBe(2);
    expect(nextLowConfidence(segs, 2)).toBe(4);
    expect(nextLowConfidence(segs, 4)).toBe(2);
  });
  it("returns null when nothing needs review", () => {
    expect(nextLowConfidence([seg(1, { status: "confirmed", reviewed: true })], null)).toBeNull();
  });
});

describe("labels and colours", () => {
  it("pseudo-label button reflects saved outputs", () => {
    expect(labelButton(4, 0)).toBe("Re-fuse 4 saved outputs");
    expect(labelButton(4, 1)).toBe("Run 1 new model, then fuse 5");
    expect(labelButton(0, 2)).toBe("Run 2 new models, then fuse 2");
  });
  it("confidence colours follow the thresholds", () => {
    expect(confidenceColor(0.9)).toBe(C.good);
    expect(confidenceColor(0.6)).toBe(C.warn);
    expect(confidenceColor(0.2)).toBe(C.bad);
    expect(confidenceColor(null)).toBe(C.subtle);
  });
});
