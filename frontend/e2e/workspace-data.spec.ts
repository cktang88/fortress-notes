import { readFile } from "node:fs/promises";
import { expect, test, type Page } from "@playwright/test";
import { proxyToBackend } from "./backend";
import {
  apiUrl,
  createFolder,
  createNote,
  expectSavedText,
  noteTitle,
  openApp,
  readBlocks,
  sidebar,
  treeRow,
  uniqueWord,
} from "./notes";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

const editorContent = (page: Page) => page.locator('[aria-label="Document content"]');

async function openWorkspaceItem(page: Page, item: string) {
  await page.getByRole("button", { name: "Workspace menu" }).click();
  await page.getByRole("menuitem", { name: item }).click();
}

test("trash: undo from the toast, delete forever, and empty with confirmation", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("trs");
  const keep = await createNote(page, testInfo, { title: `Keep ${word}`, body: "Keep me." });
  const forever = await createNote(page, testInfo, { title: `Forever ${word}`, body: "Bye." });
  const later = await createNote(page, testInfo, { title: `Later ${word}`, body: "Later." });
  await openApp(page, `/?note=${keep.id}`);
  await expect(noteTitle(page)).toHaveValue(keep.title);

  // Deleting the open note closes it; Undo reopens it where it was.
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.getByText("Select a note or create a new one.")).toBeVisible();
  await expect(page).not.toHaveURL(/note=/);
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(noteTitle(page)).toHaveValue(keep.title);
  await expect(treeRow(page, keep.title)).toBeVisible();

  // Trash two from the sidebar menu (the second toast replaces the first).
  for (const note of [forever, later]) {
    await treeRow(page, note.title).click({ button: "right" });
    await page.getByRole("menuitem", { name: "Move to Trash" }).click();
    await expect(treeRow(page, note.title)).toHaveCount(0);
  }
  await expect(page.getByText(`Moved “${later.title}” to Trash`)).toBeVisible();
  await expect(noteTitle(page)).toHaveValue(keep.title);

  await openWorkspaceItem(page, "Trash");
  const trash = page.getByRole("dialog", { name: "Trash" });
  await expect(trash.getByRole("listitem").filter({ hasText: forever.title })).toContainText(
    "Bye.",
  );
  await trash.getByRole("button", { name: `Delete ${forever.title} forever` }).click();
  await expect(trash.getByRole("listitem").filter({ hasText: forever.title })).toHaveCount(0);

  // Empty trash asks first; Cancel keeps everything.
  await trash.getByRole("button", { name: "Empty trash…" }).click();
  await expect(trash).toContainText(/Permanently delete \d+ notes?\?/);
  await trash.getByRole("button", { name: "Cancel" }).click();
  await expect(trash.getByRole("listitem").filter({ hasText: later.title })).toBeVisible();
  await trash.getByRole("button", { name: "Empty trash…" }).click();
  await trash.getByRole("button", { name: "Empty trash", exact: true }).click();
  await expect(trash.getByText("Trash is empty.")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(trash).toBeHidden();

  // Gone for good: the links don't open them, and search doesn't find them.
  await page.goto(`/?note=${later.id}`);
  await expect(page.getByRole("alert")).toContainText("doesn't exist or is in the Trash");
  await page.getByRole("searchbox", { name: "Search notes" }).fill(word);
  const results = page.locator("[data-results] [data-list-item]");
  await expect(results).toHaveCount(1);
  await expect(results).toContainText(keep.title);
});

test("opening another note while a delete is on its way keeps that note open", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("dlr");
  const doomed = await createNote(page, testInfo, { title: `Doomed ${word}`, body: "Bye." });
  const next = await createNote(page, testInfo, { title: `Next ${word}`, body: "Stay." });
  await openApp(page, `/?note=${doomed.id}`);
  await expect(noteTitle(page)).toHaveValue(doomed.title);

  // Hold the trash request until the other note is open.
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route(`**/api/block-documents/${doomed.id}`, async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    await held;
    await route.fallback();
  });
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await treeRow(page, next.title).click();
  await expect(noteTitle(page)).toHaveValue(next.title);
  release();

  await expect(
    page.getByRole("status").filter({ hasText: `Moved “${doomed.title}”` }),
  ).toBeVisible();
  await expect(treeRow(page, doomed.title)).toHaveCount(0);
  await expect(noteTitle(page)).toHaveValue(next.title);
  await expect(page).toHaveURL(new RegExp(`note=${next.id}`));
});

