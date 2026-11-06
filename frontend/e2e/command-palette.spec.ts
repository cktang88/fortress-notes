import { expect, test } from "@playwright/test";
import { proxyToBackend } from "./backend";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

test("Ctrl+K creates, finds and opens notes and runs workspace actions", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Select a note or create a new one.")).toBeVisible();
  const palette = page.getByRole("combobox", { name: "Go to a note or run a command" });
  const title = page.getByRole("textbox", { name: "Note title" });

  // Type a title that doesn't exist yet: Enter creates it and puts you in the body.
  await page.keyboard.press("Control+KeyK");
  await expect(palette).toBeFocused();
  await page.keyboard.type("Lisbon itinerary");
  await expect(page.getByRole("option", { selected: true })).toHaveText(
    /Create note “Lisbon itinerary”/,
  );
  await page.keyboard.press("Enter");
  await expect(title).toHaveValue("Lisbon itinerary");
  await expect(page.locator('[aria-label="Document content"]')).toBeFocused();
  await page.keyboard.type("Day one: Alfama");

  // Make a second note so the palette has something to choose between.
  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type("Groceries");
  await page.keyboard.press("Enter");
  await expect(title).toHaveValue("Groceries");

  // Fuzzy title match jumps back.
  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type("lsbn");
  await expect(page.getByRole("option", { selected: true })).toHaveText(/Lisbon itinerary/);
  await page.keyboard.press("Enter");
  await expect(title).toHaveValue("Lisbon itinerary");

  // Before typing, recent notes are offered.
  await page.keyboard.press("Control+KeyK");
  await expect(page.getByRole("option").first()).toHaveText(/Lisbon itinerary|Groceries/);
  await page.keyboard.press("Escape");
  await expect(palette).toHaveCount(0);

  // "Search everywhere" hands off to full-text search.
  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type("Alfama");
  await page.getByRole("option", { name: /Search everywhere for “Alfama”/ }).click();
  await expect(page.getByRole("searchbox", { name: "Search notes" })).toHaveValue("Alfama");
  await expect(page.getByText("Day one: Alfama").first()).toBeVisible();
  await page.getByRole("searchbox", { name: "Search notes" }).press("Escape");

  // Commands reach workspace dialogs.
  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type("trash");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog", { name: "Trash" })).toBeVisible();
});
