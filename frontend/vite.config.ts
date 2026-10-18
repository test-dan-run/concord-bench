/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const api = `http://127.0.0.1:${env.CONCORD_PORT || process.env.CONCORD_PORT || 8600}`;
  return {
    plugins: [react(), tailwindcss()],
    // Build-time variant switch: "0" drops the Improve chunk from the lite build entirely.
    define: { __FEATURE_IMPROVE__: JSON.stringify((env.VITE_FEATURE_IMPROVE ?? "1") === "1") },
    server: { proxy: { "/api": { target: api, changeOrigin: false } } },
    preview: { proxy: { "/api": { target: api } } },
    build: { outDir: "dist", sourcemap: true, chunkSizeWarningLimit: 900 },
    test: { environment: "jsdom", include: ["src/**/*.test.{ts,tsx}"], globals: false },
  };
});
