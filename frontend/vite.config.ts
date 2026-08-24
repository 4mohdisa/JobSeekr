import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Tailwind v4 is CSS-first: no tailwind.config.js, no postcss config. The
// theme lives in src/index.css behind @theme.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // The dashboard talks to FastAPI on 127.0.0.1:8000. Proxying keeps the
    // frontend origin-relative so there is no CORS story in development and
    // none in production either, where both are served from localhost.
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
});
