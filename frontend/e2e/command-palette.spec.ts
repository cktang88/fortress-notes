import { expect, test } from "@playwright/test";
import { proxyToBackend } from "./backend";
import { uniqueWord } from "./notes";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

test("Ctrl+K creates, finds and opens notes and runs workspace actions", async ({ page }) => {
  // Unique titles, so notes left by other specs (or an earlier run) can't match.
  const word = uniqueWord("cmd");
  const trip = `Lisbon itinerary ${word}`;
  const groceries = `Groceries ${word}`;
  const sight = `Alfama${word}`;
  await page.goto("/");
  await expect(page.getByText("Select a note or create a new one.")).toBeVisible();
  const palette = page.getByRole("combobox", { name: "Go to a note or run a command" });
  const title = page.getByRole("textbox", { name: "Note title" });

  // Type a title that doesn't exist yet: Enter creates it and puts you in the body.
  await page.keyboard.press("Control+KeyK");
  await expect(palette).toBeFocused();
  await page.keyboard.type(trip);
  await expect(page.getByRole("option", { selected: true })).toHaveText(
    new RegExp(`Create note “${trip}”`),
  );
  await page.keyboard.press("Enter");
  await expect(title).toHaveValue(trip);
  await expect(page.locator('[aria-label="Document content"]')).toBeFocused();
  await page.keyboard.type(`Day one: ${sight}`);

  // Make a second note so the palette has something to choose between.
  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type(groceries);
  await page.keyboard.press("Enter");
  await expect(title).toHaveValue(groceries);

  // Fuzzy title match jumps back.
  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type(`lsbn ${word}`);
  await expect(page.getByRole("option", { selected: true })).toHaveText(new RegExp(trip));
  await page.keyboard.press("Enter");
  await expect(title).toHaveValue(trip);

  // Before typing, recent notes are offered.
  await page.keyboard.press("Control+KeyK");
  await expect(page.getByRole("option").first()).toHaveText(new RegExp(`${trip}|${groceries}`));
  await page.keyboard.press("Escape");
  await expect(palette).toHaveCount(0);

  // "Search everywhere" hands off to full-text search.
  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type(sight);
  await page.getByRole("option", { name: new RegExp(`Search everywhere for “${sight}”`) }).click();
  await expect(page.getByRole("searchbox", { name: "Search notes" })).toHaveValue(sight);
  // …with the cursor in the search box, ready to refine the query or press ↓/Enter.
  await expect(page.getByRole("searchbox", { name: "Search notes" })).toBeFocused();
  await expect(page.getByText(`Day one: ${sight}`).first()).toBeVisible();
  await page.getByRole("searchbox", { name: "Search notes" }).press("Escape");

  // Commands reach workspace dialogs.
  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type("trash");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog", { name: "Trash" })).toBeVisible();
});
