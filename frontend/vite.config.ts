import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The built bundle is served by the FastAPI app from frontend/dist on 127.0.0.1.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
});
