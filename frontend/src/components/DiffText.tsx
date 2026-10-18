// Word diff from an alignment: insertions emerald "+tok", deletions rose strike-through "−tok",
// substitutions amber "ref → hyp" (AeroAudio §7).

import type { AlignOp } from "../lib/api";
import { cx } from "./ui";

export type DiffMode = "inline" | "ref" | "hyp";

export interface DiffToken {
  kind: "eq" | "sub" | "del" | "ins";
  text: string;
  ref?: string | null;
  hyp?: string | null;
}

/** Tokens to render for one side (or both, inline). Pure, unit-tested. */
export function diffTokens(ops: AlignOp[], mode: DiffMode): DiffToken[] {
  const out: DiffToken[] = [];
  for (const o of ops) {
    if (o.op === "eq") out.push({ kind: "eq", text: (mode === "hyp" ? o.hyp : o.ref) ?? "" });
    else if (o.op === "sub") {
      if (mode === "inline") out.push({ kind: "sub", text: `${o.ref} → ${o.hyp}`, ref: o.ref, hyp: o.hyp });
      else out.push({ kind: "sub", text: (mode === "ref" ? o.ref : o.hyp) ?? "", ref: o.ref, hyp: o.hyp });
    } else if (o.op === "del") {
      if (mode !== "hyp") out.push({ kind: "del", text: mode === "inline" ? `−${o.ref}` : (o.ref ?? "") });
    } else if (mode !== "ref") out.push({ kind: "ins", text: mode === "inline" ? `+${o.hyp}` : (o.hyp ?? "") });
  }
  return out;
}

const CLASS: Record<DiffToken["kind"], string> = {
  eq: "text-fg",
  sub: "text-sub bg-sub/10 rounded-sm px-0.5",
  del: "text-del line-through decoration-del/70 bg-del/10 rounded-sm px-0.5",
  ins: "text-ins bg-ins/10 rounded-sm px-0.5",
};

const TITLE: Record<DiffToken["kind"], string> = { eq: "", sub: "substitution", del: "deletion", ins: "insertion" };

export function DiffText({ ops, mode = "inline", className, highlight }: { ops: AlignOp[]; mode?: DiffMode; className?: string; highlight?: string }) {
  const toks = diffTokens(ops, mode);
  if (!toks.length) return <span className="text-subtle italic">(empty)</span>;
  return (
    <span className={cx("leading-relaxed", className)}>
      {toks.map((t, i) => (
        <span key={i}>
          <span
            className={cx("num text-[12.5px]", CLASS[t.kind], highlight && (t.ref === highlight || t.text === highlight) && "outline outline-1 outline-focus")}
            title={TITLE[t.kind] || undefined}
          >
            {t.text}
          </span>{" "}
        </span>
      ))}
    </span>
  );
}

/** Two-row REF / HYP alignment grid (expanded rows). */
export function AlignmentGrid({ ops }: { ops: AlignOp[] }) {
  return (
    <div className="flex gap-2 overflow-x-auto scroll-thin pb-1">
      <div className="flex flex-col gap-1 text-[10px] text-subtle uppercase tracking-wider pt-0.5 shrink-0">
        <span className="h-5 leading-5">Ref</span>
        <span className="h-5 leading-5">Hyp</span>
      </div>
      <div className="flex gap-1">
        {ops.map((o, i) => (
          <span key={i} className={cx("flex flex-col gap-1 rounded px-1", o.op !== "eq" && "bg-track")}>
            <span className={cx("h-5 leading-5 num text-xs whitespace-nowrap", o.op === "del" && "text-del", o.op === "sub" && "text-sub", !o.ref && "text-subtle")}>
              {o.ref ?? "·"}
            </span>
            <span className={cx("h-5 leading-5 num text-xs whitespace-nowrap", o.op === "ins" && "text-ins", o.op === "sub" && "text-sub", !o.hyp && "text-subtle")}>
              {o.hyp ?? "·"}
            </span>
          </span>
        ))}
      </div>
    </div>
  );
}
