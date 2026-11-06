import { expect, test, type Locator, type Page } from "@playwright/test";
import { proxyToBackend } from "./backend";

const editorSelector = '[contenteditable="true"][aria-label="Document content"]';

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

async function newTitledNote(page: Page, title: string) {
  await page.goto("/");
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  const titleInput = page.getByRole("textbox", { name: "Note title" });
  await titleInput.fill(title);
  await titleInput.press("Enter");
  const editor = page.locator(editorSelector);
  await expect(editor).toBeFocused();
  return editor;
}

/** Wait until every edit has reached the server (nothing left queued on this device). */
async function allSaved(page: Page) {
  const noteId = new URL(page.url()).searchParams.get("note");
  await expect
    .poll(() =>
      page.evaluate(
        (id) => localStorage.getItem(`fortress-notes:pending-block-transactions:${id}`),
        noteId,
      ),
    )
    .toBeNull();
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
}

function blocks(editor: Locator) {
  return editor.locator(".bn-block-outer");
}

async function slash(page: Page, query: string, item: string) {
  await page.keyboard.type(`/${query}`);
  const option = page.getByRole("option", { name: new RegExp(item, "i") }).first();
  await expect(option).toBeVisible();
  await option.click();
}

test("markdown shortcuts and the slash menu build a structured note that survives a reload", async ({
  page,
}) => {
  const editor = await newTitledNote(page, "Structured trip plan");
  await page.keyboard.type("# Trip to Kyoto");
  await page.keyboard.press("Enter");
  await page.keyboard.type("## Packing");
  await page.keyboard.press("Enter");
  await page.keyboard.type("- Rain jacket");
  await page.keyboard.press("Enter");
  await page.keyboard.type("Walking shoes");
  await page.keyboard.press("Enter");
  // An empty list item turns back into a paragraph.
  await page.keyboard.press("Enter");
  await page.keyboard.type("1. Book ryokan");
  await page.keyboard.press("Enter");
  await page.keyboard.type("Buy rail pass");
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");
  await page.keyboard.type("[ ] Renew passport");
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");

  await slash(page, "code", "Code Block");
  await page.keyboard.type("const yen = 150;");
  // Leave the code block for a fresh paragraph below it.
  await editor.locator("pre").press("Control+End");
  await page.keyboard.press("ArrowDown");

  await expect(editor.locator("h1")).toHaveText("Trip to Kyoto");
  await expect(editor.locator("h2")).toHaveText("Packing");
  await expect(editor.locator('[data-content-type="bulletListItem"]')).toHaveText([
    "Rain jacket",
    "Walking shoes",
  ]);
  await expect(editor.locator('[data-content-type="numberedListItem"]')).toHaveText([
    "Book ryokan",
    "Buy rail pass",
  ]);
  const task = editor.locator('[data-content-type="checkListItem"]');
  await expect(task).toHaveText("Renew passport");
  await expect(editor.locator("pre")).toContainText("const yen = 150;");

  // Tick the task with the mouse.
  await task.locator('input[type="checkbox"]').check();
  await expect(task.locator('input[type="checkbox"]')).toBeChecked();
  await allSaved(page);

  await page.reload();
  const restored = page.locator(editorSelector);
  await expect(restored.locator("h1")).toHaveText("Trip to Kyoto");
  await expect(restored.locator("h2")).toHaveText("Packing");
  await expect(restored.locator('[data-content-type="bulletListItem"]')).toHaveText([
    "Rain jacket",
    "Walking shoes",
  ]);
  await expect(restored.locator('[data-content-type="numberedListItem"]')).toHaveText([
    "Book ryokan",
    "Buy rail pass",
  ]);
  await expect(restored.locator('[data-content-type="checkListItem"]')).toHaveText(
    "Renew passport",
  );
  await expect(
    restored.locator('[data-content-type="checkListItem"] input[type="checkbox"]'),
  ).toBeChecked();
  await expect(restored.locator("pre")).toContainText("const yen = 150;");
});

test("callout and math placeholder blocks come from the slash menu and persist", async ({
  page,
}) => {
  const editor = await newTitledNote(page, "Physics revision");
  await slash(page, "callout", "Callout");
  await expect(editor.getByRole("note", { name: "Callout" })).toBeVisible();
  await page.keyboard.type("Remember units!");
  await page.keyboard.press("Enter");
  await slash(page, "math", "Math placeholder");
  await expect(editor.getByRole("note", { name: "Math placeholder" })).toBeVisible();
  await expect(editor.getByRole("note", { name: "Callout" })).toHaveText("Remember units!");
  await allSaved(page);

  await page.reload();
  const restored = page.locator(editorSelector);
  await expect(restored.getByRole("note", { name: "Callout" })).toHaveText("Remember units!");
  await expect(restored.getByRole("note", { name: "Math placeholder" })).toBeVisible();

  // Escape closes the slash menu without inserting anything.
  await restored.getByRole("note", { name: "Callout" }).click();
  await page.keyboard.press("End");
  await page.keyboard.press("Enter");
  await page.keyboard.type("/");
  await expect(page.getByRole("option").first()).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("option")).toHaveCount(0);
});

