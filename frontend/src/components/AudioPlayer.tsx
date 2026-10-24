// Native <audio> (the server answers range requests) with play-from, play-a-span and region markers.

import { useCallback, useEffect, useRef, useState } from "react";
import { clock } from "../lib/format";
import * as I from "./icons";
import { Button, cx } from "./ui";

export interface AudioController {
  audioRef: React.RefObject<HTMLAudioElement | null>;
  src: string;
  time: number;
  playing: boolean;
  rate: number;
  setRate: (r: number) => void;
  /** Play from `from` (default: current time) until `until` (default: the end). */
  play: (from?: number, until?: number) => void;
  /** Play several spans back to back (e.g. "play errors only"). */
  playSpans: (spans: [number, number][]) => void;
  pause: () => void;
  seek: (t: number) => void;
}

/** Playback of a file's derived audio, or of its version in an enhanced copy (`copyId`). */
export function useAudio(fileId: number | null | undefined, copyId?: number | null): AudioController {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [time, setTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [rate, setRateState] = useState(1);
  const until = useRef<number | null>(null);
  const queue = useRef<[number, number][]>([]);
  const raf = useRef<number | null>(null);

  const tick = useCallback(() => {
    const a = audioRef.current;
    if (!a) return;
    setTime(a.currentTime);
    if (until.current !== null && a.currentTime >= until.current) {
      const next = queue.current.shift();
      if (next) {
        a.currentTime = next[0];
        until.current = next[1];
      } else {
        a.pause();
        until.current = null;
      }
    }
    if (!a.paused) raf.current = requestAnimationFrame(tick);
  }, []);

  useEffect(() => {
    const a = audioRef.current;
    if (!a) return;
    const onPlay = () => {
      setPlaying(true);
      raf.current = requestAnimationFrame(tick);
    };
    const onPause = () => {
      setPlaying(false);
      if (raf.current) cancelAnimationFrame(raf.current);
      setTime(a.currentTime);
    };
    a.addEventListener("play", onPlay);
    a.addEventListener("pause", onPause);
    a.addEventListener("ended", onPause);
    return () => {
      a.removeEventListener("play", onPlay);
      a.removeEventListener("pause", onPause);
      a.removeEventListener("ended", onPause);
      if (raf.current) cancelAnimationFrame(raf.current);
    };
  }, [tick, fileId, copyId]);

  const play = useCallback((from?: number, to?: number) => {
    const a = audioRef.current;
    if (!a) return;
    queue.current = [];
    if (from !== undefined) a.currentTime = from;
    until.current = to ?? null;
    void a.play().catch(() => setPlaying(false));
  }, []);

  const playSpans = useCallback((spans: [number, number][]) => {
    const a = audioRef.current;
    if (!a || !spans.length) return;
    const [first, ...rest] = spans;
    queue.current = rest;
    a.currentTime = first[0];
    until.current = first[1];
    void a.play().catch(() => setPlaying(false));
  }, []);

  const pause = useCallback(() => audioRef.current?.pause(), []);
  const seek = useCallback((t: number) => {
    const a = audioRef.current;
    if (!a) return;
    a.currentTime = t;
    setTime(t);
  }, []);
  const setRate = useCallback((r: number) => {
    setRateState(r);
    if (audioRef.current) audioRef.current.playbackRate = r;
  }, []);

  return { audioRef, src: fileId ? `/api/audio/${fileId}${copyId ? `?copy_id=${copyId}` : ""}` : "", time, playing, rate, setRate, play, playSpans, pause, seek };
}

/** Transport bar for a span of a file (the whole file when start/end are omitted). */
export function AudioPlayer({
  ctl,
  start = 0,
  end,
  errorSpans,
  label = "Play",
}: {
  ctl: AudioController;
  start?: number;
  end?: number;
  errorSpans?: [number, number][];
  label?: string;
}) {
  const inSpan = ctl.time >= start - 0.01 && (end === undefined || ctl.time <= end + 0.01);
  const shown = inSpan ? ctl.time - start : 0;
  return (
    <div className="flex flex-wrap items-center gap-3">
      <audio ref={ctl.audioRef} src={ctl.src} preload="none" />
      <Button
        variant="primary"
        size="sm"
        className="!min-h-9 !w-9 !px-0 rounded-full"
        aria-label={ctl.playing ? "Pause" : label}
        onClick={() => (ctl.playing ? ctl.pause() : ctl.play(inSpan && ctl.time < (end ?? Infinity) - 0.05 ? ctl.time : start, end))}
      >
        {ctl.playing ? <I.Pause size={14} /> : <I.Play size={14} />}
      </Button>
      <span className="num text-xs text-muted">
        <span className="text-fg">{clock(shown)}</span> / {clock(end !== undefined ? end - start : null)}
      </span>
      <span className="flex gap-1.5">
        <Button size="sm" variant={ctl.rate === 0.75 ? "primary" : "outline"} onClick={() => ctl.setRate(ctl.rate === 0.75 ? 1 : 0.75)}>
          0.75×
        </Button>
        {errorSpans && errorSpans.length > 0 && (
          <Button size="sm" onClick={() => ctl.playSpans(errorSpans.map(([s, e]) => [Math.max(start, s - 0.3), Math.min(end ?? e + 0.3, e + 0.3)]))}>
            Play errors only
          </Button>
        )}
      </span>
    </div>
  );
}

export function PlayFrom({ ctl, t, until, label }: { ctl: AudioController; t: number; until?: number; label: string }) {
  return (
    <button
      type="button"
      aria-label={label}
      onClick={() => ctl.play(Math.max(0, t), until)}
      className={cx("inline-flex items-center justify-center size-7 rounded-full border border-line text-fg bg-transparent cursor-pointer hover:bg-track shrink-0")}
    >
      <I.Play size={10} />
    </button>
  );
}

/** A play button for one moment of a file, with its own audio element (lists of moments from many files). */
export function ClipButton({ fileId, start, end, label, copyId }: { fileId: number; start: number; end: number; label: string; copyId?: number | null }) {
  const ctl = useAudio(fileId, copyId);
  return (
    <>
      <audio ref={ctl.audioRef} src={ctl.src} preload="none" />
      <button
        type="button"
        aria-label={ctl.playing ? "Pause" : label}
        onClick={() => (ctl.playing ? ctl.pause() : ctl.play(Math.max(0, start), end))}
        className="inline-flex items-center justify-center size-7 rounded-full border border-line text-fg bg-transparent cursor-pointer hover:bg-track shrink-0"
      >
        {ctl.playing ? <I.Pause size={10} /> : <I.Play size={10} />}
      </button>
    </>
  );
}
