import { describe, expect, it } from "vitest";
import type { ReferenceImport } from "./api";
import { presetFor } from "../features/benchmarks/PlanPanel";
import { flagsPushBlocker, fromLabel, labelLinked, skippedSummary, sortImports, sourceLine } from "./peer";

const source = { app: "concord-label", install_id: "3f2a", dataset_uid: "8d4e", dataset_name: "Help desk calls" };
const fromLabelDs = { provenance: { import_id: 1, package_name: "x", source } } as never;
const handMade = { provenance: { import_id: 2, package_name: "y", source: null } } as never;
const cfg = (peer: { url: string | null; reachable: boolean; contract_version: number | null }) => ({ contract_version: 1, peer });

describe("Label links", () => {
  it("need a reachable Label with the same contract version", () => {
    expect(labelLinked(cfg({ url: "http://l:8610", reachable: true, contract_version: 1 }))).toBe(true);
    expect(labelLinked(cfg({ url: "http://l:8610", reachable: true, contract_version: 2 }))).toBe(false);
    expect(labelLinked(cfg({ url: "http://l:8610", reachable: false, contract_version: null }))).toBe(false);
    expect(labelLinked(cfg({ url: null, reachable: false, contract_version: null }))).toBe(false);
    expect(labelLinked(undefined)).toBe(false);
  });

  it("only for datasets whose references came from Label", () => {
    expect(fromLabel(fromLabelDs)).toBe(true);
    expect(fromLabel(handMade)).toBe(false);
    expect(fromLabel({ provenance: {} })).toBe(false);
    expect(fromLabel({ provenance: { import_id: 3, package_name: "z", source: { ...source, app: "other-tool" } } } as never)).toBe(false);
  });

  it("explain why flags cannot be pushed", () => {
    const up = cfg({ url: "http://l:8610", reachable: true, contract_version: 1 });
    expect(flagsPushBlocker(up, fromLabelDs)).toBeNull();
    expect(flagsPushBlocker(up, handMade)).toMatch(/did not come from Concord Label/);
    expect(flagsPushBlocker(cfg({ url: null, reachable: false, contract_version: null }), fromLabelDs)).toMatch(/LABEL_URL is not set/);
    expect(flagsPushBlocker(cfg({ url: "http://l:8610", reachable: false, contract_version: null }), fromLabelDs)).toMatch(/not answering/);
    expect(flagsPushBlocker(cfg({ url: "http://l:8610", reachable: true, contract_version: 2 }), fromLabelDs)).toMatch(/contract version 2/);
  });
});

describe("reference imports", () => {
  it("describe the source", () => {
    expect(sourceLine({ source, export_version: 2, has_manifest: true })).toBe("Concord Label · Help desk calls · version 2 · install 3f2a");
    expect(sourceLine({ source: null, export_version: null, has_manifest: true })).toMatch(/no source/);
    expect(sourceLine({ source: null, export_version: null, has_manifest: false })).toMatch(/No manifest/);
  });

  it("list waiting imports first, oldest first, then decided ones newest first", () => {
    const mk = (id: number, status: ReferenceImport["status"]) => ({ id, status }) as ReferenceImport;
    const { waiting, decided } = sortImports([mk(5, "accepted"), mk(4, "staged"), mk(3, "rejected"), mk(2, "staged"), mk(6, "accepting")]);
    expect(waiting.map((i) => i.id)).toEqual([2, 4, 6]);
    expect(decided.map((i) => i.id)).toEqual([5, 3]);
  });

  it("pre-tick the rerun plan with the previous version's models the catalog still offers verified", () => {
    const m = (catalog_id: string, usable = true) => ({ catalog_id, usable }) as never;
    const plan = presetFor(
      { seg_models: ["pyannote", "gone"], asr_models: ["whisper", "fw", "stale"], oracle: true, mode: "grid" },
      { seg: [m("pyannote")], asr: [m("whisper"), m("fw"), m("other"), m("stale", false)], oracle_available: false },
    );
    expect(plan).toEqual({ seg_models: ["pyannote"], asr_models: ["whisper", "fw"], oracle: false, mode: "grid", enhancers: [], include_original: true, placements: ["before"] });
  });

  it("summarise skipped outputs", () => {
    expect(skippedSummary({ build_differs: 2, unknown_model: 0, params_differ: 1 })).toBe("2 build differs, 1 params differ");
    expect(skippedSummary(undefined)).toBe("");
  });
});
