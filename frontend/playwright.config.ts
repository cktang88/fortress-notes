import { defineConfig } from "@playwright/test";
import { mkdtemp } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";

async function freePort(): Promise<number> {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") return reject(new Error("No free port"));
      const { port } = address;
      server.close((error) => (error ? reject(error) : resolve(port)));
    });
  });
}

const backendPort = Number(process.env.FORTRESS_E2E_BACKEND_PORT ?? (await freePort()));
const frontendPort = Number(process.env.FORTRESS_E2E_FRONTEND_PORT ?? (await freePort()));
process.env.FORTRESS_E2E_BACKEND_PORT = String(backendPort);
process.env.FORTRESS_E2E_FRONTEND_PORT = String(frontendPort);
const notesDir = await mkdtemp(path.join(os.tmpdir(), "fortress-notes-e2e-"));
process.env.FORTRESS_E2E_NOTES_DIR = notesDir;
const backendUrl = `http://127.0.0.1:${backendPort}`;
const frontendUrl = `http://127.0.0.1:${frontendPort}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  // Every spec shares one backend and database, so run them one at a time.
  workers: 1,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: frontendUrl,
    browserName: "chromium",
    launchOptions: { headless: true },
    permissions: ["clipboard-read", "clipboard-write"],
  },
  webServer: [
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${frontendPort} --strictPort`,
      cwd: process.cwd(),
      url: frontendUrl,
      stdout: "pipe",
      stderr: "pipe",
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `uv run uvicorn app.main:app --host 127.0.0.1 --port ${backendPort}`,
      cwd: path.resolve("../backend"),
      url: `${backendUrl}/api/health`,
      env: {
        NOTES_DIR: notesDir,
        EMBEDDINGS_ENABLED: "false",
        VISION_ENABLED: "false",
        VLM_CAPTION_ENABLED: "false",
        BLOCK_DB_IMPORT_ON_STARTUP: "false",
        // Never call the real AI service from tests, even if backend/.env has a key.
        OPENROUTER_API_KEY: "",
      },
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
  globalTeardown: "./e2e/teardown.ts",
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  expect: { timeout: 10_000 },
  timeout: 45_000,
  metadata: { backendUrl },
});
