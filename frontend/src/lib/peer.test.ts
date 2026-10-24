import { describe, expect, it } from "vitest";
import type { ReferenceImport } from "./api";
import { presetFor } from "../features/benchmarks/PlanPanel";
import { flagsPushBlocker, labelLinked, linksToLabel, skippedSummary, sortImports, sourceLine } from "./peer";

const source = { app: "concord-label", install_id: "3f2a", dataset_uid: "8d4e", dataset_name: "Help desk calls" };
const fromCatalog = { catalog: { dataset_id: "8d4e2b6a", n: 2, published: false, url: "http://c/datasets/8d4e2b6a/v/2" } } as never;
const handMade = { catalog: null } as never;
const cfg = (peer: { url: string | null; reachable: boolean; contract_version: number | null }) => ({ contract_version: 1, peer });

describe("Label links", () => {
  it("need a reachable Label with the same contract version", () => {
    expect(labelLinked(cfg({ url: "http://l:8610", reachable: true, contract_version: 1 }))).toBe(true);
    expect(labelLinked(cfg({ url: "http://l:8610", reachable: true, contract_version: 2 }))).toBe(false);
    expect(labelLinked(cfg({ url: "http://l:8610", reachable: false, contract_version: null }))).toBe(false);
    expect(labelLinked(cfg({ url: null, reachable: false, contract_version: null }))).toBe(false);
    expect(labelLinked(undefined)).toBe(false);
  });

  it("only for datasets that are Dataset Catalog versions", () => {
    expect(linksToLabel(fromCatalog)).toBe(true);
    expect(linksToLabel(handMade)).toBe(false);
    expect(linksToLabel(undefined)).toBe(false);
  });

  it("explain why flags cannot be pushed", () => {
    const up = cfg({ url: "http://l:8610", reachable: true, contract_version: 1 });
    expect(flagsPushBlocker(up, fromCatalog)).toBeNull();
    expect(flagsPushBlocker(up, handMade)).toMatch(/not a Dataset Catalog version/);
    expect(flagsPushBlocker(cfg({ url: null, reachable: false, contract_version: null }), fromCatalog)).toMatch(/LABEL_URL is not set/);
    expect(flagsPushBlocker(cfg({ url: "http://l:8610", reachable: false, contract_version: null }), fromCatalog)).toMatch(/not answering/);
    expect(flagsPushBlocker(cfg({ url: "http://l:8610", reachable: true, contract_version: 2 }), fromCatalog)).toMatch(/contract version 2/);
  });
});

describe("reference imports", () => {
  it("describe the source", () => {
    expect(sourceLine({ source, export_version: 2, has_manifest: true })).toBe("Concord Label · Help desk calls · version 2 · install 3f2a");
    expect(sourceLine({ source: null, export_version: null, has_manifest: true })).toMatch(/no source/);
    expect(sourceLine({ source: null, export_version: null, has_manifest: false })).toMatch(/No manifest/);
    const catalog = { ...source, install_id: "dataset-catalog", dataset_uid: "8d4e2b6a" };
    expect(sourceLine({ source: catalog, export_version: 3, has_manifest: true })).toBe("Dataset Catalog · Help desk calls · v3 · published by Concord Label");
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
