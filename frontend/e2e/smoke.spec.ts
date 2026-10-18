import { expect, test, type Page } from "@playwright/test";

// Acceptance item 3: every error-analysis view loads, sorts, expands and plays audio on the fixture
// set; plus a load check of every other Phase 1 screen. Console errors fail the test.

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

test("bring-your-own screens load; lite has no Training", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/datasets");
  await expect(page.getByRole("link", { name: "Synth fixtures" })).toBeVisible();
  await page.getByRole("link", { name: "Synth fixtures" }).click();
  await expect(page.getByText("Flagged files")).toBeVisible();
  await expect(page.getByText(/Over the length cap/)).toBeVisible();
  await page.goto("/datasets/upload");
  await expect(page.getByRole("heading", { name: "Upload dataset" })).toBeVisible();
  await expect(page.getByRole("link", { name: "asr.jsonl" })).toBeVisible();
  await page.goto("/models/add?name=pyannote-diarization");
  await expect(page.getByText("OIP signature")).toBeVisible();
  await page.goto("/models");
  await expect(page.getByText("faster-whisper-large-v3 · openai / audio.transcriptions")).toBeVisible();
  await page.goto("/contract");
  await expect(page.getByText("How capabilities and timing are detected")).toBeVisible();
  await page.goto("/jobs");
  await expect(page.getByRole("heading", { name: "Jobs" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Training" })).toHaveCount(0);
  await page.goto("/training");
  await expect(page).toHaveURL(/\/benchmarks/);
  expect(errors).toEqual([]);
});
