import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by FastAPI at /desk in production; the dev server proxies API
// calls to the running backend on 8899.
export default defineConfig({
  plugins: [react()],
  base: "/desk/",
  server: {
    port: 5180,
    proxy: {
      "/resumes": "http://localhost:8899",
      "/job-profiles": "http://localhost:8899",
      "/settings": "http://localhost:8899",
    },
  },
});
