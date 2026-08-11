import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by FastAPI at / (with /studio and /desk aliases) in
// production; the relative base lets the same build work at every
// mount. The dev server proxies API calls to the backend on 8899.
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: {
    port: 5180,
    proxy: {
      "/resumes": "http://localhost:8899",
      "/job-profiles": "http://localhost:8899",
      "/settings": "http://localhost:8899",
    },
  },
});
