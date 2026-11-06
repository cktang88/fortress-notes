import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { proxyToBackend } from "./backend";
import { apiUrl, createNote, noteTitle, openApp, readBlocks, treeRow, uniqueWord } from "./notes";

const editorSelector = '[contenteditable="true"][aria-label="Document content"]';

async function serverTitle(page: Page, api: string, id: string) {
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

// No page.route here: requests go through the dev server's proxy, as in real use, so a
// request still on its way when the page goes away isn't cut off by the test's routing.
test.describe("closing the page mid-edit", () => {
  async function serverText(page: Page, testInfo: TestInfo, id: string) {
    return (await readBlocks(page, testInfo, id)).map((block) => block.text).join("\n");
  }

  for (const how of ["navigating away", "closing the tab"] as const) {
    test(`words typed just before ${how} reach the server without reopening the note`, async ({
      page,
      context,
    }, testInfo) => {
      const word = uniqueWord("bye");
      const note = await createNote(page, testInfo, {
        title: `Leaving ${word}`,
        body: `Opening line ${word}.`,
      });
      await openApp(page, `/?note=${note.id}`);
      const editor = page.locator(editorSelector);
      await expect(editor).toContainText(`Opening line ${word}.`);
      await editor.locator("p").first().click();
      await page.keyboard.press("End");
      await page.keyboard.type(` Closing words ${word}.`);
      // Gone well inside the save pause.
      if (how === "navigating away") await page.goto("about:blank");
      else await page.close({ runBeforeUnload: true });

      const checker = how === "navigating away" ? page : await context.newPage();
      await expect
        .poll(() => serverText(checker, testInfo, note.id))
        .toContain(`Closing words ${word}.`);
      // Search has it too, before the note is ever opened again.
      await expect
        .poll(async () => {
          const response = await checker.request.get(
            `${apiUrl(testInfo)}/unified-search?q=${encodeURIComponent(`Closing words ${word}`)}`,
          );
          return JSON.stringify(await response.json());
        })
        .toContain(note.id);

      // Reopening sends the copy kept on this device again; the server knows it already.
      const revision = async () =>
        (
          (await (
            await checker.request.get(`${apiUrl(testInfo)}/block-documents/${note.id}`)
          ).json()) as { revision: number }
        ).revision;
      const savedRevision = await revision();
      await checker.addInitScript((id) => {
        (window as unknown as { keptAtStart: string | null }).keptAtStart = localStorage.getItem(
          `fortress-notes:pending-block-transactions:${id}`,
        );
      }, note.id);
      await openApp(checker, `/?note=${note.id}`);
      const keptAtStart = await checker.evaluate(
        () => (window as unknown as { keptAtStart: string | null }).keptAtStart,
      );
      expect(keptAtStart).toContain(`Closing words ${word}.`);
      const reopened = checker.locator(editorSelector);
      await expect(reopened).toContainText(`Opening line ${word}. Closing words ${word}.`);
      await expect
        .poll(() =>
          checker.evaluate(
            (id) => localStorage.getItem(`fortress-notes:pending-block-transactions:${id}`),
            note.id,
          ),
        )
        .toBeNull();
      await expect(checker.getByRole("alert")).toHaveCount(0);
      expect((await serverText(checker, testInfo, note.id)).split("Closing words").length).toBe(2);
      expect(await revision()).toBe(savedRevision);
      await expect(reopened).toHaveAttribute("contenteditable", "true");
    });
  }
});
