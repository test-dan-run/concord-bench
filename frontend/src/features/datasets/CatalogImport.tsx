// Import reference package → From Dataset Catalog (DATASET_CATALOG_PLAN.md §9.2): pick a catalog dataset
// and a version with references; "Preview" stages it as an import, which then waits above with the same
// preview a zip gets. Versions with audio only are listed but can't be imported.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, qs, type CatalogDataset, type CatalogVersion } from "../../lib/api";
import { ago, hours, num } from "../../lib/format";
import { appName } from "../../lib/peer";
import { Button, Chip, cx, Empty, ErrorNote, Panel, Spinner } from "../../components/ui";

function typeChips(types: string[]) {
  if (!types.length) return <Chip tone="warn">audio only</Chip>;
  return types.map((l) => <Chip key={l}>{l === "asr" ? "ASR" : l === "diar" ? "Diarisation" : l}</Chip>);
}

export function CatalogImport({ uiUrl }: { uiUrl: string }) {
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const list = useQuery({
    queryKey: ["dataset-catalog", "datasets", q],
    queryFn: () => api.get<CatalogDataset[]>(`/dataset-catalog/datasets${qs({ q })}`),
  });
  return (
    <Panel
      title="From Dataset Catalog"
      subtitle="Versions published by Concord Label or the catalog. The audio stays in the store, so nothing is copied."
      actions={
        <a href={uiUrl} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs">
          Open the Dataset Catalog
        </a>
      }
    >
      <input
        aria-label="Search the Dataset Catalog"
        placeholder="Search datasets"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        className="min-h-9 bg-canvas border border-line rounded-md px-3 text-[13px]"
      />
      {list.isLoading && <Spinner />}
      <ErrorNote error={list.error} />
      {list.data && list.data.length === 0 && <Empty>{q ? "No dataset matches." : "The Dataset Catalog has no datasets yet."}</Empty>}
      <ul className="m-0 p-0 list-none flex flex-col gap-1.5">
        {list.data?.map((d) => (
          <li key={d.id}>
            <button
              type="button"
              aria-expanded={picked === d.id}
              onClick={() => setPicked(picked === d.id ? null : d.id)}
              className={cx(
                "w-full text-left flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border px-3 py-2 cursor-pointer text-[13px] bg-transparent text-fg",
                picked === d.id ? "border-primary" : "border-track hover:bg-track",
              )}
            >
              <span className="font-medium">{d.name}</span>
              {d.latest && <Chip tone="info">v{d.latest.n}</Chip>}
              {d.latest ? typeChips(d.latest.label_types) : <Chip>no versions</Chip>}
              {d.language && <span className="text-xs text-muted num">{d.language}</span>}
              <span className="text-xs text-subtle ml-auto">{ago(d.updated_at)}</span>
            </button>
            {picked === d.id && <CatalogVersions id={d.id} />}
          </li>
        ))}
      </ul>
    </Panel>
  );
}

function CatalogVersions({ id }: { id: string }) {
  const qc = useQueryClient();
  const d = useQuery({ queryKey: ["dataset-catalog", "dataset", id], queryFn: () => api.get<CatalogDataset>(`/dataset-catalog/datasets/${id}`) });
  const stage = useMutation({
    mutationFn: (n: number) => api.post<{ id: number }>("/dataset-catalog/imports", { dataset_id: id, n }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["reference-imports"] }),
  });
  if (d.isLoading) return <Spinner />;
  if (d.error || !d.data) return <ErrorNote error={d.error} />;
  const versions = d.data.versions ?? [];
  return (
    <div className="flex flex-col border-l-2 border-track ml-3 pl-3 py-1">
      {versions.length === 0 && <span className="text-xs text-muted">No versions yet.</span>}
      {versions.map((v) => (
        <VersionRow key={v.n} v={v} pending={stage.isPending && stage.variables === v.n} onPreview={() => stage.mutate(v.n)} />
      ))}
      <ErrorNote error={stage.error} />
      {stage.isSuccess && <span className="text-xs text-good">Staged: check the preview under Waiting for a decision, then accept it.</span>}
    </div>
  );
}

function VersionRow({ v, pending, onPreview }: { v: CatalogVersion; pending: boolean; onPreview: () => void }) {
  const files = v.counts.files;
  return (
    <div className="flex items-start gap-3 py-2 border-b border-track last:border-0 text-[13px]" aria-label={`Version ${v.n}`}>
      <div className="flex-1 min-w-0 flex flex-col gap-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-medium num">v{v.n}</span>
          {typeChips(v.label_types)}
        </span>
        <span className="text-xs text-muted num">
          {num(files)} file{files === 1 ? "" : "s"} · {hours(v.counts.hours)}
          {v.label_types.length > 0 && ` · ${num(v.counts.segments)} segment${v.counts.segments === 1 ? "" : "s"}`}
        </span>
        <span className="text-xs text-subtle">
          Published by {appName(v.published_by)} {ago(v.published_at)}
          {v.parent_n != null && ` · based on v${v.parent_n}${v.parent_deleted ? " (deleted)" : ""}`}
        </span>
        {!v.importable && <span className="text-xs text-muted">Audio only: label it in Concord Label and publish a version with references to benchmark it.</span>}
        {v.bench.map((b) => (
          <span key={b.id} className="text-xs">
            {b.published ? "Published from" : "In Bench as"} <Link to={`/datasets/${b.id}`}>{b.name}</Link>
            {b.status !== "ready" && <span className="text-muted"> ({b.status})</span>}
          </span>
        ))}
      </div>
      <Button size="sm" disabled={!v.importable || pending} onClick={onPreview}>
        {pending ? "Staging…" : "Preview"}
      </Button>
    </div>
  );
}
