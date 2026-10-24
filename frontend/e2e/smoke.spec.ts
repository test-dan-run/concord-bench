import { expect, test, type Page } from "@playwright/test";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

// contracts/ of this repository (mounted at /contracts by `just e2e`).
const CONTRACTS = process.env.E2E_CONTRACTS ?? fileURLToPath(new URL("../../contracts", import.meta.url));

// Acceptance item 3: every error-analysis view loads, sorts, expands and plays audio on the fixture
// set; plus a load check of every other Concord Bench screen. Console errors fail the test.

function watchErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && !m.text().includes("favicon")) errors.push(m.text());
  });
  return errors;
}

async function expectPlays(page: Page) {
  const audio = page.locator("audio").first();
  await expect.poll(async () => audio.evaluate((a: HTMLAudioElement) => a.currentTime), { timeout: 15_000 }).toBeGreaterThan(0.05);
}

test("benchmarks: ASR, diarisation and pipeline tabs show scores", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/benchmarks/asr");
  await expect(page.getByRole("heading", { name: "Benchmarks" })).toBeVisible();
  const cell = page.getByRole("button", { name: /faster-whisper-large-v3 on Synth fixtures: WER/ });
  await expect(cell).toBeVisible();
  await cell.click();
  await expect(page.getByText("Substitutions").first()).toBeVisible();
  await page.getByRole("tab", { name: "Diarisation", exact: true }).click();
  await expect(page.getByRole("button", { name: /pyannote-diarization on Synth fixtures: DER/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /silero-vad on Synth fixtures: VAD error/ })).toBeVisible();
  await page.getByRole("tab", { name: "ASR + diarisation pipelines" }).click();
  await expect(page.getByRole("heading", { name: /Ablation grid/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Reference segments \+ speakers → whisper-large-v3: \d/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /silero-vad → whisper-large-v3: n\/a/ })).toBeVisible();
  // The panel opens on the best run (the oracle, which has no attribution); pick a real diariser.
  await page.getByRole("button", { name: /nemotron-3-diarization → whisper-large-v3: \d/ }).click();
  await expect(page.getByText("Where the errors come from")).toBeVisible();
  expect(errors).toEqual([]);
});

test("the text normaliser stays the same across Benchmarks and Error analysis", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/benchmarks/asr");
  await page.getByLabel("Text normaliser").selectOption("whisper_en");
  await expect(page.getByText(/normaliser whisper_en@1/)).toBeVisible();
  // nav links carry no query string: the choice still holds
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Error analysis" }).click();
  await expect(page).toHaveURL(/\/errors\/asr\/\d+/);
  await expect(page.getByLabel("Normaliser")).toHaveValue("whisper_en");
  await page.getByLabel("Normaliser").selectOption("none");
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Benchmarks" }).click();
  await expect(page.getByLabel("Text normaliser")).toHaveValue("none");
  await page.getByRole("tab", { name: "ASR + diarisation pipelines" }).click();
  await expect(page.getByLabel("Text normaliser")).toHaveValue("none");
  // ...and survives a reload (each test starts with fresh browser storage)
  await page.goto("/benchmarks/asr");
  await expect(page.getByLabel("Text normaliser")).toHaveValue("none");
  expect(errors).toEqual([]);
});

test("ASR errors: loads, sorts, expands and plays", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/errors");
  await expect(page).toHaveURL(/\/errors\/asr\/\d+/);
  await expect(page.getByRole("heading", { name: "Utterances" })).toBeVisible();
  const rows = page.locator("table[class] tbody tr");
  await expect(rows.first()).toBeVisible();
  const werHeader = page.getByRole("columnheader", { name: /WER/ });
  await expect(werHeader).toHaveAttribute("aria-sort", "descending");
  await werHeader.getByRole("button").click();
  await expect(werHeader).toHaveAttribute("aria-sort", "ascending");
  await page.getByRole("columnheader", { name: /^ID/ }).getByRole("button").click();
  await expect(page.getByRole("columnheader", { name: /^ID/ })).toHaveAttribute("aria-sort", /descending|ascending/);
  await page.locator("tbody tr").first().click();
  await expect(page.getByText("Word alignment")).toBeVisible();
  await page.getByRole("button", { name: "Play utterance" }).click();
  await expectPlays(page);
  await expect(page.getByText("Most mis-transcribed words")).toBeVisible();
  expect(errors).toEqual([]);
});

test("VAD / diarisation errors: loads, sorts, expands and plays", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/errors");
  await page.getByRole("link", { name: "VAD / diarisation" }).click();
  await expect(page).toHaveURL(/\/errors\/diar\/\d+/);
  await expect(page.getByRole("heading", { name: "Files" })).toBeVisible();
  await page.getByRole("columnheader", { name: /Duration/ }).getByRole("button").click();
  await expect(page.getByRole("columnheader", { name: /Duration/ })).toHaveAttribute("aria-sort", /descending|ascending/);
  await page.locator("tbody tr").first().click();
  await expect(page.getByRole("img", { name: "Speaker and error lanes" })).toBeVisible();
  await page.getByRole("button", { name: "Play file" }).click();
  await expectPlays(page);
  await expect(page.getByText("Boundary accuracy")).toBeVisible();
  expect(errors).toEqual([]);
});

