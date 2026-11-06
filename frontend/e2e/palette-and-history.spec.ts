import { expect, test, type Page } from "@playwright/test";
import { proxyToBackend } from "./backend";
import {
  apiUrl,
  createNote,
  expectSavedText,
  focusedBlockId,
  noteTitle,
  openApp,
  uniqueWord,
} from "./notes";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

const palette = (page: Page) =>
  page.getByRole("combobox", { name: "Go to a note or run a command" });
const selectedOption = (page: Page) => page.getByRole("option", { selected: true });

test("the palette is driven by arrow keys, and Escape or a click outside closes it", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("pal");
  const garden = await createNote(page, testInfo, { title: `Garden ${word}`, body: "Roses." });
  const gardening = await createNote(page, testInfo, {
    title: `Gardening tools ${word}`,
    body: "Spade.",
  });
  await openApp(page, "/");

  await page.keyboard.press("Control+KeyK");
  await expect(palette(page)).toBeFocused();
  await page.keyboard.type(`gardn ${word}`);
  const options = page.getByRole("option");
  await expect(options.first()).toHaveText(new RegExp(garden.title));
  await expect(options.nth(1)).toHaveText(new RegExp(gardening.title));
  // Down moves the highlight, Up wraps around to the end.
  await page.keyboard.press("ArrowDown");
  await expect(selectedOption(page)).toHaveText(new RegExp(gardening.title));
  await page.keyboard.press("ArrowUp");
  await page.keyboard.press("ArrowUp");
  await expect(selectedOption(page)).toHaveText(/Search everywhere/);
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await expect(selectedOption(page)).toHaveText(new RegExp(gardening.title));
  await page.keyboard.press("Enter");
  await expect(noteTitle(page)).toHaveValue(gardening.title);
  await expect(palette(page)).toHaveCount(0);

  // Reopening starts fresh, not with the last query.
  await page.keyboard.press("Control+KeyK");
  await expect(palette(page)).toHaveValue("");
  await page.keyboard.type("something");
  await page.keyboard.press("Escape");
  await expect(palette(page)).toHaveCount(0);
  await page.keyboard.press("Control+KeyK");
  await expect(palette(page)).toHaveValue("");
  // Ctrl+K again toggles it closed.
  await page.keyboard.press("Control+KeyK");
  await expect(palette(page)).toHaveCount(0);

  // Clicking the backdrop closes it without doing anything.
  await page.keyboard.press("Control+KeyK");
  await page.mouse.click(5, 5);
  await expect(palette(page)).toHaveCount(0);
  await expect(noteTitle(page)).toHaveValue(gardening.title);

  // An exact title offers no "Create" duplicate.
  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type(garden.title.toUpperCase());
  await expect(page.getByRole("option", { name: /Create note/ })).toHaveCount(0);
  await expect(selectedOption(page)).toHaveText(new RegExp(garden.title));
  await page.keyboard.press("Escape");
});

test("the palette creates notes with a title and opens workspace dialogs", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("new");
  await openApp(page, "/");

  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type(`Recipe ideas ${word}`);
  await page.keyboard.press("Enter");
  await expect(noteTitle(page)).toHaveValue(`Recipe ideas ${word}`);
  const id = new URL(page.url()).searchParams.get("note");
  expect(id).toBeTruthy();
  // It is a real note with that title, and shows up in the sidebar after a reload.
  await page.reload();
  await expect(noteTitle(page)).toHaveValue(`Recipe ideas ${word}`);
  const saved = await (await page.request.get(`${apiUrl(testInfo)}/block-documents/${id}`)).json();
  expect(saved.title).toBe(`Recipe ideas ${word}`);

  // "New note" makes an untitled note ready for typing.
  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type("new note");
  await expect(selectedOption(page)).toHaveText(/^New note/);
  await page.keyboard.press("Enter");
  await expect(page).not.toHaveURL(new RegExp(`note=${id}`));
  await expect(noteTitle(page)).toHaveValue("Untitled");
  await expect(page.locator('[aria-label="Document content"]')).toBeFocused();

  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type("backups");
  await page.keyboard.press("Enter");
  const backups = page.getByRole("dialog", { name: "Backups" });
  await expect(backups).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(backups).toBeHidden();

  // The same command twice in a row still opens it.
  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type("backups");
  await page.keyboard.press("Enter");
  await expect(backups).toBeVisible();
  await backups.getByRole("button", { name: "Close" }).click();

  const chooser = page.waitForEvent("filechooser");
  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type("import");
  await expect(selectedOption(page)).toHaveText(/Import Markdown files/);
  await page.keyboard.press("Enter");
  expect((await chooser).isMultiple()).toBe(true);
});

