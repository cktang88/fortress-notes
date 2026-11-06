import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { proxyToBackend } from "./backend";
import { apiUrl, createNote, noteTitle, openApp, uniqueWord } from "./notes";

const editorSelector = '[contenteditable="true"][aria-label="Document content"]';

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

function backlinks(page: Page) {
  return page.getByText("Backlinks", { exact: true }).locator("..");
}

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

/** A note whose paragraph holds a link as plain editor text (how `[[` links used to be saved). */
async function createNoteWithStoredText(
  page: Page,
  testInfo: TestInfo,
  title: string,
  text: string,
) {
  const note = await createNote(page, testInfo, { title, body: "placeholder" });
  const document = (await (
    await page.request.get(`${apiUrl(testInfo)}/block-documents/${note.id}`)
  ).json()) as { revision: number };
  const response = await page.request.post(
    `${apiUrl(testInfo)}/block-documents/${note.id}/transactions`,
    {
      data: {
        base_revision: document.revision,
        operations: [
          {
            operation: "update",
            block_id: note.blocks[0]!.id,
            type: "paragraph",
            attrs: {},
            content: { blocknote: [{ type: "text", text, styles: {} }], markdown: text },
            text,
          },
        ],
      },
    },
  );
  expect(response.ok()).toBe(true);
  return note;
}

test("note links show the title and open the note, in old and new saved forms", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("link");
  const target = await createNote(page, testInfo, {
    title: `Wiring guide ${word}`,
    body: "Run the heater off its own circuit.",
  });
  const stored = `[[${target.id}|Wiring guide ${word}]]`;
  const imported = await createNote(page, testInfo, {
    title: `Imported ${word}`,
    body: `Before frost, read ${stored} again.`,
  });
  const saved = await createNoteWithStoredText(
    page,
    testInfo,
    `Saved text ${word}`,
    `Check ${stored} first.`,
  );

  for (const source of [imported, saved]) {
    await openApp(page, `/?note=${source.id}`);
    const editor = page.locator(editorSelector);
    const link = editor.locator("a", { hasText: `Wiring guide ${word}` });
    await expect(link).toBeVisible();
    await expect(editor).not.toContainText("[[");
    await expect(editor).not.toContainText(target.id);
    await link.click();
    await expect(noteTitle(page)).toHaveValue(`Wiring guide ${word}`);
    await expect(page).toHaveURL(new RegExp(`note=${target.id}`));
  }
  await expect(backlinks(page)).toContainText(`Imported ${word}`);
  await expect(backlinks(page)).toContainText(`Saved text ${word}`);

  // Editing a note with the old form keeps the link (and its backlink).
  await openApp(page, `/?note=${saved.id}`);
  const editor = page.locator(editorSelector);
  await editor
    .locator("p")
    .first()
    .click({ position: { x: 2, y: 5 } }); // On "Check", not the link.
  await page.keyboard.press("End");
  await page.keyboard.type(" Then rest.");
  await allSaved(page);
  await page.reload();
  await expect(
    page.locator(editorSelector).locator("a", { hasText: `Wiring guide ${word}` }),
  ).toBeVisible();
  await expect(page.locator(editorSelector)).toContainText("Then rest.");
  await page.goto(`/?note=${target.id}`);
  await expect(backlinks(page)).toContainText(`Saved text ${word}`);
});

test("a [[ link inserted from the menu is a real link, survives a reload, and exports as [[…]]", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("menu");
  const target = await createNote(page, testInfo, {
    title: `Bread ratios ${word}`,
    body: "Hydration seventy percent.",
  });
  const source = await createNote(page, testInfo, { title: `Baking day ${word}`, body: "Start" });
  await openApp(page, `/?note=${source.id}`);
  const editor = page.locator(editorSelector);
  await editor.locator("p").first().click();
  await page.keyboard.press("End");
  await page.keyboard.type(" with ");
  await page.keyboard.type("[[");
  await page.keyboard.type(`Bread ratios ${word}`);
  await expect(
    page.getByRole("option", { name: new RegExp(`Bread ratios ${word}`) }),
  ).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("option")).toHaveCount(0);
  await page.keyboard.type(" today.");
  const link = editor.locator("a", { hasText: `Bread ratios ${word}` });
  await expect(link).toBeVisible();
  await expect(editor).not.toContainText("[[");
  await allSaved(page);

  await page.reload();
  const reloaded = page.locator(editorSelector);
  await expect(reloaded).toContainText(`Start with Bread ratios ${word} today.`);
  await expect(reloaded).not.toContainText("[[");

  const markdown = await (
    await page.request.get(`${apiUrl(testInfo)}/block-documents/${source.id}/markdown`)
  ).text();
  expect(markdown).toContain(`Start with [[${target.id}|Bread ratios ${word}]] today.`);

  await reloaded.locator("a", { hasText: `Bread ratios ${word}` }).click();
  await expect(noteTitle(page)).toHaveValue(`Bread ratios ${word}`);
  await expect(backlinks(page)).toContainText(`Baking day ${word}`);
});
