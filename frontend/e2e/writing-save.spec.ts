import { expect, test, type Page, type Route } from "@playwright/test";
import { proxyToBackend } from "./backend";

const editorSelector = '[contenteditable="true"][aria-label="Document content"]';

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

async function newTitledNote(page: Page, title: string) {
  const before = page.url();
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  await expect(page).not.toHaveURL(before);
  const titleInput = page.getByRole("textbox", { name: "Note title" });
  await expect(page.locator(editorSelector)).toBeFocused();
  await titleInput.fill(title);
  await titleInput.press("Enter");
  const editor = page.locator(editorSelector);
  await expect(editor).toBeFocused();
  return editor;
}

function pendingEdits(page: Page, noteId: string) {
  return page.evaluate(
    (id) => localStorage.getItem(`fortress-notes:pending-block-transactions:${id}`),
    noteId,
  );
}

function currentNoteId(page: Page) {
  return new URL(page.url()).searchParams.get("note")!;
}

async function allSaved(page: Page) {
  const noteId = currentNoteId(page);
  await expect.poll(() => pendingEdits(page, noteId)).toBeNull();
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
}

test("reloading straight after typing shows the edit and keeps the note editable", async ({
  page,
}) => {
  await page.goto("/");
  const editor = await newTitledNote(page, "Reload mid save");
  await page.keyboard.type("First sentence.");
  await allSaved(page);
  const noteId = currentNoteId(page);

  // Type and reload before the save pause is over: the edit only exists on this device.
  await page.keyboard.type(" Second sentence.");
  await expect(editor).toContainText("First sentence. Second sentence.");
  expect(await pendingEdits(page, noteId)).not.toBeNull();
  await page.reload();

  const restored = page.locator(editorSelector);
  await expect(restored).toContainText("First sentence. Second sentence.");
  await expect.poll(() => pendingEdits(page, noteId)).toBeNull();
  await expect(restored).toHaveAttribute("contenteditable", "true");

  // Carrying on doesn't overwrite the recovered sentence.
  await restored.locator("p").first().click();
  await page.keyboard.press("End");
  await page.keyboard.type(" Third.");
  await allSaved(page);
  await page.reload();
  await expect(page.locator(editorSelector)).toContainText(
    "First sentence. Second sentence. Third.",
  );
});

test("switching notes mid-sentence saves both notes", async ({ page }) => {
  await page.goto("/");
  const suffix = Date.now().toString(36);
  const editor = await newTitledNote(page, `Left note ${suffix}`);
  await page.keyboard.type("Left words");
  await allSaved(page);
  const leftId = currentNoteId(page);

  await newTitledNote(page, `Right note ${suffix}`);
  await page.keyboard.type("Right words");
  await allSaved(page);
  const rightId = currentNoteId(page);

  const sidebar = page.getByRole("navigation", { name: "Documents" });
  // Type in the left note and switch away before the save pause ends, then come back.
  await sidebar
    .getByRole("button", { name: `Left note ${suffix}` })
    .first()
    .click();
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue(
    `Left note ${suffix}`,
  );
  await editor.locator("p").first().click();
  await page.keyboard.press("End");
  await page.keyboard.type(" plus more");
  await sidebar
    .getByRole("button", { name: `Right note ${suffix}` })
    .first()
    .click();
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue(
    `Right note ${suffix}`,
  );
  await editor.locator("p").first().click();
  await page.keyboard.press("End");
  await page.keyboard.type(" and others");
  await sidebar
    .getByRole("button", { name: `Left note ${suffix}` })
    .first()
    .click();
  await expect(page.locator(editorSelector)).toContainText("Left words plus more");
  await expect(page.locator(editorSelector)).toHaveAttribute("contenteditable", "true");
  await expect.poll(() => pendingEdits(page, leftId)).toBeNull();
  await expect.poll(() => pendingEdits(page, rightId)).toBeNull();

  await page.reload();
  await expect(page.locator(editorSelector)).toContainText("Left words plus more");
  await page.goto(`/?note=${rightId}`);
  await expect(page.locator(editorSelector)).toContainText("Right words and others");
});

test("fast typing across several blocks all arrives in order", async ({ page }) => {
  await page.goto("/");
  await newTitledNote(page, "Speed typing");
  const lines = ["One quick line", "Two quick line", "Three quick line", "Four quick line"];
  for (const [index, line] of lines.entries()) {
    await page.keyboard.type(line, { delay: 5 });
    if (index < lines.length - 1) await page.keyboard.press("Enter");
  }
  // Fix a typo in the middle without waiting.
  await page.keyboard.press("ArrowUp");
  await page.keyboard.press("End");
  await page.keyboard.type("!");
  await allSaved(page);
  await page.reload();
  await expect(page.locator(editorSelector).locator(".bn-block-outer")).toHaveText([
    "One quick line",
    "Two quick line",
    "Three quick line!",
    "Four quick line",
  ]);
});

test("undo and redo step through saved changes and survive a reload", async ({ page }) => {
  await page.goto("/");
  const editor = await newTitledNote(page, "Undo history");
  await page.keyboard.type("Version one");
  await allSaved(page);
  await page.keyboard.press("Enter");
  await page.keyboard.type("Version two line");
  await allSaved(page);

  const undo = page.getByRole("button", { name: "Undo saved change" });
  const redo = page.getByRole("button", { name: "Redo saved change" });
  await expect(redo).toBeDisabled();
  await undo.click();
  await expect(editor).not.toContainText("Version two line");
  await expect(editor).toContainText("Version one");
  await expect(redo).toBeEnabled();

  await page.reload();
  const restored = page.locator(editorSelector);
  await expect(restored).toContainText("Version one");
  await expect(restored).not.toContainText("Version two line");
  await redo.click();
  await expect(restored).toContainText("Version two line");
  await page.reload();
  await expect(page.locator(editorSelector)).toContainText("Version two line");

  // Typing after an undo is a new change, saved like any other.
  await undo.click();
  await expect(page.locator(editorSelector)).not.toContainText("Version two line");
  await page.locator(editorSelector).locator("p").first().click();
  await page.keyboard.press("End");
  await page.keyboard.type(" revised");
  await allSaved(page);
  await page.reload();
  await expect(page.locator(editorSelector)).toContainText("Version one revised");
  await expect(page.locator(editorSelector)).not.toContainText("Version two line");
});

test("edits made while offline are still there after the connection returns and a reload", async ({
  page,
}) => {
  await page.goto("/");
  const editor = await newTitledNote(page, "Train ride notes");
  await page.keyboard.type("Leaving the station.");
  await allSaved(page);

  await page.context().setOffline(true);
  const unplugged = (route: Route) => route.abort("internetdisconnected");
  await page.route("**/api/**", unplugged);
  await page.keyboard.press("Enter");
  await page.keyboard.type("In the tunnel now.");
  await expect(page.getByText("Can't reach the server")).toBeVisible();
  await page.keyboard.press("Enter");
  await page.keyboard.type("Still underground.");
  await expect(editor).toContainText("Still underground.");

  // Remove only this handler; the backend proxy stays in place.
  await page.unroute("**/api/**", unplugged);
  await page.context().setOffline(false);
  // It syncs by itself (no click needed).
  await expect(page.getByText("Can't reach the server")).toHaveCount(0, { timeout: 15_000 });
  await allSaved(page);
  await page.reload();
  await expect(page.locator(editorSelector).locator(".bn-block-outer")).toHaveText([
    "Leaving the station.",
    "In the tunnel now.",
    "Still underground.",
  ]);
});
