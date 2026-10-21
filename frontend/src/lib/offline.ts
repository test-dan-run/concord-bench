// Export for offline: the size summary of a selection, shown while the server's preview is loading.

import type { OfflineService } from "./api";

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
