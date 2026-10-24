import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, qs, type Job, type OfflineAppChoice, type OfflineExport, type OfflinePreview, type OfflineServices } from "../../lib/api";
import { ago, bytes, num, pct } from "../../lib/format";
import { appImageBytes, appsQuery, initialAppPick, pickedApps, selectionSummary, type AppPick } from "../../lib/offline";
import * as I from "../../components/icons";
import { Button, Chip, cx, Empty, ErrorNote, Fact, JobPill, PageHeader, Panel, Select, Spinner, Toggle } from "../../components/ui";

const KINDS = [
  { value: "full", label: "Full kit" },
  { value: "models", label: "Model kit" },
];
const KIND_NOTE: Record<string, string> = {
  full: "The ticked apps, platform and the chosen models. Used once, to set up the offline machine.",
  models: "The chosen models only. Added with install.sh --add to an offline machine that already has a full kit.",
};

function size(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : bytes(n);
}

function useDebounced<T>(value: T, ms = 400): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function ExportOffline() {
  const qc = useQueryClient();
  const table = useQuery({ queryKey: ["offline-services"], queryFn: () => api.get<OfflineServices>("/offline/services") });
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [kind, setKind] = useState("full");
  const [appPick, setAppPick] = useState<AppPick | null>(null);
  const [dest, setDest] = useState("");
  const [name, setName] = useState("");
  const [q, setQ] = useState("");
  const [started, setStarted] = useState<number | null>(null);
  useEffect(() => {
    if (table.data && !dest) setDest(table.data.default_dest);
    if (table.data && !appPick) setAppPick(initialAppPick(table.data.apps));
  }, [table.data, dest, appPick]);

  const choices = table.data?.apps ?? [];
  const apps = pickedApps(choices, appPick ?? {});
  const appsQ = kind === "full" ? appsQuery(apps) : undefined;
  const noApp = kind === "full" && !!appPick && Object.keys(apps).length === 0;
  const names = useMemo(() => [...picked].sort(), [picked]);
  const destQ = useDebounced(dest);
  const nameQ = useDebounced(name);
  const pv = useQuery({
    queryKey: ["offline-preview", names.join(","), kind, destQ, appsQ, nameQ],
    queryFn: () => api.get<OfflinePreview>(`/offline/preview${qs({ services: names.join(","), kind, dest: destQ, apps: appsQ, name: nameQ })}`),
    enabled: names.length > 0 && !!destQ && !noApp,
  });
  const go = useMutation({
    mutationFn: () => api.post<{ export: OfflineExport }>("/offline/exports", { services: names, kind, dest, apps: kind === "full" ? apps : null, name: name || null }),
    onSuccess: (r) => {
      setStarted(r.export.id);
      void qc.invalidateQueries({ queryKey: ["offline-exports"] });
      void qc.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  const rows = (table.data?.services ?? []).filter((r) => !q || `${r.name} ${r.alias ?? ""} ${r.tasks.join(" ")} ${r.description ?? ""}`.toLowerCase().includes(q.toLowerCase()));
  const quick = selectionSummary(table.data?.services ?? [], picked);
  const p = pv.data;
  const toggle = (n: string) =>
    setPicked((s) => {
      const next = new Set(s);
      if (next.has(n)) next.delete(n);
      else next.add(n);
      return next;
    });

  return (
    <>
      <PageHeader
        crumbs="Bring your own"
        title="Export for offline"
        subtitle="Write a kit for a machine without internet access: the models' images and weights, the platform files that start them, and for a full kit Concord Bench, Concord Label or both. Registrations, scores and datasets stay here."
      />
      <div className="flex flex-col xl:flex-row gap-5 items-start">
        <Panel className="flex-1 w-full" title="Platform registry" subtitle="Every service in the platform, used by Bench or not. Nothing is added automatically, not even the other half of a pipeline.">
          <label className="relative flex max-w-sm">
            <span className="sr-only">Search the registry</span>
            <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-subtle"><I.Search size={15} /></span>
            <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search services or tasks" className="w-full min-h-9 bg-canvas border border-line rounded-md pl-8 pr-2 text-[13px] text-fg" />
          </label>
          {table.isLoading && <Spinner label="Reading the platform (image and weight sizes)" />}
          <ErrorNote error={table.error} />
          {table.data && (
            <div className="overflow-x-auto scroll-thin">
              <table className="w-full border-collapse text-[13px]">
                <caption className="sr-only">Platform services with image and weight sizes; tick the ones to export</caption>
                <thead>
                  <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
                    <th scope="col" className="font-medium px-2 py-1.5 border-b border-line w-8"><span className="sr-only">Export</span></th>
                    <th scope="col" className="font-medium px-2 py-1.5 border-b border-line">Service</th>
                    <th scope="col" className="font-medium px-2 py-1.5 border-b border-line">Tasks</th>
                    <th scope="col" className="font-medium px-2 py-1.5 border-b border-line text-right">Image</th>
                    <th scope="col" className="font-medium px-2 py-1.5 border-b border-line text-right">Weights</th>
                    <th scope="col" className="font-medium px-2 py-1.5 border-b border-line">In Concord</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.name} className={cx("border-b border-track align-top", picked.has(r.name) ? "bg-active" : "hover:bg-track/60")}>
                      <td className="px-2 py-1.5">
                        <input type="checkbox" aria-label={`Export ${r.name}`} checked={picked.has(r.name)} onChange={() => toggle(r.name)} className="mt-0.5" />
                      </td>
                      <td className="px-2 py-1.5 min-w-[200px]">
                        <span className="flex flex-col cursor-pointer" onClick={() => toggle(r.name)}>
                          <span>{r.name}{r.alias && <span className="text-subtle"> · {r.alias}</span>}</span>
                          <span className="text-[11px] text-subtle">{r.kind} · {r.engine} · {r.gpu ? `GPU ${pct(r.gpu_fraction, 0)}` : "CPU"}</span>
                        </span>
                        {r.problems.map((x) => (
                          <span key={x} className="flex items-start gap-1 text-[11px] text-warn mt-0.5"><I.Info size={12} />{x.replace(`${r.name}: `, "")}</span>
                        ))}
                      </td>
                      <td className="px-2 py-1.5 text-xs text-muted" title={r.tasks.join(", ")}>
                        {r.tasks.slice(0, 2).join(", ")}{r.tasks.length > 2 && <span className="text-subtle"> +{r.tasks.length - 2}</span>}
                      </td>
                      <td className="px-2 py-1.5 text-right num whitespace-nowrap" title={r.image}>{r.image_present === false ? <Chip tone="warn">not built</Chip> : size(r.image_bytes)}</td>
                      <td className="px-2 py-1.5 text-right num whitespace-nowrap">{!r.weights ? <span className="text-subtle">none</span> : r.weights.problems.length ? <Chip tone="warn">missing</Chip> : size(r.weights_bytes)}</td>
                      <td className="px-2 py-1.5">{r.model_id ? <Chip tone="good">{r.display_name ?? "registered"}</Chip> : <span className="text-xs text-subtle">no</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {rows.length === 0 && <Empty>No service matches “{q}”.</Empty>}
            </div>
          )}
        </Panel>

        <div className="w-full xl:w-[380px] shrink-0 flex flex-col gap-5 xl:sticky xl:top-5">
          <Panel title="Kit">
            <div className="flex flex-col gap-1.5">
              <Toggle label="Kit kind" value={kind} onChange={setKind} options={KINDS} />
              <span className="text-xs text-muted">{KIND_NOTE[kind]}</span>
            </div>
            {kind === "full" && (
              <fieldset className="flex flex-col gap-2">
                <legend className="text-xs text-muted mb-1">Apps in the kit</legend>
                {choices.map((c) => (
                  <AppChoice
                    key={c.app}
                    choice={c}
                    on={!!appPick?.[c.app]?.on}
                    image={appPick?.[c.app]?.image ?? null}
                    onChange={(next) => setAppPick((p) => ({ ...(p ?? {}), [c.app]: next }))}
                  />
                ))}
                {Object.keys(apps).length === 2 && <span className="text-[11px] text-subtle">Both apps start from one compose file and know each other's address, so Send to Bench and Open in Label work offline.</span>}
              </fieldset>
            )}
            <label className="flex flex-col gap-1 text-xs text-muted">
              Destination folder (on this machine)
              <input value={dest} onChange={(e) => setDest(e.target.value)} spellCheck={false} className="bg-canvas border border-line rounded-md px-2 min-h-[34px] text-fg text-[13px] num" />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted">
              Kit name (optional)
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="from the services" className="bg-canvas border border-line rounded-md px-2 min-h-[34px] text-fg text-[13px]" />
            </label>

            <div className="flex flex-col">
              <Fact k="Services" v={num(quick.services)} />
              <Fact k="Model images" v={p ? `up to ${size(p.images.bytes)}` : quick.services ? `up to ${size(quick.imageBytes)}` : "—"} />
              <Fact k="Weights" v={size(p ? p.weights_bytes : quick.services ? quick.weightsBytes : null)} />
              {kind === "full" && <Fact k="App images" v={size(p ? p.apps_bytes : appImageBytes(choices, appPick ?? {}))} />}
              <Fact k="Total" v={p ? `up to ${size(p.bytes)}` : "—"} />
              <Fact k="Free at destination" v={size(p?.free_bytes)} />
              <Fact k="VRAM, all together" v={p ? `${pct(p.budget.total_fraction)} of ${pct(p.budget.util_limit, 0)} limit` : quick.services ? pct(quick.gpuFraction) : "—"} />
            </div>
            <span className="text-[11px] text-subtle">Images are counted before shared layers and compression; one docker save stores shared layers once, so the kit is usually smaller.</span>
            {pv.isFetching && <Spinner label="Checking images and weights" />}
            {noApp && <div className="border border-bad/50 bg-bad/10 rounded-md px-3 py-2 text-xs">A full kit needs at least one app: include Concord Bench, Concord Label or both.</div>}
            <ErrorNote error={pv.error} />
            {p?.problems.map((x) => <div key={x} className="border border-bad/50 bg-bad/10 rounded-md px-3 py-2 text-xs">{x}</div>)}
            {p?.warnings.map((x) => <div key={x} className="border border-warn/50 bg-warn/10 rounded-md px-3 py-2 text-xs">{x}</div>)}
            {p?.kit && !p.problems.length && <span className="text-xs text-muted">Writes <span className="num text-fg break-all">{p.kit}</span></span>}
            <ErrorNote error={go.error} />
            <Button variant="primary" disabled={!names.length || noApp || !p || p.problems.length > 0 || pv.isFetching || go.isPending} onClick={() => go.mutate()}>
              <I.Download size={14} /> {go.isPending ? "Starting…" : "Export kit"}
            </Button>
          </Panel>
          {started !== null && <Started id={started} />}
        </div>
      </div>
      <History />
    </>
  );
}

// One app a full kit can carry: a checkbox, and its local image (a choice when `docker images` lists several).
export function AppChoice({ choice: c, on, image, onChange }: { choice: OfflineAppChoice; on: boolean; image: string | null; onChange: (next: { on: boolean; image: string | null }) => void }) {
  const present = c.images.filter((i) => i.present);
  const current = c.images.find((i) => i.image === image);
  return (
    <div className="flex flex-col gap-1">
      <label className="flex items-center gap-2 text-[13px] text-fg">
        <input type="checkbox" checked={on} disabled={!present.length && !on} onChange={(e) => onChange({ on: e.target.checked, image })} />
        Include {c.title}
        <span className="text-[11px] text-subtle">port {c.port}{c.own ? " · this app" : ""}</span>
      </label>
      <span className="pl-6 text-xs text-muted">
        {!present.length || (current && !current.present) ? (
          <span className="text-warn">{image ? `${image}: ` : ""}image not found — run <span className="num">just build</span> in that repo</span>
        ) : present.length > 1 ? (
          <Select aria-label={`${c.title} image`} value={image ?? ""} onChange={(e) => onChange({ on, image: e.target.value })}>
            {present.map((i) => <option key={i.image} value={i.image}>{i.image} · {size(i.bytes)}</option>)}
          </Select>
        ) : (
          <span className="num">{present[0].image} · {size(present[0].bytes)}</span>
        )}
      </span>
    </div>
  );
}

function Started({ id }: { id: number }) {
  const hist = useQuery({ queryKey: ["offline-exports"], queryFn: () => api.get<OfflineExport[]>("/offline/exports"), refetchInterval: 2000 });
  const rec = hist.data?.find((e) => e.id === id);
  const job = useQuery({
    queryKey: ["job", rec?.job_id],
    queryFn: () => api.get<Job>(`/jobs/${rec!.job_id}`),
    enabled: !!rec?.job_id,
    refetchInterval: (query) => (query.state.data && ["queued", "running"].includes(query.state.data.status) ? 1500 : false),
  });
  if (!rec) return null;
  return (
    <Panel title="Export" actions={<Link to="/jobs" className="text-xs">Jobs page</Link>}>
      <span className="flex items-center gap-2 text-[13px]">
        <JobPill status={job.data?.status ?? rec.status} progress={job.data?.progress} />
        <span className="text-xs text-muted truncate">{job.data?.message}</span>
      </span>
      {rec.status === "done" && (
        <div className="flex flex-col gap-1 text-[13px]">
          <span className="text-good">Kit written ({size(rec.bytes)}).</span>
          <span className="num break-all">{rec.path}</span>
          <span className="text-xs text-muted">Copy the folder to the offline machine and run <span className="num">./install.sh</span>{rec.kind === "models" ? " --add <install>" : ""} there.</span>
        </div>
      )}
      {rec.status === "failed" && <ErrorNote error={rec.error} />}
    </Panel>
  );
}

function History() {
  const hist = useQuery({ queryKey: ["offline-exports"], queryFn: () => api.get<OfflineExport[]>("/offline/exports"), refetchInterval: 4000 });
  return (
    <Panel title="History" subtitle="Kits written by this install">
      {hist.isLoading && <Spinner />}
      <ErrorNote error={hist.error} />
      {hist.data?.length === 0 && <Empty>No kits yet.</Empty>}
      {!!hist.data?.length && (
        <div className="overflow-x-auto scroll-thin">
          <table className="w-full border-collapse text-[13px]">
            <caption className="sr-only">Offline kits written by this install</caption>
            <thead>
              <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
                <th scope="col" className="font-medium px-2 py-1.5 border-b border-line">When</th>
                <th scope="col" className="font-medium px-2 py-1.5 border-b border-line">Kind</th>
                <th scope="col" className="font-medium px-2 py-1.5 border-b border-line">Services</th>
                <th scope="col" className="font-medium px-2 py-1.5 border-b border-line">Status</th>
                <th scope="col" className="font-medium px-2 py-1.5 border-b border-line">Kit</th>
                <th scope="col" className="font-medium px-2 py-1.5 border-b border-line text-right">Size</th>
              </tr>
            </thead>
            <tbody>
              {hist.data.map((e) => (
                <tr key={e.id} className="border-b border-track align-top">
                  <td className="px-2 py-1.5 text-xs text-muted whitespace-nowrap">{ago(e.finished_at ?? e.created_at)}</td>
                  <td className="px-2 py-1.5 text-xs">{e.kind === "full" ? "Full" : "Models"}{e.app_images.length > 0 && <span className="text-subtle"> · {e.app_images.join(", ")}</span>}</td>
                  <td className="px-2 py-1.5 text-xs">{e.services.join(", ")}</td>
                  <td className="px-2 py-1.5"><JobPill status={e.job?.status ?? e.status} progress={e.job?.progress} /></td>
                  <td className="px-2 py-1.5 text-xs num break-all">{e.path ?? (e.error ? <span className="text-bad">{e.error}</span> : e.dest)}</td>
                  <td className="px-2 py-1.5 text-right num whitespace-nowrap">{size(e.bytes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
