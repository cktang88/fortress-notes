import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { proxyToBackend } from "./backend";

const editorSelector = '[contenteditable="true"][aria-label="Document content"]';

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

async function createNote(page: Page, testInfo: TestInfo, title: string, body: string) {
  const api = `${String(testInfo.config.metadata.backendUrl)}/api`;
  const response = await page.request.post(`${api}/notes`, { data: { title, body } });
  return ((await response.json()) as { id: string }).id;
}

async function startWriting(page: Page) {
  await page.goto("/");
  await page.evaluate(() => localStorage.removeItem("fortress-notes:writing-suggestions"));
  await page.reload();
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  const editor = page.locator(editorSelector);
  await expect(editor).toBeFocused();
  return editor;
}

test("a linked related paragraph previews on hover and keeps its link after a reload", async ({
  page,
}, testInfo) => {
  const sourceId = await createNote(
    page,
    testInfo,
    "Beekeeping log",
    "Intro line about the apiary.\n\nThe langstroth hive swarmed in June after the queen cells hatched.",
  );
  const editor = await startWriting(page);
  await page.keyboard.type("Our langstroth hive swarmed again once the queen cells hatched");
  const chip = page.getByRole("button", { name: /related/ });
  await expect(chip).toBeVisible();
  await chip.click();
  const panel = page.getByRole("dialog", { name: "Seen before" });
  const item = panel.getByRole("listitem").filter({ hasText: "Beekeeping log" });
  await expect(item).toContainText("langstroth hive swarmed in June");
  await item.getByRole("button", { name: "Link" }).click();

  const link = editor.locator("a").filter({ hasText: "Beekeeping log" });
  await expect(link).toBeVisible();
  // Writing carries on after the link.
  await expect(editor).toBeFocused();
  await page.keyboard.type(" (see above)");
  await expect(editor).toContainText("Beekeeping log (see above)");

  await link.hover();
  const preview = page.getByRole("status").filter({ hasText: "langstroth hive swarmed in June" });
  await expect(preview).toBeVisible();
  await expect(preview).toContainText("Beekeeping log");

  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
  await expect
    .poll(() =>
      page.evaluate(() =>
        Object.keys(localStorage).filter((key) => key.includes("pending-block-transactions")),
      ),
    )
    .toEqual([]);
  await page.reload();
  await expect(page.locator(editorSelector).locator("a")).toHaveText("Beekeeping log");

  // "Open" from the chip jumps to the matching paragraph in the other note.
  await page.locator(editorSelector).locator("p").first().click();
  await page.keyboard.press("End");
  await page.keyboard.press("Enter");
  await page.keyboard.type("The langstroth hive swarmed after the queen cells hatched in June");
  await page.getByRole("button", { name: /related/ }).click();
  await page
    .getByRole("dialog", { name: "Seen before" })
    .getByRole("listitem")
    .filter({ hasText: "Beekeeping log" })
    .getByRole("button", { name: "Open" })
    .click();
  await expect(page).toHaveURL(new RegExp(`note=${sourceId}`));
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue("Beekeeping log");
});

test("turning related suggestions off is remembered, and they come back when turned on", async ({
  page,
}, testInfo) => {
  await createNote(
    page,
    testInfo,
    "Telescope notes",
    "The dobsonian telescope needs collimation before every observing session.",
  );
  await startWriting(page);
  await page.keyboard.type("Remember collimation of the dobsonian telescope before observing");
  const chip = page.getByRole("button", { name: /related/ });
  await expect(chip).toBeVisible();
  await chip.click();
  const panel = page.getByRole("dialog", { name: "Seen before" });
  await panel
    .getByRole("checkbox", { name: "Look for related paragraphs while I write" })
    .uncheck();
  await expect(page.getByRole("button", { name: /related/ })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);

  await page.reload();
  const off = page.getByRole("button", { name: "↗", exact: true });
  await expect(off).toBeVisible();
  await page.locator(editorSelector).locator("p").first().click();
  await page.keyboard.press("End");
  await page.keyboard.type(" tonight");
  // While off, nothing is suggested even after a pause.
  await expect(off).toBeVisible();
  await expect(page.getByRole("button", { name: /related/ })).toHaveCount(0);

  await off.click();
  await page
    .getByRole("dialog", { name: "Seen before" })
    .getByRole("checkbox", { name: "Look for related paragraphs while I write" })
    .check();
  await page.keyboard.press("Escape");
  await page.locator(editorSelector).locator("p").first().click();
  await page.keyboard.press("End");
  await page.keyboard.type(" again");
  await expect(page.getByRole("button", { name: /related/ })).toBeVisible();
});
