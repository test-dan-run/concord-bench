import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useSyncExternalStore, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type AppConfig, type Normaliser } from "./api";

export function useConfig() {
  return useQuery({ queryKey: ["config"], queryFn: () => api.get<AppConfig>("/config"), staleTime: Infinity });
}

export function useNormalisers() {
  return useQuery({ queryKey: ["normalisers"], queryFn: () => api.get<Normaliser[]>("/normalisers"), staleTime: 60_000 });
}

// ---------------------------------------------------------------- the text normaliser, shared by every page

const NORMALISER_KEY = "concord.normaliser";
const listeners = new Set<() => void>();

function readStored(): string | null {
  try {
    return localStorage.getItem(NORMALISER_KEY);
  } catch {
    return null;
  }
}

let chosen: string | null = readStored();

function remember(id: string): void {
  if (id === chosen) return;
  chosen = id;
  try {
    localStorage.setItem(NORMALISER_KEY, id);
  } catch {
    /* private window or blocked storage: the choice still holds for this session */
  }
  listeners.forEach((l) => l());
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** Test hook: forget the remembered choice. */
export function resetNormaliserChoice(): void {
  chosen = null;
  try {
    localStorage.removeItem(NORMALISER_KEY);
  } catch {
    /* ignore */
  }
  listeners.forEach((l) => l());
}

/**
 * The text normaliser chosen anywhere in the app (Benchmarks, Error analysis, Live evaluation), as a
 * normaliser id. A `?normaliser=` in the URL wins (so shared links keep theirs) and becomes the choice;
 * otherwise the last choice made on any page, kept across reloads; otherwise the server default. A
 * remembered id the server no longer offers falls back to the default.
 */
export function useNormaliser(): [string, (id: string) => void] {
  const [params, setParams] = useSearchParams();
  const { data: cfg } = useConfig();
  const { data: norms } = useNormalisers();
  const remembered = useSyncExternalStore(subscribe, () => chosen);
  const fromUrl = params.get("normaliser")?.split("@")[0] || null;
  useEffect(() => {
    if (fromUrl) remember(fromUrl);
  }, [fromUrl]);
  const fallback = cfg?.default_normaliser ?? "basic";
  let value = fromUrl ?? remembered ?? fallback;
  if (!fromUrl && norms && !norms.some((n) => n.id === value)) value = fallback;
  const set = useCallback(
    (id: string) => {
      remember(id);
      setParams(
        (prev) => {
          const p = new URLSearchParams(prev);
          p.set("normaliser", id);
          return p;
        },
        { replace: true },
      );
    },
    [setParams],
  );
  return [value, set];
}

/** Improve is on only when this build includes it AND the server mounted its router. */
export function useFeature(name: "improve"): boolean {
  const { data } = useConfig();
  if (name === "improve") return __FEATURE_IMPROVE__ && !!data?.features.improve;
  return false;
}

/** The single wrapper for everything Improve-related (nav item, "Use for retraining", ...). */
export function IfFeature({ name, children }: { name: "improve"; children: ReactNode }) {
  return useFeature(name) ? <>{children}</> : null;
}
