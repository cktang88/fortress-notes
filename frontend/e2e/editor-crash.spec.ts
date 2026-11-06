import { expect, test, type Page } from "@playwright/test";
import { proxyToBackend } from "./backend";
import { createNote, expectSavedText, noteTitle, openApp, treeRow, uniqueWord } from "./notes";

const editorSelector = '[contenteditable="true"][aria-label="Document content"]';

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

/** Answer this note's document request with a block the editor can't show. */
async function breakDocument(page: Page, id: string) {
  const pattern = `**/api/block-documents/${id}`;
  await page.route(pattern, async (route) => {
    const response = await route.fetch();
    const document = (await response.json()) as { children: Record<string, unknown>[] };
    document.children.push({
      ...document.children[0],
      id: "01JZZZZZZZZZZZZZZZZZZZZZZZ",
      position: document.children.length,
      content: { blocknote: [{ type: "no-such-inline-content", content: 42 }] },
    });
    await route.fulfill({ response, json: document });
  });
  return () => page.unroute(pattern);
}

test("a note the editor can't show leaves the rest of the app working and can be reloaded", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("crash");
  const broken = await createNote(page, testInfo, {
    title: `Broken ${word}`,
    body: `Saved words ${word}.`,
  });
  await createNote(page, testInfo, { title: `Healthy ${word}`, body: `Fine words ${word}.` });

  // Leave an edit waiting on this device (the server is unreachable for saves).
  await openApp(page, `/?note=${broken.id}`);
  const editor = page.locator(editorSelector);
  await expect(editor).toContainText(`Saved words ${word}.`);
  await page.route("**/transactions", (route) => route.abort("internetdisconnected"));
  await editor.locator("p").first().click();
  await page.keyboard.press("End");
  await page.keyboard.type(` Unsent words ${word}.`);
  await expect(page.getByText("Can't reach the server")).toBeVisible();

  const restore = await breakDocument(page, broken.id);
  await page.reload();
  const problem = page.getByRole("alert").filter({ hasText: "This note couldn't be displayed" });
  await expect(problem).toBeVisible();
  await expect(noteTitle(page)).toHaveValue(`Broken ${word}`);
  await expect(page.getByRole("searchbox", { name: "Search notes" })).toBeVisible();
  // The edit is still kept on this device.
  expect(
    await page.evaluate(
      (id) => localStorage.getItem(`fortress-notes:pending-block-transactions:${id}`),
      broken.id,
    ),
  ).toContain(`Unsent words ${word}.`);

  // The rest of the app still works: open another note, then come back.
  await treeRow(page, `Healthy ${word}`).click();
  await expect(page.locator(editorSelector)).toContainText(`Fine words ${word}.`);
  await treeRow(page, `Broken ${word}`).click();
  await expect(problem).toBeVisible();

  // Once the server copy is fine again, Reload note shows it with the waiting edit.
  await restore();
  await page.unroute("**/transactions");
  await problem.getByRole("button", { name: "Reload note" }).click();
  await expect(problem).toHaveCount(0);
  await expect(page.locator(editorSelector)).toContainText(
    `Saved words ${word}. Unsent words ${word}.`,
  );
  await expectSavedText(page, testInfo, broken.id, `Unsent words ${word}.`);
});
