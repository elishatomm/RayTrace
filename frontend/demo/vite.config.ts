import { realpathSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { type Plugin, defineConfig } from "vite";

// The recorded demo: the production UI (frontend/src, unchanged) with a replay in place of the network. Built by
// scripts/demo/build-demo.sh into DEMO_OUT_DIR, a folder outside the repository; it never writes into frontend/.
const demoRoot = realpathSync(path.dirname(fileURLToPath(import.meta.url)));
const demoSource = path.join(demoRoot, "src");
const productionSource = realpathSync(path.join(demoRoot, "..", "src"));

// The demo build's only departures from the production modules: each import of the first file, from anywhere but
// frontend/demo/src, gets the second. The stand-ins import the production modules themselves.
const SWAPS: ReadonlyMap<string, string> = new Map([
  [path.join(productionSource, "api.ts"), path.join(demoSource, "demoApi.ts")], // receipt links: static files
  [path.join(productionSource, "components", "LoginPanel.tsx"), path.join(demoSource, "DemoLoginPanel.tsx")], // no token
]);

function recordedDemoSwaps(): Plugin {
  const swapped = new Set<string>();
  return {
    name: "recorded-demo-swaps",
    enforce: "pre",
    async resolveId(source, importer, options) {
      if (importer === undefined || importer.startsWith(demoSource + path.sep)) {
        return null;
      }
      const resolved = await this.resolve(source, importer, { ...options, skipSelf: true });
      const swap = resolved === null ? undefined : SWAPS.get(resolved.id.split("?")[0] ?? "");
      if (swap === undefined) {
        return null;
      }
      swapped.add(swap);
      return swap;
    },
    buildEnd(error) {
      // Fail rather than ship the production login (a token prompt) or API links that point nowhere.
      const missing = [...SWAPS.values()].filter((swap) => !swapped.has(swap));
      if (error === undefined && missing.length > 0) {
        this.error(`the demo build did not swap in ${missing.join(", ")}: a production import changed; update SWAPS`);
      }
    },
  };
}

const outDir = process.env.DEMO_OUT_DIR;
if (outDir === undefined || outDir === "") {
  throw new Error("set DEMO_OUT_DIR to a folder outside the repository (scripts/demo/build-demo.sh does)");
}

export default defineConfig({
  root: demoRoot,
  base: "./",
  publicDir: path.join(demoRoot, "public"),
  plugins: [recordedDemoSwaps(), react()],
  build: { outDir, emptyOutDir: true, sourcemap: false },
});
