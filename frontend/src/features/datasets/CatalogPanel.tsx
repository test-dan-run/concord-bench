// The dataset page's Dataset Catalog panel (DATASET_CATALOG_PLAN.md §9.3): the catalog version the dataset
// was imported from or published as, or "Publish" for a dataset from an annotated upload or a zip, as
// version 1 of a new catalog dataset or as the next version of an existing one. When another app published
// a newer version meanwhile (409), the person is asked whether to publish anyway.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, ApiError, type CatalogDataset, type Dataset } from "../../lib/api";
import { ago } from "../../lib/format";
import { appName } from "../../lib/peer";
import { Button, ErrorNote, Fact, Panel, Select, Spinner, Toggle } from "../../components/ui";

type Target = "new" | "existing";

export function CatalogPanel({ d }: { d: Dataset }) {
  if (d.catalog) {
    return (
      <Panel id="publish" title="Dataset Catalog">
        <span className="text-[13px]">
          {d.catalog.published ? `Published from here as v${d.catalog.n}.` : `Made from v${d.catalog.n} in the Dataset Catalog.`}
        </span>
        {!d.catalog.published && <span className="text-xs text-muted">Its next versions are published from Concord Label, where the references are corrected.</span>}
        <a href={d.catalog.url} target="_blank" rel="noopener noreferrer" className="text-xs self-start">
          Open v{d.catalog.n} in the Dataset Catalog
        </a>
      </Panel>
    );
  }
  if (d.from_catalog) {
    return (
      <Panel id="publish" title="Dataset Catalog">
        <span className="text-xs text-muted">Made from a Dataset Catalog version. Its next versions are published from Concord Label, where the references are corrected.</span>
      </Panel>
    );
  }
  return <PublishForm d={d} />;
}

function PublishForm({ d }: { d: Dataset }) {
  const qc = useQueryClient();
  const [target, setTarget] = useState<Target>(d.publish_target ? "existing" : "new");
  const [name, setName] = useState(d.name);
  const [picked, setPicked] = useState<string>(d.publish_target?.dataset_id ?? "");
  const list = useQuery({
    queryKey: ["dataset-catalog", "datasets", ""],
    queryFn: () => api.get<CatalogDataset[]>("/dataset-catalog/datasets"),
    enabled: target === "existing",
  });
  useEffect(() => {
    if (!picked && list.data?.length) setPicked(list.data[0].id);
  }, [list.data, picked]);
  const chosen = list.data?.find((c) => c.id === picked);
  const publish = useMutation({
    mutationFn: async () => {
      const body = target === "new" ? { name } : { catalog_dataset_id: picked, parent: chosen?.latest?.n ?? null };
      try {
        return await api.post(`/datasets/${d.id}/publish`, body);
      } catch (e) {
        // A stale parent: another app published a newer version since this page showed the latest.
        if (e instanceof ApiError && e.status === 409 && confirm(`${e.message} Publish anyway?`))
          return api.post(`/datasets/${d.id}/publish`, { ...body, force: true });
        throw e;
      }
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["dataset", String(d.id)] });
      void qc.invalidateQueries({ queryKey: ["dataset-catalog"] });
    },
  });
  if (d.publish_blocker) {
    return (
      <Panel id="publish" title="Dataset Catalog">
        <span className="text-xs text-muted">{d.publish_blocker}</span>
      </Panel>
    );
  }
  return (
    <Panel id="publish" title="Publish to the Dataset Catalog" subtitle="A version written from this dataset's segments and labels, so Concord Label and others can use it. The audio goes to the store if it isn't there yet.">
      <Toggle
        label="Publish as"
        value={target}
        onChange={(v) => setTarget(v as Target)}
        options={[
          { value: "new", label: "New dataset" },
          { value: "existing", label: "New version of a dataset" },
        ]}
      />
      {target === "new" ? (
        <label className="flex flex-col gap-1.5 text-xs text-muted">
          Name in the catalog
          <input value={name} onChange={(e) => setName(e.target.value)} className="min-h-10 bg-canvas border border-line rounded-md px-3 text-[13px] text-fg" />
        </label>
      ) : list.isLoading ? (
        <Spinner />
      ) : (
        <>
          <ErrorNote error={list.error} />
          {list.data && list.data.length === 0 && <span className="text-xs text-muted">The Dataset Catalog has no datasets yet.</span>}
          {list.data && list.data.length > 0 && (
            <Select label="Dataset" value={picked} onChange={(e) => setPicked(e.target.value)} className="min-w-0 max-w-[240px]">
              {list.data.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                  {c.latest ? ` (v${c.latest.n})` : ""}
                </option>
              ))}
            </Select>
          )}
          {chosen && (
            <Fact
              k="Based on"
              v={chosen.latest ? `v${chosen.latest.n}, published by ${appName(chosen.latest.published_by)} ${ago(chosen.latest.published_at)}` : "no version yet (this becomes v1)"}
            />
          )}
        </>
      )}
      <ErrorNote error={publish.error} />
      {publish.isSuccess && <span className="text-xs text-good">Published.</span>}
      <Button
        variant="primary"
        disabled={publish.isPending || (target === "new" ? !name.trim() : !chosen)}
        onClick={() => publish.mutate()}
      >
        {publish.isPending ? "Publishing…" : "Publish"}
      </Button>
    </Panel>
  );
}
