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

  test("folding a note into another first saves the words just typed", async ({
    page,
  }, testInfo) => {
    const word = uniqueWord("fold");
    const plan =
      `Shed rebuild ${word}: pour a gravel base, frame the walls with cedar, ` +
      "fit a polycarbonate roof, hang the door on the south side, run one power line, " +
      "budget two thousand dollars, finish before the autumn rains arrive.";
    const original = await createNote(page, testInfo, {
      title: `Shed plan ${word}`,
      body: `${plan}\n\nPaint colour undecided.`,
    });
    const repeat = await createNote(page, testInfo, {
      title: `Shed thoughts ${word}`,
      body: plan.replace("south", "east"),
    });
    await openApp(page, `/?note=${repeat.id}`);
    const chip = page.getByRole("button", { name: new RegExp(`Looks like “Shed plan ${word}”`) });
    await expect(chip).toBeVisible();

    // Slow the save down so the Move click lands while the edit is still waiting.
    await page.route("**/transactions", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      await route.fallback();
    });
    const editor = page.locator(editorSelector);
    await editor.locator("p").first().click();
    await page.keyboard.press("End");
    await page.keyboard.type(` Last minute ${word}.`);
    await chip.click();
    const panel = page.getByRole("dialog", { name: "Seen before" });
    await panel.getByRole("button", { name: "Move this into it…" }).click();
    await panel.getByRole("button", { name: "Move", exact: true }).click();

    await expect(page).toHaveURL(new RegExp(`note=${original.id}`));
    await expect(editor).toContainText(`Last minute ${word}.`);
    await expect
      .poll(async () => {
        const tree = (await (
          await page.request.get(`${apiUrl(testInfo)}/block-documents/${original.id}`)
        ).json()) as { children: { text: string }[] };
        return tree.children.map((block) => block.text).join("\n");
      })
      .toContain(`Last minute ${word}.`);
  });
});
