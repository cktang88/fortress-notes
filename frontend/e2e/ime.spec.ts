import { expect, test, type Page } from "@playwright/test";
import { proxyToBackend } from "./backend";
import { createNote, noteTitle, openApp, treeRow, uniqueWord } from "./notes";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

/**
 * Type `text` the way a Japanese or Chinese input method does: as a composition still
 * being edited. Enter then only confirms the composition; it must not run anything.
 */
async function composeAndConfirm(page: Page, text: string) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Input.imeSetComposition", {
    text,
    selectionStart: text.length,
    selectionEnd: text.length,
  });
  await page.keyboard.press("Enter");
  await cdp.send("Input.insertText", { text });
  await cdp.detach();
}

test("Enter that confirms an input-method composition doesn't run anything", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("ime");
  const note = await createNote(page, testInfo, {
    title: `Trip ${word}`,
    body: `Tokyo 東京 ${word}.`,
  });
  await openApp(page);
  const url = page.url();

  // The palette: no note is created from the half-typed title.
  await page.keyboard.press("Control+KeyK");
  const palette = page.getByRole("combobox", { name: "Go to a note or run a command" });
  await expect(palette).toBeFocused();
  await composeAndConfirm(page, "とうきょう");
  await expect(palette).toBeFocused();
  await expect(palette).toHaveValue("とうきょう");
  await page.keyboard.press("Escape");
  await expect(palette).toHaveCount(0);
  expect(page.url()).toBe(url);

  // The search box: the matching result isn't opened.
  const search = page.getByRole("searchbox", { name: "Search notes" });
  await search.fill(`${word} `);
  await composeAndConfirm(page, "東京");
  await expect(search).toHaveValue(`${word} 東京`);
  const hit = page.locator("[data-results] [data-list-item]").filter({ hasText: note.title });
  await expect(hit.first()).toBeVisible();
  await expect(search).toBeFocused();
  expect(page.url()).toBe(url);
  // A plain Enter afterwards does open it.
  await search.press("Enter");
  await expect(noteTitle(page)).toHaveValue(note.title);
  await search.press("Escape");

  // Quick capture: nothing is saved yet, and the text stays for more typing.
  const capture = page.getByRole("textbox", { name: "Quick capture" });
  await capture.focus();
  await composeAndConfirm(page, `メモ${word}`);
  await expect(capture).toHaveValue(`メモ${word}`);
  await expect(page.getByRole("status").filter({ hasText: "Saved" })).toHaveCount(0);
  await capture.press("Escape");

  // Renaming a note in the sidebar: still editing.
  await treeRow(page, note.title).dblclick();
  const rename = page.getByRole("textbox", { name: `Rename ${note.title}` });
  await rename.press("End");
  await composeAndConfirm(page, "旅行");
  await expect(rename).toBeVisible();
  await expect(rename).toHaveValue(`${note.title}旅行`);
  await rename.press("Escape");
  await expect(treeRow(page, note.title)).toBeVisible();
});
