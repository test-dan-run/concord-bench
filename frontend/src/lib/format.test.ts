import { describe, expect, it } from "vitest";
import { coldStart, jobStatusText, modelStateLabel } from "./format";

describe("catalog formatting", () => {
  it("says a job waits for compute with its queue position", () => {
    expect(jobStatusText({ status: "waiting", progress: 0, lease: { id: "l-1", state: "queued", position: 2, waiting_for: "x", models: ["a"] } })).toBe(
      "waiting for compute #2",
    );
    expect(jobStatusText({ status: "waiting", progress: 0, lease: { id: "l-1", state: "starting", position: null, waiting_for: null, models: ["a"] } })).toBe(
      "starting models",
    );
    expect(jobStatusText({ status: "running", progress: 0.4, lease: null })).toBe("40%");
    expect(jobStatusText({ status: "blocked", progress: 0, lease: null })).toBe("blocked");
  });

  it("formats cold starts, marking estimates", () => {
    expect(coldStart(21)).toBe("21 s");
    expect(coldStart(180, false)).toBe("~3.0 min");
    expect(coldStart(null)).toBe("—");
    expect(modelStateLabel("failed")).toBe("failed to start");
    expect(modelStateLabel(null)).toBe("unknown");
  });
});
