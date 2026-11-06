import { expect, test } from "@playwright/test";
import { proxyToBackend } from "./backend";
import { uniqueWord } from "./notes";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

test("captures each thought as its own note in Uncategorized without leaving the current note", async ({
  page,
}) => {
  // Unique thoughts, so an earlier run's identical notes can't stand in for these.
  const word = uniqueWord("qc");
  const dentist = `Call the dentist on Monday ${word}`;
  const book = `Book idea: a history of maps ${word}`;
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
  await capture.fill(dentist);
  await capture.press("Enter");
  await expect(
    page.getByRole("status").filter({ hasText: `Saved “${dentist}” to Uncategorized` }),
  ).toBeVisible();
  await expect(capture).toHaveValue("");
  await expect(title).toHaveValue("Essay draft");

  // The box keeps focus, so the next thought can be typed straight away.
  await expect(capture).toBeFocused();
  await page.keyboard.type(book);
  await page.keyboard.press("Enter");
  const folder = page.getByRole("button", { name: "Folder: Uncategorized" });
  await expect(folder).toBeVisible();
  const sidebar = page.getByRole("navigation", { name: "Documents" });
  await expect(sidebar.getByText(book).first()).toBeVisible();
  await expect(sidebar.getByText(dentist).first()).toBeVisible();

  await page.getByRole("button", { name: "Open" }).click();
  await expect(title).toHaveValue(book);
});
