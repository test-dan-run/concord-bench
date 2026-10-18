// Pipeline stage colours: ASR (sky), segmentation (emerald), speaker attribution (violet).

import type { Attribution } from "../lib/api";
import { STAGE_COLORS, STAGE_LABELS } from "../lib/colors";
import { pct } from "../lib/format";

export function StageLegend() {
  return (
    <span className="flex flex-wrap gap-3 text-[11px] text-muted">
      {Object.entries(STAGE_LABELS).map(([k, v]) => (
        <span key={k} className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-sm" style={{ background: STAGE_COLORS[k] }} />
          {v}
        </span>
      ))}
    </span>
  );
}

/** Stacked bar of the A / B−A / C−B split; negatives are clipped to 0 for the bar only. */
export function AttributionBar({ att, height = 10 }: { att: Attribution; height?: number }) {
  const parts = (["asr", "segmentation", "speaker"] as const).map((k) => ({ k, v: Math.max(0, att[k] ?? 0) }));
  const total = parts.reduce((a, p) => a + p.v, 0) || 1;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex w-full rounded overflow-hidden bg-track" style={{ height }} role="img" aria-label="Error attribution">
        {parts.map((p) => (
          <span key={p.k} style={{ width: `${(p.v / total) * 100}%`, background: STAGE_COLORS[p.k] }} title={`${STAGE_LABELS[p.k]} ${pct(att[p.k])}`} />
        ))}
      </div>
      <div className="flex flex-col gap-1">
        {parts.map((p) => (
          <div key={p.k} className="flex items-center justify-between text-[13px]">
            <span className="flex items-center gap-2 text-muted">
              <span className="size-2.5 rounded-sm" style={{ background: STAGE_COLORS[p.k] }} />
              {STAGE_LABELS[p.k]}
            </span>
            <span className="num">
              {pct(att[p.k])} <span className="text-subtle">· {att[`${p.k}_errors` as "asr_errors"]} words</span>
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