test("formatting toolbar bold, italic and link survive a reload", async ({ page }) => {
  const editor = await newTitledNote(page, "Formatting check");
  await page.keyboard.type("Fortress docs");
  await page.keyboard.press("Enter");
  await page.keyboard.type("Plain then bold words");
  // Select "bold words" with the keyboard.
  for (let i = 0; i < "bold words".length; i += 1) await page.keyboard.press("Shift+ArrowLeft");
  const toolbar = page.getByRole("group", { name: "Text formatting" });
  await expect(toolbar).toBeVisible();
  await toolbar.getByRole("button", { name: /bold/i }).click();
  await expect(editor.locator("strong")).toHaveText("bold words");
  await toolbar.getByRole("button", { name: /italic/i }).click();
  await expect(editor.locator("em")).toHaveText("bold words");

  // Link the first line: the URL field takes focus straight away.
  await editor.locator("p").first().click();
  await page.keyboard.press("End");
  await page.keyboard.press("Shift+Home");
  await toolbar.getByRole("button", { name: /create link/i }).click();
  const url = page.locator("input:focus");
  await expect(url).toBeVisible();
  await page.keyboard.type("https://example.org/docs");
  await page.keyboard.press("Enter");
  await expect(editor.locator('a[href="https://example.org/docs"]')).toHaveText("Fortress docs");
  await allSaved(page);

  await page.reload();
  const restored = page.locator(editorSelector);
  await expect(restored.locator("strong")).toHaveText("bold words");
  await expect(restored.locator("em")).toHaveText("bold words");
  await expect(restored.locator('a[href="https://example.org/docs"]')).toHaveText("Fortress docs");
});

test("dragging a block by its handle reorders the note, and the order is saved", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const editor = await newTitledNote(page, "Reorder me");
  await page.keyboard.type("Alpha line");
  await page.keyboard.press("Enter");
  await page.keyboard.type("Bravo line");
  await page.keyboard.press("Enter");
  await page.keyboard.type("Charlie line");
  await allSaved(page);
  await expect(blocks(editor)).toHaveText(["Alpha line", "Bravo line", "Charlie line"]);

  // Hover Charlie from the left margin, then drag its handle above Alpha.
  const charlie = blocks(editor).filter({ hasText: "Charlie line" });
  const box = (await charlie.boundingBox())!;
  await page.mouse.move(5, 5);
  await page.mouse.move(box.x - 60, box.y + box.height / 2);
  await page.mouse.move(box.x + 10, box.y + box.height / 2);
  const handle = page.locator(".bn-side-menu [draggable='true']");
  await expect(handle).toBeVisible();
  const handleBox = (await handle.boundingBox())!;
  const alphaBox = (await blocks(editor).filter({ hasText: "Alpha line" }).boundingBox())!;
  await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(alphaBox.x + 40, alphaBox.y + 4, { steps: 8 });
  await page.mouse.move(alphaBox.x + 40, alphaBox.y + 2, { steps: 2 });
  await page.mouse.up();

  await expect(blocks(editor)).toHaveText(["Charlie line", "Alpha line", "Bravo line"]);
  await allSaved(page);
  await page.reload();
  await expect(blocks(page.locator(editorSelector))).toHaveText([
    "Charlie line",
    "Alpha line",
    "Bravo line",
  ]);
});

test("selecting everything copies every kind of block", async ({ page }) => {
  const editor = await newTitledNote(page, "Copy all of it");
  await page.keyboard.type("# Heading to copy");
  await page.keyboard.press("Enter");
  await page.keyboard.type("- First bullet");
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");
  await page.keyboard.type("Closing paragraph");
  await expect(editor.locator("h1")).toHaveText("Heading to copy");
  await allSaved(page);

  await page.keyboard.press("Control+a");
  await page.keyboard.press("Control+a");
  await page.keyboard.press("Control+c");
  const copied = await page.evaluate(async () => {
    const [item] = await navigator.clipboard.read();
    return {
      html: item?.types.includes("text/html") ? await (await item.getType("text/html")).text() : "",
      text: item?.types.includes("text/plain")
        ? await (await item.getType("text/plain")).text()
        : "",
    };
  });
  expect(copied.html).toMatch(/<h1[^>]*>.*Heading to copy/s);
  expect(copied.html).toMatch(/<li[^>]*>.*First bullet/s);
  expect(copied.html).toContain("Closing paragraph");
  expect(copied.text).toContain("Heading to copy");
  expect(copied.text).toContain("Closing paragraph");

  // Pasting it into a fresh note reproduces the structure.
  const other = await newTitledNote(page, "Pasted copy");
  await page.keyboard.press("Control+v");
  await expect(other.locator("h1")).toHaveText("Heading to copy");
  await expect(other.locator('[data-content-type="bulletListItem"]')).toHaveText("First bullet");
  await expect(other).toContainText("Closing paragraph");
  await allSaved(page);
  await page.reload();
  await expect(page.locator(editorSelector).locator("h1")).toHaveText("Heading to copy");
});
