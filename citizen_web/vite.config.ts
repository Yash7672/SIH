import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    // Fail loudly instead of silently moving to 5174, which would break the
    // printed URLs (and the firewall rules added by scripts/start.ps1).
    strictPort: true,
  },
  preview: {
    host: true,
    strictPort: true,
  },
  build: {
    // Modern browsers only: everything this app targets is evergreen, and the
    // smaller output parses faster on the mid-range Android phones citizens use.
    target: "es2020",
    cssTarget: "chrome87",
    sourcemap: false,
    reportCompressedSize: true,
    rollupOptions: {
      output: {
        // Vendor code changes far less often than app code, so it gets its own
        // long-lived chunk. lucide-react is grouped here too: without this the
        // icons split into a dozen sub-kilobyte requests, which costs more in
        // round trips than it saves in bytes.
        manualChunks: {
          "vendor-react": ["react", "react-dom", "react-router-dom"],
          "vendor-icons": ["lucide-react"],
        },
      },
    },
  },
});
