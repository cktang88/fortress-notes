import { defineConfig } from "vite-plus";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react({ babel: { plugins: ["babel-plugin-react-compiler"] } }), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
  // Vitest (vp test)
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
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
