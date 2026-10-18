// Waveform (server-computed peaks on canvas, never recoloured per speaker) + per-speaker lanes,
// an error lane and the overlap hatch in SVG, a playhead, and click-to-seek.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type ErrorRegion, type Peaks } from "../lib/api";
import { C, ERROR_COLORS } from "../lib/colors";
import { clock } from "../lib/format";
import { clipSegments, niceTicks, peakColumns, tToX, xToT, type View } from "./timelineMath";

export interface LaneDef {
  name: string;
  color: string;
  segs: [number, number][];
}

export interface LaneGroup {
  title: string;
  lanes: LaneDef[];
}

export interface Marker {
  t0: number;
  t1: number;
  color: string;
  label?: string;
}

const LANE_H = 14;
const LANE_GAP = 4;
const WAVE_H = 56;
const LABEL_W = 118;

function useWidth(ref: React.RefObject<HTMLDivElement | null>): number {
  const [w, setW] = useState(600);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(200, Math.floor(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}

export function Timeline({
  fileId,
  t0,
  t1,
  groups = [],
  errors,
  overlaps,
  markers,
  time,
  onSeek,
  showWave = true,
}: {
  fileId: number;
  t0: number;
  t1: number;
  groups?: LaneGroup[];
  errors?: ErrorRegion[];
  overlaps?: { start: number; end: number }[];
  markers?: Marker[];
  time?: number;
  onSeek?: (t: number) => void;
  showWave?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  const full = useWidth(box);
  const width = Math.max(100, full - LABEL_W);
  const view: View = useMemo(() => ({ t0, t1, width }), [t0, t1, width]);
  const peaks = useQuery({
    queryKey: ["peaks", fileId],
    queryFn: () => api.get<Peaks>(`/audio/${fileId}/peaks`),
    staleTime: Infinity,
    enabled: showWave,
  });
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const cv = canvas.current;
    if (!cv || !peaks.data) return;
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.floor(width * dpr);
    cv.height = Math.floor(WAVE_H * dpr);
    const g = cv.getContext("2d");
    if (!g) return;
    g.scale(dpr, dpr);
    g.clearRect(0, 0, width, WAVE_H);
    g.fillStyle = C.subtle;
    const mid = WAVE_H / 2;
    const cols = peakColumns(peaks.data.min, peaks.data.max, peaks.data.per_second, view);
    cols.forEach((c, x) => {
      const top = mid - c.hi * mid * 0.95;
      const h = Math.max(1, (c.hi - c.lo) * mid * 0.95);
      g.fillRect(x, top, 1, h);
    });
  }, [peaks.data, view, width]);

  const rows: { kind: "title" | "lane" | "errors"; title?: string; lane?: LaneDef }[] = [];
  for (const grp of groups) {
    rows.push({ kind: "title", title: grp.title });
    for (const l of grp.lanes) rows.push({ kind: "lane", lane: l });
  }
  if (errors) rows.push({ kind: "errors" });
  const rowH = (r: (typeof rows)[number]) => (r.kind === "title" ? 16 : LANE_H + LANE_GAP);
  const lanesH = rows.reduce((a, r) => a + rowH(r), 0);
  const overlapRects = clipSegments((overlaps ?? []).map((o) => [o.start, o.end]), view);
  const ticks = niceTicks(t0, t1, Math.max(3, Math.floor(width / 90)));
  const playX = time !== undefined && time >= t0 && time <= t1 ? tToX(time, view) : null;

  const seek = (e: React.MouseEvent) => {
    if (!onSeek) return;
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    onSeek(Math.max(t0, Math.min(t1, xToT(e.clientX - rect.left, view))));
  };

  let y = 0;
  return (
    <div ref={box} className="w-full select-none">
      <div className="flex">
        <div style={{ width: LABEL_W }} className="shrink-0" />
        <div className="relative" style={{ width }}>
          <svg width={width} height={16} className="block" aria-hidden>
            {ticks.map((t) => (
              <g key={t} transform={`translate(${tToX(t, view)},0)`}>
                <line y1={11} y2={16} stroke={C.line} />
                <text y={9} fontSize={9} fill={C.subtle} textAnchor="middle" fontFamily="JetBrains Mono Variable, monospace">
                  {clock(t, false)}
                </text>
              </g>
            ))}
          </svg>
        </div>
      </div>
      {showWave && (
        <div className="flex items-center">
          <div style={{ width: LABEL_W }} className="shrink-0 text-[10px] text-subtle uppercase tracking-wider">
            Waveform
          </div>
          <div className="relative cursor-pointer bg-canvas rounded" style={{ width, height: WAVE_H }} onClick={seek} role="presentation">
            <canvas ref={canvas} style={{ width, height: WAVE_H }} className="block" />
            {(markers ?? []).map((m, i) => {
              const r = clipSegments([[m.t0, m.t1]], view)[0];
              return r ? (
                <div key={i} className="absolute top-0 bottom-0 pointer-events-none" style={{ left: r.x, width: r.w, background: `${m.color}33`, borderLeft: `1px solid ${m.color}` }}>
                  {m.label && (
                    <span className="absolute top-0.5 left-0.5 text-[9px] num font-semibold" style={{ color: m.color }}>
                      {m.label}
                    </span>
                  )}
                </div>
              ) : null;
            })}
            {playX !== null && <div className="absolute top-0 bottom-0 w-px bg-focus pointer-events-none" style={{ left: playX }} />}
          </div>
        </div>
      )}
      {rows.length > 0 && (
        <div className="flex mt-1.5">
          <div style={{ width: LABEL_W }} className="shrink-0 relative" aria-hidden>
            {rows.map((r, i) => {
              const top = y;
              y += rowH(r);
              if (r.kind === "title")
                return (
                  <span key={i} className="absolute text-[10px] text-subtle uppercase tracking-wider truncate whitespace-nowrap" style={{ top, maxWidth: LABEL_W - 6 }} title={r.title}>
                    {r.title}
                  </span>
                );
              const label = r.kind === "errors" ? "Errors" : r.lane!.name;
              return (
                <span key={i} className="absolute flex items-center gap-1.5 text-[11px] text-muted truncate num" style={{ top, height: LANE_H, maxWidth: LABEL_W - 6 }} title={label}>
                  {r.kind === "lane" && <span className="size-2 rounded-sm shrink-0" style={{ background: r.lane!.color }} />}
                  <span className="truncate">{label}</span>
                </span>
              );
            })}
          </div>
          <svg width={width} height={lanesH} className="block cursor-pointer" onClick={seek} role="img" aria-label="Speaker and error lanes">
            {(() => {
              let yy = 0;
              return rows.map((r, i) => {
                const top = yy;
                yy += rowH(r);
                if (r.kind === "title") return null;
                const bg = <rect key={`bg${i}`} x={0} y={top} width={width} height={LANE_H} fill={C.track} rx={2} />;
                if (r.kind === "errors") {
                  return (
                    <g key={i}>
                      {bg}
                      {(errors ?? []).map((er, k) =>
                        clipSegments([[er.start, er.end]], view).map((rc, j) => (
                          <rect key={`${k}-${j}`} x={rc.x} y={top} width={rc.w} height={LANE_H} fill={ERROR_COLORS[er.type]} rx={1}>
                            <title>{`${er.type.replace("_", " ")} ${er.start.toFixed(2)}–${er.end.toFixed(2)} s`}</title>
                          </rect>
                        )),
                      )}
                    </g>
                  );
                }
                return (
                  <g key={i}>
                    {bg}
                    {clipSegments(r.lane!.segs, view).map((rc, j) => (
                      <rect key={j} x={rc.x} y={top + 1} width={rc.w} height={LANE_H - 2} fill={r.lane!.color} rx={2} opacity={0.9}>
                        <title>{`${r.lane!.name} ${rc.s.toFixed(2)}–${rc.e.toFixed(2)} s`}</title>
                      </rect>
                    ))}
                    {overlapRects.map((rc, j) => (
                      <rect key={`o${j}`} x={rc.x} y={top} width={rc.w} height={LANE_H} fill="url(#overlap-hatch)" />
                    ))}
                  </g>
                );
              });
            })()}
            <defs>
              <pattern id="overlap-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
                <rect width="2" height="6" fill={C.overlap} opacity="0.55" />
              </pattern>
            </defs>
            {playX !== null && <line x1={playX} x2={playX} y1={0} y2={lanesH} stroke={C.focus} />}
          </svg>
        </div>
      )}
    </div>
  );
}

export function ErrorLegend({ overlap = true }: { overlap?: boolean }) {
  return (
    <span className="flex flex-wrap gap-3 text-[11px] text-muted">
      {Object.entries({ missed: "Missed speech", false_alarm: "False alarm", confusion: "Confusion" }).map(([k, v]) => (
        <span key={k} className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-sm" style={{ background: ERROR_COLORS[k] }} />
          {v}
        </span>
      ))}
      {overlap && (
        <span className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-sm hatch" />
          Overlap
        </span>
      )}
    </span>
  );
}
