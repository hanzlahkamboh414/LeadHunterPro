import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Backend base. The dev proxy forwards /api to the FastAPI server so the
// frontend never needs CORS. Override with VITE_API_TARGET if the backend
// runs on a different host/port.
const API_TARGET = process.env.VITE_API_TARGET || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    // Recharts is a heavy dependency used ONLY by the Dashboard analytics —
    // split it into its own chunk so the login/leads screens (the daily-use
    // path) keep a small first-load bundle; the chart chunk loads in parallel
    // once the Dashboard opens.
    rollupOptions: {
      output: {
        manualChunks: { recharts: ["recharts"] },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
});
