// Export for offline: the size summary of a selection, shown while the server's preview is loading, and the
// apps a full kit carries (CONTRACTS.md §6).

import type { OfflineAppChoice, OfflineService } from "./api";

export interface SelectionSummary {
  services: number;
  imageBytes: number; // each distinct image once, before shared layers
  weightsBytes: number;
  gpuFraction: number;
  unknownImages: number; // images not present locally (their size is unknown)
}

export function selectionSummary(rows: OfflineService[], picked: Set<string>): SelectionSummary {
  const chosen = rows.filter((r) => picked.has(r.name));
  const images = new Map<string, number | null>();
  for (const r of chosen) images.set(r.image, r.image_bytes);
  return {
    services: chosen.length,
    imageBytes: [...images.values()].reduce<number>((a, b) => a + (b ?? 0), 0),
    weightsBytes: chosen.reduce((a, r) => a + r.weights_bytes, 0),
    gpuFraction: chosen.reduce((a, r) => a + r.gpu_fraction, 0),
    unknownImages: [...images.values()].filter((b) => b === null).length,
  };
}

// Which apps a full kit carries, and with which image.
export type AppPick = Record<string, { on: boolean; image: string | null }>;

// This app ticked with its configured image; the other app unticked, with its default local image (if any).
export function initialAppPick(choices: OfflineAppChoice[]): AppPick {
  return Object.fromEntries(choices.map((c) => [c.app, { on: c.own, image: c.default_image }]));
}

// The ticked apps as {app: image}, in the order the server lists them.
export function pickedApps(choices: OfflineAppChoice[], pick: AppPick): Record<string, string | null> {
  return Object.fromEntries(choices.filter((c) => pick[c.app]?.on).map((c) => [c.app, pick[c.app].image]));
}

// The preview's `apps` query: `app=image` (or a bare app for its default), comma-separated; "" for none.
export function appsQuery(apps: Record<string, string | null>): string {
  return Object.entries(apps).map(([app, image]) => (image ? `${app}=${image}` : app)).join(",");
}

// Bytes of the ticked app images, as `docker image inspect` reports them (before compression).
export function appImageBytes(choices: OfflineAppChoice[], pick: AppPick): number {
  return choices.reduce((sum, c) => {
    const p = pick[c.app];
    if (!p?.on) return sum;
    return sum + (c.images.find((i) => i.image === p.image)?.bytes ?? 0);
  }, 0);
}
