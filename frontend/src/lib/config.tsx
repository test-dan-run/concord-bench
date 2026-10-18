import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, type AppConfig, type Normaliser } from "./api";

export function useConfig() {
  return useQuery({ queryKey: ["config"], queryFn: () => api.get<AppConfig>("/config"), staleTime: Infinity });
}

export function useNormalisers() {
  return useQuery({ queryKey: ["normalisers"], queryFn: () => api.get<Normaliser[]>("/normalisers"), staleTime: 60_000 });
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
