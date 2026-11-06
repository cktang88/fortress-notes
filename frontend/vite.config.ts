import { defineConfig } from "vite-plus";
import babel from "@rolldown/plugin-babel";
import react, { reactCompilerPreset } from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The e2e tests run their own backend; everyone else uses the default port.
const backendUrl = process.env.FORTRESS_BACKEND_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), babel({ presets: [reactCompilerPreset()] }), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": backendUrl,
      "/media": backendUrl,
    },
  },
  // Vitest (vp test)
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    setupFiles: ["src/test.setup.ts"],
  },
  // oxlint (vp lint)
  lint: {
    ignorePatterns: ["dist/**"],
  },
  // oxfmt (vp fmt)
  fmt: {
    semi: true,
    singleQuote: false,
  },
  // pre-commit / staged-file workflow (vp check --fix on staged files)
  staged: {
    "*": "vp check --fix",
  },
});
