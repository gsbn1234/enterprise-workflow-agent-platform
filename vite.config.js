import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  root: "frontend",
  base: "/static/react/",
  plugins: [react()],
  build: {
    outDir: "../app/static/react",
    emptyOutDir: false,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8010",
    },
  },
});
