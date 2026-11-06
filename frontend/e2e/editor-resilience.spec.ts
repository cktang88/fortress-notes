import { expect, test, type Page } from "@playwright/test";
import { proxyToBackend } from "./backend";
import { openNoteRow } from "./notes";

const editorSelector = '[contenteditable="true"][aria-label="Document content"]';

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

async function newNote(page: Page, title: string) {
  await page.goto("/");
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  const titleInput = page.getByRole("textbox", { name: "Note title" });
  await titleInput.click();
  await titleInput.fill(title);
  await titleInput.press("Enter");
  const editor = page.locator(editorSelector);
  await expect(editor).toBeFocused();
  return editor;
}

test("typing saves in a few batches and the title survives fast typing", async ({ page }) => {
  let transactions = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/transactions")) transactions += 1;
  });
  await newNote(page, "Grocery run tomorrow");
  await page.keyboard.type("Milk, eggs, and a loaf of bread", { delay: 15 });
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
  await expect.poll(() => transactions).toBeGreaterThan(0);
  expect(transactions).toBeLessThanOrEqual(4);

  await page.reload();
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue(
    "Grocery run tomorrow",
  );
  await expect(page.locator(editorSelector)).toContainText("Milk, eggs, and a loaf of bread");
});

test("keeps editing while offline and syncs when the server is back", async ({ page }) => {
  const editor = await newNote(page, "Offline draft");
  await page.keyboard.type("first part");
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);

  await page.route("**/transactions", (route) => route.abort("internetdisconnected"));
  await editor.press("End");
  await page.keyboard.type(" and second part");
  await expect(page.getByText("Can't reach the server")).toBeVisible();
  await page.keyboard.type(" still typing");
  await expect(editor).toHaveAttribute("contenteditable", "true");

  await page.unroute("**/transactions");
  await page.getByRole("button", { name: "Retry now" }).click();
  await expect(page.getByText("Can't reach the server")).toHaveCount(0);

  await page.reload();
  await expect(page.locator(editorSelector)).toContainText(
    "first part and second part still typing",
  );
});

test("edits still waiting to sync stay visible after reopening the note", async ({ page }) => {
  const editor = await newNote(page, "Offline then back");
  await page.keyboard.type("first part");
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);

  await page.route("**/transactions", (route) => route.abort("internetdisconnected"));
  await editor.press("End");
  await page.keyboard.type(" and second part");
  await expect(page.getByText("Can't reach the server")).toBeVisible();

  // Reopen the note while its last edits exist only on this device.
  await page.reload();
  const reopened = page.locator(editorSelector);
  await expect(reopened).toContainText("first part and second part");
  await reopened.click();
  await page.keyboard.press("ControlOrMeta+End");
  await page.keyboard.type(" and more");

  await page.unroute("**/transactions");
  await page.getByRole("button", { name: "Retry now" }).click();
  await expect(page.getByText("Can't reach the server")).toHaveCount(0);
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
  await page.reload();
  await expect(page.locator(editorSelector)).toContainText("first part and second part and more");
});

test("a failed rename doesn't roll the text back to an older copy", async ({ page }) => {
  const editor = await newNote(page, "Rename rollback");
  await page.keyboard.type("first");
  await expect
    .poll(() =>
      page.evaluate(
        () => Object.keys(localStorage).filter((key) => key.includes("pending-block")).length,
      ),
    )
    .toBe(0);

  let releaseRename = () => {};
  const renameHeld = new Promise<void>((resolve) => (releaseRename = resolve));
  await page.route("**/api/block-documents/*", async (route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    await renameHeld;
    await route.fulfill({ status: 500, body: "rename failed" });
  });
  const title = page.getByRole("textbox", { name: "Note title" });
  await title.fill("Renamed note");
  await title.press("Enter");
  await expect(editor).toBeFocused();
  await page.keyboard.press("End");
  const saved = page.waitForResponse(
    (response) => response.url().endsWith("/transactions") && response.ok(),
  );
  await page.keyboard.type(" second");
  await saved;
  const failed = page.waitForResponse((response) => response.request().method() === "PATCH");
  releaseRename();
  await failed;

  await expect(editor).toContainText("first second");
  await page.keyboard.type(" third");
  await expect(page.getByRole("alert").filter({ hasText: "changed somewhere else" })).toHaveCount(
    0,
  );
  await expect
    .poll(() =>
      page.evaluate(
        () => Object.keys(localStorage).filter((key) => key.includes("pending-block")).length,
      ),
    )
    .toBe(0);
  await page.reload();
  await expect(page.locator(editorSelector)).toContainText("first second third");
});

