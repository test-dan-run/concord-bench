import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Dataset } from "../../lib/api";
import { ago, hours, num } from "../../lib/format";
import * as I from "../../components/icons";
import { Button, Chip, Empty, EnhChip, ErrorNote, JobPill, LinkButton, PageHeader, Panel, Spinner } from "../../components/ui";
import { AddEnhancement } from "./AddEnhancement";

export function DatasetsPage() {
  const q = useQuery({
    queryKey: ["datasets"],
    queryFn: () => api.get<Dataset[]>("/datasets"),
    refetchInterval: (query) =>
      query.state.data?.some((d) => d.status === "ingesting" || (d.enhanced ?? []).some((c) => c.status === "building")) ? 2000 : false,
  });
  const [addFor, setAddFor] = useState<number | null>(null);
  const ready = (q.data ?? []).filter((d) => d.status === "ready");
  return (
    <>
      <PageHeader
        crumbs="Bring your own"
        title="Datasets"
        subtitle="Every upload is its own dataset. No train/dev/test splits inside a dataset."
        actions={
          <>
            {ready.length > 0 && (
              <Button onClick={() => setAddFor(ready[0].id)} aria-haspopup="dialog">
                <span className="text-enh">
                  <I.Pulse size={16} />
                </span>
                Add enhancement
              </Button>
            )}
            <LinkButton to="/datasets/import">
              <I.Download /> Import reference package
            </LinkButton>
            <LinkButton to="/datasets/upload" variant="primary">
              <I.Upload /> Upload dataset
            </LinkButton>
          </>
        }
      />
      {addFor !== null && <AddEnhancement datasets={ready} initial={addFor} onClose={() => setAddFor(null)} />}
      <Panel>
        {q.isLoading && <Spinner />}
        <ErrorNote error={q.error} />
        {q.data && q.data.length === 0 && <Empty>No datasets yet. <Link to="/datasets/upload">Upload one</Link>.</Empty>}
        {q.data && q.data.length > 0 && (
          <div className="overflow-x-auto scroll-thin">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Name</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Labels</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">Files</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">Audio</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Flagged</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Enhanced copies</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Tags</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Added</th>
                </tr>
              </thead>
              <tbody>
                {q.data.map((d) => (
                  <tr key={d.id} className="border-b border-track hover:bg-track/50">
                    <td className="px-3 py-2.5">
                      <Link to={`/datasets/${d.id}`} className="!text-fg hover:!text-focus">{d.name}</Link>
                      {d.status !== "ready" && <span className="ml-2"><JobPill status={d.status === "ingesting" ? "running" : "failed"} /></span>}
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="flex gap-1">
                        {d.label_types.includes("asr") && <Chip>ASR</Chip>}
                        {d.label_types.includes("diar") && <Chip>Diarisation</Chip>}
                        {d.has_speaker_text && <Chip tone="info">cpWER</Chip>}
                        {d.export_version != null && <Chip title="Reference package version">v{d.export_version}</Chip>}
                        {!d.label_types.length && d.status === "ready" && <Chip>audio only</Chip>}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-right num">{num(d.usable_files)}<span className="text-subtle">/{num(d.files)}</span></td>
                    <td className="px-3 py-2.5 text-right num">{hours(d.usable_hours)}</td>
                    <td className="px-3 py-2.5 text-xs text-muted num">
                      {Object.entries(d.flagged).filter(([, n]) => n).map(([k, n]) => `${n} ${k.replace("_", " ")}`).join(" · ") || "—"}
                    </td>
                    <td className="px-3 py-2.5">
                      <span className="flex flex-wrap items-center gap-1">
                        {d.status !== "ready" ? (
                          <span className="text-[11px] text-subtle" title="Enhanced copies can be added once the dataset finishes ingesting">after ingest</span>
                        ) : (
                          <>
                            {(d.enhanced ?? []).filter((c) => c.status !== "failed" && c.status !== "cancelled").map((c) => (
                              <EnhChip key={c.display_name} dashed={c.status === "building"} title={`${c.display_name} · ${c.status}`}>
                                {c.display_name}
                                {c.status === "building" ? ` · ${Math.round(c.progress * 100)}%` : ""}
                              </EnhChip>
                            ))}
                            {!(d.enhanced ?? []).length && <span className="text-xs text-subtle">none</span>}
                            <button type="button" aria-label={`Add enhancement models to ${d.name}`} onClick={() => setAddFor(d.id)} className="inline-flex items-center min-h-[22px] px-2 border border-dashed border-line rounded bg-transparent text-muted text-[11px] cursor-pointer hover:text-fg">
                              + Add
                            </button>
                          </>
                        )}
                      </span>
                    </td>
                    <td className="px-3 py-2.5"><span className="flex flex-wrap gap-1">{d.tags.map((t) => <Chip key={t}>#{t}</Chip>)}</span></td>
                    <td className="px-3 py-2.5 text-xs text-muted">{ago(d.created_at)}</td>
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
