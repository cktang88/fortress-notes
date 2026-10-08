import { expect, test } from "@playwright/test";
import { proxyToBackend } from "./backend";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

test("captures each thought as its own note in Uncategorized without leaving the current note", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  const title = page.getByRole("textbox", { name: "Note title" });
  await title.fill("Essay draft");
  await title.press("Enter");
  await page.keyboard.type("Writing the introduction");

  // From the middle of a sentence: jump to capture, jot, and carry on.
  await page.keyboard.press("Control+Shift+Space");
  const capture = page.getByRole("textbox", { name: "Quick capture" });
  await expect(capture).toBeFocused();
  await capture.fill("Call the dentist on Monday");
  await capture.press("Enter");
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Saved “Call the dentist on Monday” to Uncategorized" }),
  ).toBeVisible();
  await expect(capture).toHaveValue("");
  await expect(title).toHaveValue("Essay draft");

  await capture.fill("Book idea: a history of maps");
  await capture.press("Enter");
  const folder = page.getByRole("button", { name: "Folder: Uncategorized" });
  await expect(folder).toBeVisible();
  const sidebar = page.getByRole("navigation", { name: "Documents" });
  await expect(sidebar.getByText("Book idea: a history of maps").first()).toBeVisible();
  await expect(sidebar.getByText("Call the dentist on Monday").first()).toBeVisible();

  await page.getByRole("button", { name: "Open" }).click();
  await expect(title).toHaveValue("Book idea: a history of maps");
});