test("typing while Undo saved change is on its way keeps the note consistent", async ({ page }) => {
  const editor = await newNote(page, "Undo in flight");
  await page.keyboard.type("alpha");
  await expect(page.getByRole("button", { name: "Undo saved change" })).toBeEnabled();
  await expect
    .poll(() =>
      page.evaluate(
        () => Object.keys(localStorage).filter((key) => key.includes("pending-block")).length,
      ),
    )
    .toBe(0);
  await page.keyboard.type(" beta");
  await expect
    .poll(() =>
      page.evaluate(
        () => Object.keys(localStorage).filter((key) => key.includes("pending-block")).length,
      ),
    )
    .toBe(0);

  let releaseUndo = () => {};
  const undoHeld = new Promise<void>((resolve) => (releaseUndo = resolve));
  await page.route("**/undo", async (route) => {
    await undoHeld;
    await route.fallback();
  });
  await page.getByRole("button", { name: "Undo saved change" }).click();
  await page.locator('[aria-label="Document content"]').click();
  await page.keyboard.press("End");
  await page.keyboard.type(" gamma");
  const undone = page.waitForResponse((response) => response.url().endsWith("/undo"));
  releaseUndo();
  await undone;
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
  await expect
    .poll(() =>
      page.evaluate(
        () => Object.keys(localStorage).filter((key) => key.includes("pending-block")).length,
      ),
    )
    .toBe(0);

  // Whatever the editor shows now is what the server has.
  const shown = (await editor.textContent()) ?? "";
  await page.reload();
  await expect(page.locator(editorSelector)).toHaveText(shown);

  // And the note is editable again once the undo has landed.
  await page.locator(editorSelector).click();
  await page.keyboard.press("End");
  await page.keyboard.type(" delta");
  await expect
    .poll(() =>
      page.evaluate(
        () => Object.keys(localStorage).filter((key) => key.includes("pending-block")).length,
      ),
    )
    .toBe(0);
  await page.reload();
  await expect(page.locator(editorSelector)).toHaveText(`${shown} delta`);
});

test("a change made in another tab shows a clear way forward", async ({
  page,
  context,
}, testInfo) => {
  const editor = await newNote(page, "Shared note");
  await page.keyboard.type("original");
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);

  const other = await context.newPage();
  await proxyToBackend(other, testInfo);
  await other.goto(page.url());
  const otherEditor = other.locator(editorSelector);
  await otherEditor.click();
  await other.keyboard.press("End");
  const otherSaved = other.waitForResponse(
    (response) => response.url().endsWith("/transactions") && response.ok(),
  );
  await other.keyboard.type(" edited elsewhere");
  await otherSaved;
  await expect(other.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
  await other.close();

  // Returning to the tab may already show the other tab's change (refetch on focus);
  // otherwise typing on the stale copy is refused with a banner. Either way nothing
  // is silently overwritten and the note ends up editable with the other tab's text.
  await editor.press("End");
  await page.keyboard.type(" and mine");
  const banner = page.getByRole("alert").filter({ hasText: "changed somewhere else" });
  await expect(banner.or(editor.getByText("edited elsewhere"))).toBeVisible();
  if (await banner.isVisible()) {
    await banner.getByRole("button", { name: "Load latest version" }).click();
    await expect(banner).toHaveCount(0);
  }
  await expect(editor).toContainText("original edited elsewhere");
  await expect(editor).toHaveAttribute("contenteditable", "true");
});

test("keyboard undo after loading the latest version doesn't bring back the refused edit", async ({
  page,
  context,
}, testInfo) => {
  const editor = await newNote(page, "Shared undo note");
  await page.keyboard.type("original");
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);

  const other = await context.newPage();
  await proxyToBackend(other, testInfo);
  await other.goto(page.url());
  await other.locator(editorSelector).click();
  await other.keyboard.press("End");
  const otherSaved = other.waitForResponse(
    (response) => response.url().endsWith("/transactions") && response.ok(),
  );
  await other.keyboard.type(" edited elsewhere");
  await otherSaved;
  await expect(other.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
  await other.close();

  await editor.press("End");
  await page.keyboard.type(" and mine");
  const banner = page.getByRole("alert").filter({ hasText: "changed somewhere else" });
  await banner.getByRole("button", { name: "Load latest version" }).click();
  await expect(editor).toContainText("original edited elsewhere");

  await editor.click();
  await page.keyboard.press("ControlOrMeta+z");
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
  await expect(editor).toContainText("original edited elsewhere");
  await page.reload();
  await expect(page.locator(editorSelector)).toContainText("original edited elsewhere");
});

test("a new note is ready for typing and takes its title from the first line", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  const editor = page.locator(editorSelector);
  await expect(editor).toBeFocused();
  await page.keyboard.type("Ideas for the garden");
  await page.keyboard.press("Enter");
  await page.keyboard.type("Tomatoes along the fence");

  const title = page.getByRole("textbox", { name: "Note title" });
  await expect(title).toHaveValue("Ideas for the garden");
  await expect(openNoteRow(page)).toHaveAccessibleName("Ideas for the garden");

  // Choosing a title stops it following the first line.
  await title.fill("Garden plan");
  await title.press("Enter");
  await page.keyboard.press("Control+Home");
  await page.keyboard.type("Spring: ");
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue("Garden plan");
});

test("clearing a chosen title hands it back to the first line", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  const editor = page.locator(editorSelector);
  await expect(editor).toBeFocused();
  await page.keyboard.type("Notes on bread");
  const title = page.getByRole("textbox", { name: "Note title" });
  await expect(title).toHaveValue("Notes on bread");
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);

  const renamed = () =>
    page.waitForResponse((response) => response.request().method() === "PATCH" && response.ok());
  const named = renamed();
  await title.fill("Sourdough");
  await title.press("Enter");
  await named;

  // Without touching the note's text, clearing the title gives it back to the first line.
  const cleared = renamed();
  await title.fill("");
  await title.press("Enter");
  await cleared;
  await expect(title).toHaveValue("Notes on bread");
  await page.reload();
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue("Notes on bread");
});