test("Back and Forward move between opened notes; deep links open a note", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("hst");
  const first = await createNote(page, testInfo, { title: `First ${word}`, body: "One." });
  const second = await createNote(page, testInfo, {
    title: `Second ${word}`,
    body: "Two.\n\nTwo and a half.",
  });
  const third = await createNote(page, testInfo, { title: `Third ${word}`, body: "Three." });
  await openApp(page, `/?note=${first.id}`);
  await expect(noteTitle(page)).toHaveValue(first.title);

  for (const note of [second, third]) {
    await page.keyboard.press("Control+KeyK");
    await page.keyboard.type(note.title);
    await page.keyboard.press("Enter");
    await expect(noteTitle(page)).toHaveValue(note.title);
  }

  await page.goBack();
  await expect(noteTitle(page)).toHaveValue(second.title);
  await expect(page).toHaveURL(new RegExp(`note=${second.id}`));
  await page.goBack();
  await expect(noteTitle(page)).toHaveValue(first.title);
  await page.goForward();
  await expect(noteTitle(page)).toHaveValue(second.title);
  await page.goForward();
  await expect(noteTitle(page)).toHaveValue(third.title);

  // Typing and going Back straight away keeps the edit when coming Forward again,
  // even while the save is still on its way (a slow server).
  await page.route("**/api/block-documents/*/transactions", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    await route.fallback();
  });
  await page.locator(`.bn-block[data-id="${third.blocks[0].id}"]`).click();
  await page.keyboard.press("End");
  await page.keyboard.type(" Typed before leaving.");
  await page.goBack();
  await expect(noteTitle(page)).toHaveValue(second.title);
  await page.goForward();
  await expect(noteTitle(page)).toHaveValue(third.title);
  await expect(page.locator('[aria-label="Document content"]')).toContainText(
    "Three. Typed before leaving.",
  );
  await expectSavedText(page, testInfo, third.id, "Three. Typed before leaving.");

  // A deep link straight to a paragraph.
  await openApp(page, `/?note=${second.id}#block=${second.blocks[1].id}`);
  await expect(noteTitle(page)).toHaveValue(second.title);
  await expect(
    page
      .locator(`.bn-block[data-id="${second.blocks[1].id}"]`)
      .filter({ hasText: "Two and a half." }),
  ).toBeVisible();
  await expect.poll(() => focusedBlockId(page)).toBe(second.blocks[1].id);
});

test("a link to a missing or trashed note explains itself and can be closed", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("gone");
  const note = await createNote(page, testInfo, { title: `Gone ${word}`, body: "Bye." });
  await page.request.delete(`${apiUrl(testInfo)}/block-documents/${note.id}`);

  await openApp(page, `/?note=${note.id}`);
  const alert = page.getByRole("alert").filter({ hasText: "doesn't exist or is in the Trash" });
  await expect(alert).toBeVisible();
  await expect(alert.getByRole("button", { name: "Try again" })).toHaveCount(0);
  await alert.getByRole("button", { name: "Close" }).click();
  await expect(page.getByText("Select a note or create a new one.")).toBeVisible();
  await expect(page).not.toHaveURL(/note=/);

  await openApp(page, "/?note=does-not-exist");
  await expect(page.getByRole("alert")).toContainText("doesn't exist or is in the Trash");

  // Restoring the trashed note makes the same link work.
  await page.request.post(`${apiUrl(testInfo)}/trash/${note.id}/restore`);
  await openApp(page, `/?note=${note.id}`);
  await expect(noteTitle(page)).toHaveValue(note.title);
});
