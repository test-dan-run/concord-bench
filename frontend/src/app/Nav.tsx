import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { api, type Job } from "../lib/api";
import * as I from "../components/icons";
import { cx } from "../components/ui";

function Item({ to, icon, children, disabled, hint, prefix }: { to: string; icon: ReactNode; children: ReactNode; disabled?: boolean; hint?: string; prefix?: boolean }) {
  if (disabled) {
    return (
      <span aria-disabled title={hint} className="flex items-center gap-2.5 px-2.5 min-h-10 rounded-md text-subtle cursor-not-allowed select-none whitespace-nowrap">
        {icon}
        {children}
      </span>
    );
  }
  return (
    <NavLink
      to={to}
      end={!prefix}
      className={({ isActive }) =>
        cx("flex items-center gap-2.5 px-2.5 min-h-10 rounded-md no-underline", isActive ? "bg-active !text-fg" : "!text-muted hover:!text-fg hover:bg-track")
      }
    >
      {icon}
      {children}
    </NavLink>
  );
}

function Group({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="flex items-baseline justify-between text-[11px] text-muted uppercase tracking-[0.06em] px-2.5 pb-1.5">
        {title}
        {note && <span className="text-[10px] text-subtle">{note}</span>}
      </span>
      {children}
    </div>
  );
}

function Jobs() {
  const { data } = useQuery({
    queryKey: ["jobs", "active"],
    queryFn: () => api.get<Job[]>("/jobs?active=true&limit=20"),
    refetchInterval: 2000,
  });
  const running = data ?? [];
  return (
    <NavLink to="/jobs" className="mt-auto flex flex-col gap-2 p-3 border border-track rounded-md no-underline !text-fg hover:bg-track">
      <span className="flex items-center gap-2 text-xs">
        <span className={cx("size-2 rounded-full", running.length ? "bg-primary animate-pulse" : "bg-subtle")} />
        {running.length ? `${running.length} job${running.length > 1 ? "s" : ""} running` : "No jobs running"}
      </span>
      {running.slice(0, 3).map((j) => (
        <span key={j.id} className="text-[11px] text-muted num truncate" title={j.message ?? undefined}>
          {j.label} · {j.status === "running" ? `${Math.round(j.progress * 100)}%` : j.status}
        </span>
      ))}
    </NavLink>
  );
}

export function Nav() {
  const [open, setOpen] = useState(false);
  const loc = useLocation();
  useEffect(() => setOpen(false), [loc.pathname]);
  return (
    // The wrapper carries the panel background for the full page height; the nav itself is sticky.
    <div className="bg-panel border-b lg:border-b-0 lg:border-r border-track lg:w-[216px] lg:shrink-0">
    <nav aria-label="Primary" className="px-3 py-4 lg:py-5 flex flex-col gap-6 lg:sticky lg:top-0 lg:h-screen lg:overflow-y-auto scroll-thin">
      <div className="flex items-center gap-2.5 px-2">
        <span className="text-primary">
          <I.Logo size={22} />
        </span>
        <div className="flex flex-col">
          <span className="font-semibold text-[15px] tracking-tight">Concord Bench</span>
          <span className="text-[11px] text-muted">Speech model benchmarks</span>
        </div>
        <button
          type="button"
          aria-expanded={open}
          aria-controls="nav-groups"
          onClick={() => setOpen((o) => !o)}
          className="lg:hidden ml-auto min-h-10 px-3 rounded-md border border-line bg-transparent text-fg text-[13px] cursor-pointer"
        >
          {open ? "Close" : "Menu"}
        </button>
      </div>
      <div id="nav-groups" className={cx("flex-col gap-6 flex-1", open ? "flex" : "hidden lg:flex")}>
      <Group title="Evaluate">
        <Item to="/benchmarks" icon={<I.Chart />}>Benchmarks</Item>
        <Item to="/errors" icon={<I.Bug />}>Error analysis</Item>
      </Group>
      <Group title="Bring your own">
        <Item to="/datasets/upload" icon={<I.Upload />}>Upload dataset</Item>
        <Item to="/datasets/import" icon={<I.Download />}>Import references</Item>
        <Item to="/datasets" icon={<I.Database />}>Datasets</Item>
        <Item to="/models/add" icon={<I.Plus />}>Add model</Item>
        <Item to="/models" icon={<I.Cpu />}>Models</Item>
        <Item to="/contract" icon={<I.Doc />}>Model contract</Item>
        <Item to="/offline" icon={<I.Download />}>Export for offline</Item>
      </Group>
      <Jobs />
      </div>
    </nav>
    </div>
  );
}
