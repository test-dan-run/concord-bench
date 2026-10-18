// A metric with its Δ vs a comparison. For error rates lower is better: improvement emerald ↓,
// degradation rose ↑ (AeroAudio §7).

import type { ReactNode } from "react";
import { cx } from "./ui";

export function deltaInfo(value: number | null | undefined, compare: number | null | undefined, lowerIsBetter = true) {
  if (value === null || value === undefined || compare === null || compare === undefined) return null;
  const d = value - compare;
  if (Math.abs(d) < 1e-9) return { d, better: null as boolean | null };
  return { d, better: lowerIsBetter ? d < 0 : d > 0 };
}

export function Delta({ value, compare, format, lowerIsBetter = true }: { value: number | null | undefined; compare: number | null | undefined; format: (d: number) => string; lowerIsBetter?: boolean }) {
  const info = deltaInfo(value, compare, lowerIsBetter);
  if (!info) return null;
  const arrow = info.d > 0 ? "↑" : info.d < 0 ? "↓" : "";
  return (
    <span className={cx("num text-xs", info.better === null ? "text-muted" : info.better ? "text-good" : "text-bad")}>
      {arrow}
      {format(Math.abs(info.d))}
    </span>
  );
}

export function MetricTile({
  label,
  value,
  sub,
  color,
  delta,
}: {
  label: ReactNode;
  value: ReactNode;
  sub?: ReactNode;
  color?: string;
  delta?: ReactNode;
}) {
  return (
    <div className="bg-panel border border-track rounded-lg px-4 py-3 flex flex-col gap-1.5 min-w-[140px] flex-1">
      <span className="flex items-center gap-2 text-xs text-muted">
        {color && <span className="size-2 rounded-sm" style={{ background: color }} />}
        {label}
      </span>
      <span className="flex items-baseline gap-2">
        <span className="num text-[22px] font-medium">{value}</span>
        {delta}
      </span>
      {sub && <span className="text-[11px] text-subtle">{sub}</span>}
    </div>
  );
}
