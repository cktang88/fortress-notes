import { expect, test, type Page } from "@playwright/test";
import { proxyToBackend } from "./backend";
import { expectSavedText, noteTitle, openApp, uniqueWord } from "./notes";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

const editor = (page: Page) => page.locator('[aria-label="Document content"]');

test("write, find, file away and restore a note without touching the mouse", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("kb");
  await openApp(page);

  // Alt+N: a new note with the cursor in its body; the first line becomes the title.
  await page.keyboard.press("Alt+KeyN");
  await expect(editor(page)).toBeFocused();
  await page.keyboard.type(`Packing ${word}`);
  await page.keyboard.press("Enter");
  await page.keyboard.type("Passport and charger.");
  await expect(noteTitle(page)).toHaveValue(`Packing ${word}`);
  const firstId = new URL(page.url()).searchParams.get("note")!;
  await expectSavedText(page, testInfo, firstId, "Passport and charger.");

  // Ctrl+K: a second note with a title, then back to the first by name.
  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type(`Budget ${word}`);
  await page.keyboard.press("Enter");
  await expect(noteTitle(page)).toHaveValue(`Budget ${word}`);
  await page.keyboard.type("Hotel 120 a night.");
  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type(`packing ${word}`);
  await page.keyboard.press("Enter");
  await expect(noteTitle(page)).toHaveValue(`Packing ${word}`);

  // Escape leaves the editor, then "/" searches; Enter opens the passage.
  await page.keyboard.press("Escape");
  await page.locator("body").press("/");
  await expect(page.getByRole("searchbox", { name: "Search notes" })).toBeFocused();
  await page.keyboard.type(`hotel ${word}`);
  await expect(page.locator("[data-results] [data-list-item]").first()).toContainText(
    `Budget ${word}`,
  );
  await page.keyboard.press("Enter");
  await expect(noteTitle(page)).toHaveValue(`Budget ${word}`);

  // "?" opens the tips; Escape closes them.
  await page.getByRole("searchbox", { name: "Search notes" }).focus();
  await page.keyboard.press("Escape");
  await page.locator("body").press("?");
  const tips = page.getByRole("dialog", { name: "Quick tips" });
  await expect(tips).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(tips).toBeHidden();

  // The workspace menu by keyboard: open, arrow to Trash, Enter.
  await page.getByRole("button", { name: "Workspace menu" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("menuitem", { name: "Import Markdown files…" })).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("menuitem", { name: "Trash" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu", { name: "Workspace" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Workspace menu" })).toBeFocused();

  // Trash the open note with the sidebar's context menu key, then restore it from Trash.
  const row = page.locator(`button[data-document-id][aria-label="Budget ${word}"]`);
  await row.focus();
  await page.keyboard.press("Shift+F10");
  const menu = page.getByRole("menu", { name: "Document actions" });
  await expect(menu).toBeVisible();
  // Up from the first item wraps around to the last one.
  await page.keyboard.press("ArrowUp");
  await expect(menu.getByRole("menuitem", { name: "Move to Trash" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(row).toHaveCount(0);
  await expect(page.getByText("Select a note or create a new one.")).toBeVisible();

  await page.getByRole("button", { name: "Workspace menu" }).focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  const trash = page.getByRole("dialog", { name: "Trash" });
  await expect(trash).toBeVisible();
  const restore = trash
    .getByRole("listitem")
    .filter({ hasText: `Budget ${word}` })
    .getByRole("button", { name: "Restore" });
  await restore.focus();
  await page.keyboard.press("Enter");
  await expect(restore).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(trash).toBeHidden();
  await expect(row).toBeVisible();
  await row.focus();
  await page.keyboard.press("Enter");
  await expect(noteTitle(page)).toHaveValue(`Budget ${word}`);
  await expect(editor(page)).toContainText("Hotel 120 a night.");
});
