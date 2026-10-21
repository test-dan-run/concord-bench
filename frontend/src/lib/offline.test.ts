import { describe, expect, it } from "vitest";
import type { OfflineAppChoice, OfflineService } from "./api";
import { appImageBytes, appsQuery, initialAppPick, pickedApps, selectionSummary, type AppPick } from "./offline";

const THIS: string = "concord-bench"; // the app this repository builds

function row(name: string, image: string, image_bytes: number | null, weights_bytes: number, gpu_fraction = 0): OfflineService {
  return {
    name, alias: null, kind: "model", engine: "vllm", contract: "openai", api: "audio.transcriptions", tasks: [], description: null,
    port: 8100, gpu: gpu_fraction > 0, gpu_fraction, image, image_present: image_bytes !== null, image_id: null, image_bytes,
    image_problem: null, weights: null, weights_bytes, problems: [],
  };
}

describe("selectionSummary", () => {
  const rows = [
    row("whisper-large-v3", "vllm/vllm-openai:v0.31.0", 32e9, 3e9, 0.15),
    row("whisper-large-v3-turbo", "vllm/vllm-openai:v0.31.0", 32e9, 1.6e9, 0.1),
    row("silero-vad", "ghcr.io/owner/silero-vad:0.1.0", 0.4e9, 2e6),
    row("kokoro-tts", "ghcr.io/owner/kokoro-tts:0.1.0", null, 3e8, 0.09),
  ];
  it("counts a shared engine image once", () => {
    const s = selectionSummary(rows, new Set(["whisper-large-v3", "whisper-large-v3-turbo"]));
    expect(s.imageBytes).toBe(32e9);
    expect(s.weightsBytes).toBeCloseTo(4.6e9);
    expect(s.gpuFraction).toBeCloseTo(0.25);
  });
  it("reports images of unknown size", () => {
    const s = selectionSummary(rows, new Set(["silero-vad", "kokoro-tts"]));
    expect(s).toMatchObject({ services: 2, imageBytes: 0.4e9, unknownImages: 1 });
  });
  it("is empty for no selection", () => {
    expect(selectionSummary(rows, new Set())).toEqual({ services: 0, imageBytes: 0, weightsBytes: 0, gpuFraction: 0, unknownImages: 0 });
  });
});

describe("apps in a full kit", () => {
  const choices: OfflineAppChoice[] = [
    { app: "concord-bench", title: "Concord Bench", port: 8600, own: THIS === "concord-bench", default_image: "concord-bench:0.2.0",
      images: [{ image: "concord-bench:0.2.0", present: true, bytes: 1.8e9 }] },
    { app: "concord-label", title: "Concord Label", port: 8620, own: THIS === "concord-label", default_image: "concord-label:0.2.0",
      images: [{ image: "concord-label:0.1.0", present: true, bytes: 1.7e9 }, { image: "concord-label:0.2.0", present: true, bytes: 1.9e9 }] },
  ];
  it("ticks this app only by default", () => {
    const pick = initialAppPick(choices);
    expect(pickedApps(choices, pick)).toEqual({ [THIS]: `${THIS}:0.2.0` });
    expect(appImageBytes(choices, pick)).toBe(THIS === "concord-bench" ? 1.8e9 : 1.9e9);
  });
  it("carries both apps with the picked tags and counts their images", () => {
    const pick: AppPick = { "concord-bench": { on: true, image: "concord-bench:0.2.0" }, "concord-label": { on: true, image: "concord-label:0.1.0" } };
    const apps = pickedApps(choices, pick);
    expect(appsQuery(apps)).toBe("concord-bench=concord-bench:0.2.0,concord-label=concord-label:0.1.0");
    expect(appImageBytes(choices, pick)).toBeCloseTo(3.5e9);
  });
  it("leaves out an app with no image and writes an empty query for none", () => {
    const missing = [{ ...choices[1], images: [], default_image: null }];
    expect(initialAppPick(missing)["concord-label"]).toEqual({ on: THIS === "concord-label", image: null });
    expect(appsQuery({ "concord-label": null })).toBe("concord-label");
    expect(appsQuery({})).toBe("");
  });
});
