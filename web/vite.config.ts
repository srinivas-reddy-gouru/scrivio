import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by FastAPI at / (with /studio and /desk aliases) in
// production; the relative base lets the same build work at every
// mount. The dev server proxies API calls to the backend.
//
// The list is every root path the API answers on. It used to name three
// of them, so in development the article, interview, job, and audio
// requests were answered by Vite with the app's own index.html and
// status 200, which the client then tried to read as data.
// tests/test_dev_proxy.py fails if a route is added and this is not.
const BACKEND = process.env.SCRIVIO_BACKEND ?? "http://localhost:8899";

export const API_ROUTES = [
  "/auth", "/health", "/mode", "/settings",
  "/generate", "/clarify", "/jobs", "/articles",
  "/interviews", "/job-profiles", "/resumes",
  "/transcribe", "/speak",
];

export default defineConfig({
  plugins: [react()],
  base: "./",
  server: {
    port: 5180,
    proxy: Object.fromEntries(API_ROUTES.map((route) => [route, {
      target: BACKEND,
      // Progress streams are long-lived and must not be buffered or timed
      // out by the proxy: a stream that stalls here looks like a dead job.
      timeout: 0,
      proxyTimeout: 0,
    }])),
  },
});
