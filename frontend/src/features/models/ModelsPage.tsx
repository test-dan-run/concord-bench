// Models come from the Model Catalog: registration, verification and the model contract live in its own
// UI. This page lists the catalog's speech models read-only, by type (ASR, VAD / diarisation, speech
// enhancement), with their state, what the catalog's verifier found, cold start, declared VRAM and build,
// and how many Bench runs use each.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type CatalogModel, type ModelList } from "../../lib/api";
import { capLabel, coldStart, modelStateLabel, timingLabel } from "../../lib/format";
import { AnchorButton, Chip, cx, Empty, EnhChip, ErrorNote, PageHeader, Panel, Spinner, StatusDot } from "../../components/ui";

function Verified({ m }: { m: CatalogModel }) {
  if (!m.in_catalog) return <Chip tone="warn" title={m.not_usable ?? undefined}>not in the catalog</Chip>;
  if (m.capability_status === "verified") return <Chip tone="good">verified</Chip>;
  if (m.capability_status === "failed") return <Chip tone="bad" title={m.not_usable ?? undefined}>verification failed</Chip>;
  return <Chip tone="warn" title={m.not_usable ?? undefined}>{m.capability_status ?? "not verified"}</Chip>;
}

const TABS = [
  { id: "asr", label: "ASR", stage: "asr", note: "Speech to text. Scored by WER, CER, MER, WIL and WIP on the ASR leaderboard." },
  { id: "seg", label: "VAD / Diarisation", stage: "seg", note: "Speech regions and speakers. Scored by DER, JER and VAD error on the diarisation leaderboard." },
  { id: "enh", label: "Enhancement", stage: "enh", note: "Audio in, cleaned audio out. Each one builds an enhanced copy of a dataset that ASR and diarisation models then run on." },
] as const;

function Output({ m }: { m: CatalogModel }) {
  const s = m.check_sample;
  if (!s || (s.si_sdr_input_db === undefined && s.si_sdr_output_db === undefined)) return <span className="text-subtle">—</span>;
  return (
    <span className="flex flex-col">
      <span className="num">
        SI-SDR {s.si_sdr_input_db?.toFixed(1)} → {s.si_sdr_output_db?.toFixed(1)} dB
      </span>
      <span className="text-[11px] text-subtle">
        on the check clip{s.length_diff_samples ? ` · ${(Math.abs(s.length_diff_samples) / 16).toFixed(1)} ms length difference` : " · same length"}
      </span>
    </span>
  );
}

export function ModelsPage() {
  const q = useQuery({ queryKey: ["models"], queryFn: () => api.get<ModelList>("/models"), refetchInterval: 5000 });
  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>("asr");
  const cat = q.data?.catalog;
  const cur = TABS.find((t) => t.id === tab)!;
  const all = q.data?.models ?? [];
  const inTab = (m: CatalogModel, stage: string) => m.stages.includes(stage) || (stage === "asr" && !m.stages.length);
  const models = all.filter((m) => inTab(m, cur.stage));
  return (
    <>
      <PageHeader
        crumbs="Bring your own"
        title="Models"
        subtitle="Speech models from the Model Catalog. Register, verify and tag models in the catalog; Bench lists them and leases them for its runs."
        actions={cat ? <AnchorButton href={cat.ui_url} variant="primary" newTab>Open the Model Catalog</AnchorButton> : undefined}
      />
      <div role="tablist" aria-label="Model type" className="flex flex-wrap gap-1 border-b border-track">
        {TABS.map((t) => {
          const on = t.id === tab;
          return (
            <button key={t.id} type="button" role="tab" aria-selected={on} onClick={() => setTab(t.id)} className={cx("inline-flex items-center gap-2 bg-transparent border-0 border-b-2 px-3 pt-1 pb-2.5 text-[13px] cursor-pointer", on ? "border-primary text-fg font-medium" : "border-transparent text-muted hover:text-fg")}>
              {t.label}
              <span className={cx("num text-[11px] px-1.5 rounded-full", on ? "bg-active text-fg" : "bg-track text-muted")}>{all.filter((m) => inTab(m, t.stage)).length}</span>
            </button>
          );
        })}
      </div>
      <span className="text-xs text-muted -mt-2">{cur.note}</span>
      <Panel>
        {q.isLoading && <Spinner />}
        <ErrorNote error={q.error} />
        {cat && !cat.available && <ErrorNote error={`The Model Catalog is not reachable (${cat.error}). Showing the models Bench has used, as last seen.`} />}
        {q.data && models.length === 0 && (
          <Empty>
            The catalog lists no {cur.label.toLowerCase()} models yet. Register one in the <a href={cat?.ui_url} target="_blank" rel="noreferrer">Model Catalog</a>.
          </Empty>
        )}
        {models.length > 0 && (
          <div className="overflow-x-auto scroll-thin">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Model</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">State</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Capabilities</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">{tab === "enh" ? "Output" : "Timing"}</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">Cold start</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">VRAM</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Build</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line">Tags</th>
                  <th scope="col" className="font-medium px-3 py-2 border-b border-line text-right">Runs</th>
                </tr>
              </thead>
              <tbody>
                {models.map((m) => (
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
                        {m.generative && <EnhChip title="Can shift timing or invent speech">generative</EnhChip>}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-xs text-muted">{tab === "enh" ? <Output m={m} /> : m.stages.includes("asr") ? timingLabel(m.timing_level) : "segments"}</td>
                    <td className="px-3 py-2.5 text-right num text-xs" title={m.cold_start_measured ? "median of measured loads" : "estimate: no load of this build measured yet"}>
                      {coldStart(m.cold_start_s, m.cold_start_measured)}
                    </td>
                    <td className="px-3 py-2.5 text-right num text-xs" title="The VRAM the model's manifest declares">
                      {m.gpu ? (m.gpu.gb !== undefined ? `${m.gpu.gb} GB` : "GPU") : "CPU"}
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
