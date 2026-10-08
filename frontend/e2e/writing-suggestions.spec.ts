import { expect, test } from "@playwright/test";
import { proxyToBackend } from "./backend";

const editorSelector = '[contenteditable="true"][aria-label="Document content"]';

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

test("suggests a related paragraph after a pause and links it", async ({ page }, testInfo) => {
  const backendUrl = String(testInfo.config.metadata.backendUrl);
  await page.request.post(`${backendUrl}/api/notes`, {
    data: {
      title: "Sourdough notes",
      body: "Feed the sourdough starter twice a day with rye flour and water.",
    },
  });
  await page.goto("/");
  await page.evaluate(() => localStorage.removeItem("fortress-notes:writing-suggestions"));
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  const editor = page.locator(editorSelector);
  await editor.click();
  await page.keyboard.type("My sourdough starter needs rye flour every morning");

  const card = page.getByRole("complementary", { name: "You wrote about this before" });
  await expect(card).toContainText("Sourdough notes");
  await card.getByRole("button", { name: "Link" }).click();
  await expect(editor.locator("a")).toHaveText("Sourdough notes");
  await expect(card).toHaveCount(0);
  // The link is saved as a reference, so the other note now lists this one as a backlink.
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
  const sourceTitle = page.getByRole("textbox", { name: "Note title" });
  await page
    .getByRole("navigation", { name: "Documents" })
    .locator("[data-document-id]")
    .filter({ hasText: "Sourdough notes" })
    .last()
    .click();
  await expect(sourceTitle).toHaveValue("Sourdough notes");
  await expect(page.getByText("Backlinks", { exact: true }).locator("..")).toContainText("Untitled");
  await page.goBack();

  // Turning suggestions off sticks.
  await page.getByRole("button", { name: "Suggestions on" }).click();
  await expect(page.getByRole("button", { name: "Suggestions off" })).toBeVisible();
  await page.keyboard.press("Enter");
  await page.keyboard.type("Another long line about the sourdough starter and rye flour");
  await page.waitForTimeout(2000);
  await expect(card).toHaveCount(0);
});
