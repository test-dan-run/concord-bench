import { describe, expect, it } from "vitest";
import type { Inspection } from "../../lib/api";
import { hasReferences, labelTypesOf } from "./UploadDataset";

function ins(over: Partial<Inspection>): Inspection {
  return {
    audio_files: 2, audio_bytes: 1000, annotation_files: 1, ignored: [], tasks: {}, unmatched: [], audio_without_labels: [],
    duplicate_stems: [], parse_errors: [], files_by_format: [], ...over,
  };
}

const match = { records: 2, files: 2, formats: ["stm"], preview: [] };

describe("upload needs references (Concord Bench)", () => {
  it("accepts audio with matched annotations", () => {
    const i = ins({ tasks: { spk_asr: match }, audio_without_labels: ["b.wav"] });
    expect(hasReferences(i)).toBe(true);
    expect(labelTypesOf(i)).toEqual(["asr", "diar"]);
  });
  it("refuses audio alone, annotations matching no audio, and nothing at all", () => {
    expect(hasReferences(ins({ annotation_files: 0, audio_without_labels: ["a.wav", "b.wav"] }))).toBe(false);
    expect(hasReferences(ins({ tasks: { asr: match }, audio_without_labels: ["a.wav", "b.wav"] }))).toBe(false);
    expect(hasReferences(ins({ audio_files: 0, tasks: { asr: match } }))).toBe(false);
    expect(hasReferences(null)).toBe(false);
  });
});
