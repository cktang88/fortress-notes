import { rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

export default async function teardown() {
  const notesDir = process.env.FORTRESS_E2E_NOTES_DIR;
  if (
    notesDir &&
    path.dirname(notesDir) === os.tmpdir() &&
    path.basename(notesDir).startsWith("fortress-notes-e2e-")
  ) {
    await rm(notesDir, { recursive: true, force: true });
  }
}
