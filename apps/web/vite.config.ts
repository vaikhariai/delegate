import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API (services/api) listens on 8011 in development. Tests point DELEGATE_API_URL at their own backend.
const api = process.env.DELEGATE_API_URL ?? "http://127.0.0.1:8011";
const proxy = Object.fromEntries(
  ["/v1", "/healthz", "/readyz"].map((p) => [p, { target: api, changeOrigin: false }]),
);

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy },
  preview: { port: 4173, proxy },
  build: { outDir: "dist", sourcemap: false, target: "es2022" },
});
