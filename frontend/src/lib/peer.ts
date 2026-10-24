// Concord Label, the peer app (CONTRACTS.md §1, SPLIT_PLAN.md §5), and reference-package helpers.
// Label links show only when the dataset came from Label and Label answers with our contract version.

import type { AppConfig, Dataset, ImportPreview, Provenance, ReferenceImport } from "./api";

export const LABEL_APP = "concord-label";
/** `source.install_id` of every Dataset Catalog version (DATASET_CATALOG_PLAN.md §5.1). */
export const CATALOG_INSTALL = "dataset-catalog";

export function appName(app: string): string {
  return app === LABEL_APP ? "Concord Label" : app === "concord-bench" ? "Concord Bench" : app;
}

/** Label answered at LABEL_URL with the contract version this app implements. */
export function labelLinked(cfg: Pick<AppConfig, "contract_version" | "peer"> | undefined): boolean {
  return !!cfg?.peer.url && cfg.peer.reachable && cfg.peer.contract_version === cfg.contract_version;
}

/** The provenance of a dataset imported from a reference package with a manifest, else null. */
export function provenanceOf(d: Pick<Dataset, "provenance"> | undefined): Provenance | null {
  const p = d?.provenance as Provenance | undefined;
  return p && typeof p.import_id === "number" ? p : null;
}

/** The dataset's references came from Concord Label (its flags can go back, its files open in Label). */
export function fromLabel(d: Pick<Dataset, "provenance"> | undefined): boolean {
  const src = provenanceOf(d)?.source;
  return !!src && src.app === LABEL_APP && !!src.dataset_uid;
}

/** Why "Send flags to Label" cannot push right now, or null when it can. */
export function flagsPushBlocker(cfg: Pick<AppConfig, "contract_version" | "peer"> | undefined, d: Pick<Dataset, "provenance">): string | null {
  if (!fromLabel(d)) return "These references did not come from Concord Label.";
  if (!cfg?.peer.url) return "LABEL_URL is not set, so flags can only be downloaded.";
  if (!cfg.peer.reachable) return `Concord Label at ${cfg.peer.url} is not answering; download the flags file instead.`;
  if (cfg.peer.contract_version !== cfg.contract_version)
    return `Concord Label implements contract version ${cfg.peer.contract_version}, this app ${cfg.contract_version}; download the flags file instead.`;
  return null;
}

/** One line on where a package or dataset came from. */
export function sourceLine(p: Pick<ImportPreview, "source" | "export_version" | "has_manifest">): string {
  if (!p.has_manifest) return "No manifest: read like an annotated upload, no provenance";
  if (!p.source) return "Hand-made or another tool (no source): no versions, no Label links";
  if (p.source.install_id === CATALOG_INSTALL)
    return `Dataset Catalog · ${p.source.dataset_name} · v${p.export_version ?? "?"} · published by ${appName(p.source.app)}`;
  const app = appName(p.source.app);
  return `${app} · ${p.source.dataset_name} · version ${p.export_version ?? "?"} · install ${p.source.install_id}`;
}

/** Imports waiting for a decision first (oldest first, as they arrived), then the decided ones, newest first. */
export function sortImports(list: ReferenceImport[]): { waiting: ReferenceImport[]; decided: ReferenceImport[] } {
  const waiting = list.filter((i) => i.status === "staged" || i.status === "accepting").sort((a, b) => a.id - b.id);
  const decided = list.filter((i) => !waiting.includes(i)).sort((a, b) => b.id - a.id);
  return { waiting, decided };
}

/** Skipped seg-model outputs as "2 build differs, 1 unknown model" (zero counts left out). */
export function skippedSummary(skipped: Record<string, number> | undefined): string {
  const parts = Object.entries(skipped ?? {})
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${n} ${k.replace(/_/g, " ")}`);
  return parts.join(", ");
}
