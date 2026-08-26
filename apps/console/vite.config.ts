import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // The CRDT is consumed as source, not as a build artifact. It keeps the type
      // boundary honest (a change breaks the console's typecheck immediately) and means
      // there is no stale-dist failure mode in a monorepo this small.
      "@rainmaker/crdt": fileURLToPath(new URL("../../packages/crdt/src/index.ts", import.meta.url)),
    },
  },
  // The same proxy for dev and preview. `server.proxy` does NOT apply to `vite preview`,
  // so a build verified only through the dev server can still 404 every API call when
  // someone previews it -- which is exactly the state a reviewer sees first.
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true, ws: true } },
  },
  preview: {
    host: "127.0.0.1",
    port: 5174,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true, ws: true } },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    // TWO PAGES, ONE BUILD. `index.html` is the console a rep uses; `embed.html` is what loads
    // inside an iframe on a customer's own website. Separate entries rather than one bundle
    // with a flag: the widget must not carry the pipeline board, and the console must not be
    // reshaped by the widget's stylesheet.
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL("index.html", import.meta.url)),
        embed: fileURLToPath(new URL("embed.html", import.meta.url)),
      },
    },
  },
});
