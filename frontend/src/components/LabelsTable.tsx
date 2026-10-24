// Labels for slicing (EVALUATE_PLAN.md §7.1): the extra fields that came with the references, with their kind,
// values and coverage. Used on the upload page (what was found) and on a dataset's page (read-only).

import type { LabelSummary } from "../lib/api";
import { cx } from "./ui";

function fmt(x: number): string {
  return Number.isInteger(x) ? String(x) : x.toPrecision(3);
}

export function LabelsTable({ labels, unit = "records" }: { labels: LabelSummary[]; unit?: string }) {
  if (!labels.length) return <span className="text-[13px] text-muted">No labels in the references. Extra fields on each record, e.g. a NeMo manifest's "attribute" object, become labels to slice scores by.</span>;
  return (
    <div className="overflow-x-auto scroll-thin">
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr className="text-muted text-left">
            <th scope="col" className="font-medium py-1 pr-3">Label</th>
            <th scope="col" className="font-medium py-1 pr-3">Applies to</th>
            <th scope="col" className="font-medium py-1 pr-3">Values</th>
            <th scope="col" className="font-medium py-1 text-right">{unit === "records" ? "Records with a value" : "Coverage"}</th>
          </tr>
        </thead>
        <tbody>
          {labels.map((l) => {
            const skip = l.kind === "unused";
            return (
              <tr key={l.key} className="border-t border-track align-top">
                <td className={cx("py-1.5 pr-3 num whitespace-nowrap", skip ? "text-subtle line-through" : "text-fg")}>{l.key}</td>
                <td className="py-1.5 pr-3 text-muted whitespace-nowrap">
                  {skip ? "not used" : (
                    <span className="flex flex-wrap gap-1">
                      <span className="border border-line rounded px-1.5">{l.level}</span>
                      <span className="border border-line rounded px-1.5">{l.kind}</span>
                    </span>
                  )}
                </td>
                <td className="py-1.5 pr-3">
                  {skip ? (
                    <span className="text-subtle">{l.reason}</span>
                  ) : l.kind === "continuous" && l.range ? (
                    <span className="num">
                      {fmt(l.range.min)} – {fmt(l.range.max)} · median {fmt(l.range.median)}
                    </span>
                  ) : (
                    <span className="flex flex-wrap gap-1">
                      {(l.values ?? []).map((v) => (
                        <span key={String(v.value)} className="num inline-flex gap-1.5 bg-track rounded px-1.5 py-px whitespace-nowrap">
                          {String(v.value)}
                          <span className="text-muted">{v.n}</span>
                        </span>
                      ))}
                      {(l.more ?? 0) > 0 && <span className="text-muted">+{l.more} more</span>}
                    </span>
                  )}
                </td>
                <td className={cx("py-1.5 text-right num", skip ? "text-subtle" : l.have < l.of ? "text-warn" : "text-fg")}>
                  {l.have} / {l.of}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
