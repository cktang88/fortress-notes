import { expect, test } from "@playwright/test";
import { proxyToBackend } from "./backend";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

test("captures thoughts into the Inbox without leaving the current note", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  const title = page.getByRole("textbox", { name: "Note title" });
  await title.fill("Essay draft");
  await title.press("Enter");
  await page.keyboard.type("Writing the introduction");

  // From the middle of a sentence: jump to capture, jot, and carry on.
  await page.keyboard.press("Control+Shift+Space");
  const capture = page.getByRole("textbox", { name: "Quick capture to Inbox" });
  await expect(capture).toBeFocused();
  await capture.fill("Call the dentist on Monday");
  await capture.press("Enter");
  const toast = page.getByRole("status").filter({ hasText: "Added to Inbox" });
  await expect(toast).toBeVisible();
  await expect(capture).toHaveValue("");
  await expect(title).toHaveValue("Essay draft");

  await capture.fill("Book idea: a history of maps");
  await capture.press("Enter");
  await expect(page.getByRole("status").filter({ hasText: "Added to Inbox" })).toBeVisible();
  await page.getByRole("button", { name: "Open" }).click();
  await expect(title).toHaveValue("Inbox");
  const editor = page.locator('[contenteditable="true"][aria-label="Document content"]');
  await expect(editor).toContainText("Call the dentist on Monday");
  await expect(editor).toContainText("Book idea: a history of maps");
});
