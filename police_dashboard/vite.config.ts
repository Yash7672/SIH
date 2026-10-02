import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    host: true,
    // Fail loudly instead of silently moving to 5175, which would break the
    // printed URLs (and the firewall rules added by scripts/start.ps1).
    strictPort: true,
  },
  preview: {
    host: true,
    strictPort: true,
  },
  build: {
    // Modern browsers only: everything this app targets is evergreen, and the
    // smaller output parses faster on the older tablets in police stations.
    target: "es2020",
    cssTarget: "chrome87",
    sourcemap: false,
    reportCompressedSize: true,
    // Split the heavy map/chart libraries out of the entry chunk so the
    // dashboard shell paints without downloading them.
    rollupOptions: {
      output: {
        // lucide-react is grouped here too: left alone, the icons split into a
        // dozen sub-kilobyte requests, which costs more in round trips than it
        // saves in bytes.
        manualChunks: {
          "vendor-react": ["react", "react-dom", "react-router-dom"],
          "vendor-icons": ["lucide-react"],
          "vendor-maps": ["leaflet", "react-leaflet"],
          "vendor-charts": ["recharts"],
        },
      },
    },
  },
});