test("pipeline errors: loads, sorts, expands and plays", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/benchmarks/pipeline");
  await page.getByRole("link", { name: /Open pipeline error analysis/ }).click();
  await expect(page).toHaveURL(/\/errors\/pipeline\/\d+/);
  await expect(page.getByRole("heading", { name: "Files" })).toBeVisible();
  await page.getByRole("columnheader", { name: /DER/ }).getByRole("button").click();
  await page.locator("tbody tr").first().click();
  await expect(page.getByText("Pipeline output")).toBeVisible();
  await page.getByRole("button", { name: "Play file" }).click();
  await expectPlays(page);
  expect(errors).toEqual([]);
});

test("bring-your-own screens load; Bench has no Label or Training pages", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/datasets");
  await expect(page.getByRole("link", { name: "Synth fixtures" })).toBeVisible();
  await page.getByRole("link", { name: "Synth fixtures" }).click();
  await expect(page.getByText("Flagged files")).toBeVisible();
  await expect(page.getByText(/Undecodable/)).toBeVisible();
  await expect(page.getByText(/Over the length cap/)).toHaveCount(0); // no duration cap
  await expect(page.getByText("Flagged references")).toBeVisible();
  await expect(page.getByRole("link", { name: /Pseudo-label|Open in annotator/ })).toHaveCount(0);
  await page.goto("/datasets/upload");
  await expect(page.getByRole("heading", { name: "Upload dataset" })).toBeVisible();
  await expect(page.getByRole("link", { name: "asr.jsonl" })).toBeVisible();
  await expect(page.getByRole("radio", { name: /Audio only/ })).toHaveCount(0);
  // Models come from the model catalog (the replay stand-in here): read-only, no registration screens.
  await page.goto("/models");
  await expect(page.getByText("faster-whisper-large-v3 · openai / audio.transcriptions")).toBeVisible();
  await expect(page.getByRole("link", { name: "Open the model catalog" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Re-check|Remove/ })).toHaveCount(0);
  const primary = page.getByRole("navigation", { name: "Primary" });
  await expect(primary.getByText("Add model")).toHaveCount(0);
  await expect(primary.getByText("Model contract")).toHaveCount(0);
  await page.goto("/models/add");
  await expect(page).toHaveURL(/\/benchmarks$/); // removed routes fall back to the benchmarks
  await page.goto("/jobs");
  await expect(page.getByRole("heading", { name: "Jobs" })).toBeVisible();
  const nav = page.getByRole("navigation", { name: "Primary" });
  await expect(nav.getByText("Concord Bench")).toBeVisible();
  for (const name of ["Benchmarks", "Error analysis", "Datasets", "Import references", "Models", "Export for offline"]) {
    await expect(nav.getByRole("link", { name, exact: true })).toBeVisible();
  }
  for (const name of ["Training", "Review console", "Pseudo-label", "Annotate", "Live evaluation", "Import / export"]) {
    await expect(nav.getByRole("link", { name })).toHaveCount(0);
  }
  for (const path of ["/training", "/label", "/label/annotate"]) {
    await page.goto(path);
    await expect(page).toHaveURL(/\/benchmarks/);
  }
  await expect(page).toHaveTitle("Concord Bench");
  expect(errors).toEqual([]);
});

test("a reference flagged on the ASR error page is listed on the dataset page", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/errors");
  await expect(page).toHaveURL(/\/errors\/asr\/\d+/);
  await page.locator("tbody tr").first().click();
  await expect(page.getByText(/Open in annotator/)).toHaveCount(0);
  await page.getByRole("button", { name: "Flag reference as wrong" }).click();
  await expect(page.getByRole("button", { name: /Reference flagged/ })).toBeVisible();
  await expect(page.getByText(/listed on the dataset page/)).toBeVisible();
  await page.goto("/datasets");
  await page.getByRole("link", { name: "Synth fixtures" }).click();
  await expect(page.getByText("Flagged references")).toBeVisible();
  await expect(page.getByRole("button", { name: "Unflag" })).toHaveCount(1);
  await page.getByRole("button", { name: "Unflag" }).click();
  await expect(page.getByText("No references flagged.")).toBeVisible();
  expect(errors).toEqual([]);
});

test("the sample reference package imports through the UI and is accepted", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/datasets");
  await page.getByRole("link", { name: "Import reference package" }).first().click();
  await expect(page.getByRole("heading", { name: "Import reference package" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Template package/ })).toHaveAttribute("href", "/api/templates/reference_package.zip");
  await page.getByLabel("Package folder").setInputFiles(join(CONTRACTS, "samples", "reference-package-complete"));
  const staged = page.getByRole("article", { name: "Staged package Help desk calls" });
  await expect(staged).toBeVisible();
  await expect(staged.getByText("Concord Label · Help desk calls · version 1 · install 3f2a9c1e5b7d4a60")).toBeVisible();
  await expect(staged.getByText("refs/segments.seglst.json")).toBeVisible();
  await expect(staged.getByLabel("Dataset name")).toHaveValue("Help desk calls");
  await staged.getByRole("button", { name: "Accept" }).click();
  await expect(page).toHaveURL(/\/datasets\/\d+$/);
  await expect(page.getByRole("heading", { name: "Where the references came from" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("Concord Label", { exact: true })).toBeVisible();
  await expect(page.getByText("v1", { exact: true })).toBeVisible();
  await expect(page.getByRole("cell", { name: "check_clip.wav" })).toBeVisible();
  // Accepted: listed in the history, nothing left waiting
  await page.goto("/datasets/import");
  await expect(page.getByRole("link", { name: "Help desk calls" })).toBeVisible();
  await expect(page.getByText("No staged packages.")).toBeVisible();
  expect(errors).toEqual([]);
});
