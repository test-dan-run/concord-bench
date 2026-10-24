// Models come from the model catalog: registration, verification and the model contract live in its own
// UI. This page lists the catalog's speech models read-only, with their state, what the catalog's verifier
// found, cold start and build, and how many Bench runs use each.

import { useQuery } from "@tanstack/react-query";
import { api, type CatalogModel, type ModelList } from "../../lib/api";
import { capLabel, coldStart, modelStateLabel, timingLabel } from "../../lib/format";
import { AnchorButton, Chip, Empty, ErrorNote, PageHeader, Panel, Spinner, StatusDot } from "../../components/ui";

function Verified({ m }: { m: CatalogModel }) {
  if (!m.in_catalog) return <Chip tone="warn" title={m.not_usable ?? undefined}>not in the catalog</Chip>;
  if (m.capability_status === "verified") return <Chip tone="good">verified</Chip>;
  if (m.capability_status === "failed") return <Chip tone="bad" title={m.not_usable ?? undefined}>verification failed</Chip>;
  return <Chip tone="warn" title={m.not_usable ?? undefined}>{m.capability_status ?? "not verified"}</Chip>;
}

export function ModelsPage() {
  const q = useQuery({ queryKey: ["models"], queryFn: () => api.get<ModelList>("/models"), refetchInterval: 5000 });
  const cat = q.data?.catalog;
  return (
    <>
      <PageHeader
        crumbs="Bring your own"
        title="Models"
        subtitle="Speech models from the model catalog. Register, verify and tag models in the catalog; Bench lists them and leases them for its runs."
        actions={cat ? <AnchorButton href={cat.ui_url} variant="primary" newTab>Open the model catalog</AnchorButton> : undefined}
      />
      <Panel>
        {q.isLoading && <Spinner />}
        <ErrorNote error={q.error} />
        {cat && !cat.available && <ErrorNote error={`The model catalog is not reachable (${cat.error}). Showing the models Bench has used, as last seen.`} />}
        {q.data?.models.length === 0 && (
          <Empty>
            The catalog lists no speech models yet. Register one in the <a href={cat?.ui_url} target="_blank" rel="noreferrer">model catalog</a>.
          </Empty>
        )}
        {!!q.data?.models.length && (
          <div className="overflow-x-auto scroll-thin">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Model</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">State</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Capabilities</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Timing</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">Cold start</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Build</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Tags</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">Runs</th>
                </tr>
              </thead>
              <tbody>
                {q.data.models.map((m) => (
                  <tr key={m.catalog_id} className="border-b border-track align-top">
                    <td className="px-3 py-2.5">
                      <span className="flex flex-col">
                        <span>{m.display_name}</span>
                        <span className="text-[11px] text-subtle num">
                          {m.catalog_id}
                          {m.contract ? ` · ${m.contract} / ${m.api}` : ""}
                        </span>
                        {m.parent_id && <span className="text-[11px] text-subtle">derived from {m.parent_id}</span>}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="inline-flex items-center gap-1.5 text-xs" title={m.state_detail ?? (m.leased_by.length ? `leased by ${m.leased_by.join(", ")}` : undefined)}>
                        <StatusDot state={m.state ?? "unknown"} />
                        {m.in_catalog ? modelStateLabel(m.state) : "—"}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="flex flex-wrap gap-1">
                        <Verified m={m} />
                        {m.capabilities.map((c) => (
                          <Chip key={c} tone="good">
                            {capLabel(c)}
                          </Chip>
                        ))}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-xs text-muted">{m.stages.includes("asr") ? timingLabel(m.timing_level) : "segments"}</td>
                    <td className="px-3 py-2.5 text-right num text-xs" title={m.cold_start_measured ? "median of measured loads" : "estimate: no load of this build measured yet"}>
                      {coldStart(m.cold_start_s, m.cold_start_measured)}
                    </td>
                    <td className="px-3 py-2.5 text-[11px] text-muted num break-all max-w-[220px]">
                      <span className="flex flex-col">
                        <span>{m.build_key ?? "—"}</span>
                        {m.image && <span className="text-subtle">{m.image}</span>}
                      </span>
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="flex flex-wrap gap-1">
                        {m.tags.map((t) => (
                          <Chip key={t}>#{t}</Chip>
                        ))}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-right num">{m.runs}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}
