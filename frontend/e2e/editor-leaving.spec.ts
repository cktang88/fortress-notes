import { expect, test } from "@playwright/test";
import { proxyToBackend } from "./backend";
import { apiUrl, createNote, noteTitle, openApp, treeRow, uniqueWord } from "./notes";

const editorSelector = '[contenteditable="true"][aria-label="Document content"]';

async function serverTitle(page: import("@playwright/test").Page, api: string, id: string) {
  const response = await page.request.get(`${api}/block-documents/${id}`);
  return ((await response.json()) as { title: string }).title;
}

test.describe("leaving a note mid-edit", () => {
  test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

  test("a title typed just before pressing Back is saved", async ({ page }, testInfo) => {
    const word = uniqueWord("back");
    const first = await createNote(page, testInfo, { title: `First ${word}` });
    const second = await createNote(page, testInfo, { title: `Second ${word}` });
    await openApp(page, `/?note=${first.id}`);
    await expect(noteTitle(page)).toHaveValue(`First ${word}`);
    await treeRow(page, `Second ${word}`).click();
    await expect(noteTitle(page)).toHaveValue(`Second ${word}`);

    await noteTitle(page).fill(`Renamed ${word}`);
    // Back before the title's save pause is over: the field never loses focus normally.
    await page.goBack();
    await expect(noteTitle(page)).toHaveValue(`First ${word}`);
    await expect.poll(() => serverTitle(page, apiUrl(testInfo), second.id)).toBe(`Renamed ${word}`);
    await expect(treeRow(page, `Renamed ${word}`)).toBeVisible();
    await expect(page.locator(editorSelector)).toBeVisible();
  });
});