test("Open on the Restored toast opens the note in one click while Trash is open", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("rso");
  const note = await createNote(page, testInfo, { title: `Brought back ${word}`, body: "Back." });
  await page.request.delete(`${apiUrl(testInfo)}/notes/${note.id}`);
  await openApp(page);

  await openWorkspaceItem(page, "Trash");
  const trash = page.getByRole("dialog", { name: "Trash" });
  await trash
    .getByRole("listitem")
    .filter({ hasText: note.title })
    .getByRole("button", { name: "Restore" })
    .click();
  // The dialog stays open, so several notes can be restored in a row…
  await expect(trash.getByRole("listitem").filter({ hasText: note.title })).toHaveCount(0);
  await expect(trash).toBeVisible();
  const toast = page.getByRole("status").filter({ hasText: `Restored “${note.title}”` });
  await expect(toast).toBeVisible();

  // …and a single click on Open (no click to close the dialog first) opens the note.
  await toast.getByRole("button", { name: "Open" }).click({ noWaitAfter: true, timeout: 2000 });
  await expect(noteTitle(page)).toHaveValue(note.title);
  await expect(trash).toBeHidden();
});

test("a manual backup downloads a real database, and restoring it brings back old text", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("bak");
  const note = await createNote(page, testInfo, {
    title: `Recipe ${word}`,
    body: "Two cups of flour.",
  });
  await openApp(page, `/?note=${note.id}`);
  await expect(editorContent(page)).toContainText("Two cups of flour.");

  await openWorkspaceItem(page, "Backups…");
  const backups = page.getByRole("dialog", { name: "Backups" });
  const download = page.waitForEvent("download");
  await backups.getByRole("button", { name: "Back up now & download" }).click();
  const file = await download;
  expect(file.suggestedFilename()).toMatch(/^fortress-.*-manual\.sqlite3$/);
  const bytes = await readFile((await file.path())!);
  expect(bytes.subarray(0, 16).toString("latin1")).toBe("SQLite format 3\u0000");
  await backups.getByRole("button", { name: "Close" }).click();

  // Change the note after the backup.
  await page.locator(`.bn-block[data-id="${note.blocks[0].id}"]`).click();
  await page.keyboard.press("End");
  await page.keyboard.type(" And a pinch of salt.");
  await expectSavedText(page, testInfo, note.id, "Two cups of flour. And a pinch of salt.");
  await noteTitle(page).fill(`Bread ${word}`);
  await noteTitle(page).press("Enter");
  await expect(treeRow(page, `Bread ${word}`)).toBeVisible();

  // Restore the backup while the note is still open.
  await openWorkspaceItem(page, "Backups…");
  const backupRow = backups.getByRole("listitem").filter({ hasText: "Manual" }).first();
  await backupRow.getByRole("button", { name: "Restore…" }).click();
  await backupRow.getByRole("button", { name: "Replace current notes" }).click();
  await expect(backups).toBeHidden();
  await expect(page.getByRole("status").filter({ hasText: "Restored notes from" })).toBeVisible();

  // The open editor shows the restored text and doesn't write the newer text back.
  await expect(editorContent(page)).toContainText("Two cups of flour.");
  await expect(editorContent(page)).not.toContainText("pinch of salt");
  await expect(noteTitle(page)).toHaveValue(note.title);
  await expect(treeRow(page, note.title)).toBeVisible();
  // Wait past the editor's save delay: nothing may be written back.
  await page.waitForTimeout(1500);
  expect((await readBlocks(page, testInfo, note.id)).map((block) => block.text)).toEqual([
    "Two cups of flour.",
  ]);
  await page.reload();
  await expect(editorContent(page)).toContainText("Two cups of flour.");
  await expect(editorContent(page)).not.toContainText("pinch of salt");

  // The restore made its own safety backup first.
  await openWorkspaceItem(page, "Backups…");
  await expect(backups.getByText(/Before restore ·/).first()).toBeVisible();
  await backups.getByRole("button", { name: "Close" }).click();

  // The restored note is editable as usual.
  await page.locator(`.bn-block[data-id="${note.blocks[0].id}"]`).click();
  await page.keyboard.press("End");
  await page.keyboard.type(" Bake.");
  await expectSavedText(page, testInfo, note.id, "Two cups of flour. Bake.");
});

