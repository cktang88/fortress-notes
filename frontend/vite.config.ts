import { defineConfig } from "vite-plus";
import babel from "@rolldown/plugin-babel";
import react, { reactCompilerPreset } from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), babel({ presets: [reactCompilerPreset()] }), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/media": "http://127.0.0.1:8000",
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
