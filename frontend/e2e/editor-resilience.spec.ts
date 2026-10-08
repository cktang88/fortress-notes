import { expect, test, type Page } from "@playwright/test";
import { proxyToBackend } from "./backend";

const editorSelector = '[contenteditable="true"][aria-label="Document content"]';

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

async function newNote(page: Page, title: string) {
  await page.goto("/");
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  const titleInput = page.getByRole("textbox", { name: "Note title" });
  await expect(titleInput).toBeFocused();
  await page.keyboard.type(title);
  await page.keyboard.press("Enter");
  const editor = page.locator(editorSelector);
  await expect(editor).toBeFocused();
  return editor;
}

test("typing saves in a few batches and the title survives fast typing", async ({ page }) => {
  let transactions = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/transactions")) transactions += 1;
  });
  await newNote(page, "Grocery run tomorrow");
  await page.keyboard.type("Milk, eggs, and a loaf of bread", { delay: 15 });
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
  await expect.poll(() => transactions).toBeGreaterThan(0);
  expect(transactions).toBeLessThanOrEqual(4);

  await page.reload();
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue(
    "Grocery run tomorrow",
  );
  await expect(page.locator(editorSelector)).toContainText("Milk, eggs, and a loaf of bread");
});

test("keeps editing while offline and syncs when the server is back", async ({ page }) => {
  const editor = await newNote(page, "Offline draft");
  await page.keyboard.type("first part");
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);

  await page.route("**/transactions", (route) => route.abort("internetdisconnected"));
  await editor.press("End");
  await page.keyboard.type(" and second part");
  await expect(page.getByText("Can't reach the server")).toBeVisible();
  await page.keyboard.type(" still typing");
  await expect(editor).toHaveAttribute("contenteditable", "true");

  await page.unroute("**/transactions");
  await page.getByRole("button", { name: "Retry now" }).click();
  await expect(page.getByText("Can't reach the server")).toHaveCount(0);

  await page.reload();
  await expect(page.locator(editorSelector)).toContainText(
    "first part and second part still typing",
  );
});

test("a change made in another tab shows a clear way forward", async ({
  page,
  context,
}, testInfo) => {
  const editor = await newNote(page, "Shared note");
  await page.keyboard.type("original");
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);

  const other = await context.newPage();
  await proxyToBackend(other, testInfo);
  await other.goto(page.url());
  const otherEditor = other.locator(editorSelector);
  await otherEditor.click();
  await other.keyboard.press("End");
  const otherSaved = other.waitForResponse(
    (response) => response.url().endsWith("/transactions") && response.ok(),
  );
  await other.keyboard.type(" edited elsewhere");
  await otherSaved;
  await expect(other.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
  await other.close();

  // Returning to the tab may already show the other tab's change (refetch on focus);
  // otherwise typing on the stale copy is refused with a banner. Either way nothing
  // is silently overwritten and the note ends up editable with the other tab's text.
  await editor.press("End");
  await page.keyboard.type(" and mine");
  const banner = page.getByRole("alert").filter({ hasText: "changed somewhere else" });
  await expect(banner.or(editor.getByText("edited elsewhere"))).toBeVisible();
  if (await banner.isVisible()) {
    await banner.getByRole("button", { name: "Load latest version" }).click();
    await expect(banner).toHaveCount(0);
  }
  await expect(editor).toContainText("original edited elsewhere");
  await expect(editor).toHaveAttribute("contenteditable", "true");
});