test("Markdown files import as notes, one or several at a time", async ({ page }) => {
  const word = uniqueWord("imp");
  await openApp(page);

  // One file: its front-matter title is used and the note opens.
  let chooser = page.waitForEvent("filechooser");
  await openWorkspaceItem(page, "Import Markdown files…");
  await (
    await chooser
  ).setFiles({
    name: "ignored-name.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(
      `---\ntitle: Imported ${word}\nstatus: polished\n---\n# Overview\n\nFirst paragraph ${word}.\n\n- one\n- two\n`,
    ),
  });
  await expect(page.getByRole("status").filter({ hasText: "Imported 1 note" })).toBeVisible();
  await expect(noteTitle(page)).toHaveValue(`Imported ${word}`);
  await expect(page.getByRole("button", { name: "Polished", pressed: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(editorContent(page)).toContainText(`First paragraph ${word}.`);
  await expect(editorContent(page).locator('[data-content-type="bulletListItem"]')).toHaveText([
    "one",
    "two",
  ]);
  // The outline picks up the imported heading.
  await expect(page.getByRole("button", { name: "Overview" })).toBeVisible();

  // Several files at once; a file that isn't text is reported by name.
  chooser = page.waitForEvent("filechooser");
  await openWorkspaceItem(page, "Import Markdown files…");
  await (
    await chooser
  ).setFiles([
    { name: `alpha-${word}.md`, mimeType: "text/markdown", buffer: Buffer.from(`Alpha ${word}`) },
    { name: `beta-${word}.md`, mimeType: "text/markdown", buffer: Buffer.from(`Beta ${word}`) },
    { name: "broken.md", mimeType: "text/markdown", buffer: Buffer.from([0xff, 0xfe, 0xfd]) },
  ]);
  await expect(
    page.getByRole("status").filter({ hasText: "Imported 2 notes · couldn't read broken.md" }),
  ).toBeVisible();
  await expect(noteTitle(page)).toHaveValue(`alpha-${word}`);
  await expect(treeRow(page, `alpha-${word}`)).toBeVisible();
  await expect(treeRow(page, `beta-${word}`)).toBeVisible();

  // Nothing readable at all is an error, not a success.
  chooser = page.waitForEvent("filechooser");
  await openWorkspaceItem(page, "Import Markdown files…");
  await (
    await chooser
  ).setFiles({ name: "bad.md", mimeType: "text/markdown", buffer: Buffer.from([0xff, 0xfe]) });
  await expect(page.getByRole("alert").filter({ hasText: "Nothing imported" })).toBeVisible();

  // Imported notes are searchable and survive a reload.
  await page.reload();
  await page.getByRole("searchbox", { name: "Search notes" }).fill(`Beta ${word}`);
  await expect(page.locator("[data-results] [data-list-item]").first()).toContainText(
    `beta-${word}`,
  );
});

test("an import can go straight into a folder", async ({ page }, testInfo) => {
  const word = uniqueWord("fld");
  const folderId = await createFolder(page, testInfo, `Inbox ${word}`);
  const response = await page.request.post(`${apiUrl(testInfo)}/markdown-import/files`, {
    multipart: {
      folder_id: folderId,
      files: { name: `filed-${word}.md`, mimeType: "text/markdown", buffer: Buffer.from("Hi") },
    },
  });
  expect(response.ok()).toBe(true);
  await openApp(page);
  await expect(
    sidebar(page)
      .locator(`[data-folder-id="${folderId}"]`)
      .getByRole("button", { name: `filed-${word}` }),
  ).toBeVisible();
});

test("a note exports as Markdown from its header and the sidebar menu", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("exp");
  const note = await createNote(page, testInfo, {
    title: `Trip: Kyoto/Osaka ${word}`,
    body: `# Day one\n\nTemples ${word}.\n\n- Fushimi Inari\n- Kiyomizu`,
  });
  await openApp(page, `/?note=${note.id}`);
  await expect(noteTitle(page)).toHaveValue(note.title);

  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Export" }).click();
  const file = await download;
  expect(file.suggestedFilename()).toMatch(/\.md$/);
  expect(file.suggestedFilename()).toContain(word);
  expect(file.suggestedFilename()).not.toContain("/");
  const text = await readFile((await file.path())!, "utf8");
  expect(text).toContain(`title: 'Trip: Kyoto/Osaka ${word}'`);
  expect(text).toContain("# Day one");
  expect(text).toContain(`Temples ${word}.`);
  expect(text).toContain("- Fushimi Inari");
  expect(text).not.toContain(note.blocks[0].id);
  // Exporting doesn't navigate away from the note.
  await expect(noteTitle(page)).toHaveValue(note.title);

  const fromMenu = page.waitForEvent("download");
  await treeRow(page, note.title).click({ button: "right" });
  await page.getByRole("menuitem", { name: "Download as Markdown" }).click();
  expect(await readFile((await (await fromMenu).path())!, "utf8")).toBe(text);
});
