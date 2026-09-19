/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by the API at /ui (agentos/api/ui.py), so every asset URL is under that base. In
// development `vite` proxies API calls to a local API process so the browser stays same-origin.
export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "^/(approvals|runs|me|health|agents|workflows|policy|executors)(/.*)?$": "http://127.0.0.1:8000",
    },
  },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    css: false,
  },
});
