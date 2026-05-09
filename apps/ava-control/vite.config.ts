import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
  },
  clearScreen: false,
  build: {
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (id.includes("node_modules/three")) return "vendor-three";
          if (id.includes("node_modules/d3") || id.includes("node_modules/d3-")) return "vendor-d3";
          if (id.includes("node_modules/@tauri-apps")) return "vendor-tauri";
          if (id.includes("OrbCanvas")) return "app-orb";
        },
      },
    },
  },
});
