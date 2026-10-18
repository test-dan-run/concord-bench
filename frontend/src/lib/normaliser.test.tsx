import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { resetNormaliserChoice, useNormaliser } from "./config";

const NORMS = ["basic", "whisper_en", "none"].map((id) => ({ id, key: `${id}@1`, label: id, description: "", default: id === "basic", service: null }));

function wrapper(url: string) {
  const qc = new QueryClient();
  qc.setQueryData(["config"], { default_normaliser: "basic" });
  qc.setQueryData(["normalisers"], NORMS);
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe("useNormaliser", () => {
  beforeEach(() => resetNormaliserChoice());

  it("defaults to the server default", () => {
    const { result } = renderHook(() => useNormaliser(), { wrapper: wrapper("/benchmarks/asr") });
    expect(result.current[0]).toBe("basic");
  });

  it("a choice made on one page holds on another page without a query string", () => {
    const a = renderHook(() => useNormaliser(), { wrapper: wrapper("/benchmarks/asr") });
    act(() => a.result.current[1]("whisper_en"));
    expect(a.result.current[0]).toBe("whisper_en");
    const b = renderHook(() => useNormaliser(), { wrapper: wrapper("/errors/asr/3") });
    expect(b.result.current[0]).toBe("whisper_en");
  });

  it("the URL wins, versioned keys included, and becomes the choice", () => {
    const a = renderHook(() => useNormaliser(), { wrapper: wrapper("/errors/asr/3?normaliser=none@1") });
    expect(a.result.current[0]).toBe("none");
    const b = renderHook(() => useNormaliser(), { wrapper: wrapper("/benchmarks/asr") });
    expect(b.result.current[0]).toBe("none");
  });

  it("a remembered normaliser the server no longer offers falls back to the default", () => {
    const a = renderHook(() => useNormaliser(), { wrapper: wrapper("/benchmarks/asr") });
    act(() => a.result.current[1]("platform:gone"));
    const b = renderHook(() => useNormaliser(), { wrapper: wrapper("/errors/asr/3") });
    expect(b.result.current[0]).toBe("basic");
  });
});
