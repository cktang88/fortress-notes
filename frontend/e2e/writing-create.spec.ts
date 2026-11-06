import { expect, test, type Page } from "@playwright/test";
import { proxyToBackend } from "./backend";

const editorSelector = '[contenteditable="true"][aria-label="Document content"]';

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

function saved(page: Page) {
  return expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
}

test("the title follows the first line until you choose one, and clearing it hands it back", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  const editor = page.locator(editorSelector);
  const title = page.getByRole("textbox", { name: "Note title" });
  const sidebar = page.getByRole("navigation", { name: "Documents" });
  await expect(editor).toBeFocused();

  await page.keyboard.type("Autumn reading list");
  await expect(title).toHaveValue("Autumn reading list");
  // Editing the first line keeps the title in step.
  await page.keyboard.type(" for 2027");
  await expect(title).toHaveValue("Autumn reading list for 2027");
  await expect(sidebar.getByText("Autumn reading list for 2027").first()).toBeVisible();
  await page.keyboard.press("Enter");
  await page.keyboard.type("The Overstory");
  await saved(page);
  await page.reload();
  await expect(title).toHaveValue("Autumn reading list for 2027");

  // A chosen title sticks, even when the first line changes and after a reload.
  await title.fill("Books");
  await title.press("Enter");
  await expect(editor).toBeFocused();
  await page.keyboard.press("Control+Home");
  await page.keyboard.type("My ");
  await saved(page);
  await expect(sidebar.getByText("Books", { exact: true }).first()).toBeVisible();
  await page.reload();
  await expect(title).toHaveValue("Books");
  await expect(page.locator(editorSelector)).toContainText("My Autumn reading list for 2027");

  // Clearing the title hands it back to the first line.
  await title.fill("");
  await title.press("Enter");
  await expect(title).toHaveValue("My Autumn reading list for 2027");
  await page.reload();
  await expect(title).toHaveValue("My Autumn reading list for 2027");
  // ...and it follows edits again.
  await page.locator(editorSelector).locator("p").first().click();
  await page.keyboard.press("End");
  await page.keyboard.type("!");
  await expect(title).toHaveValue("My Autumn reading list for 2027!");
  await saved(page);
  await page.reload();
  await expect(title).toHaveValue("My Autumn reading list for 2027!");
});

test("Alt+N makes a note you can type into straight away", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Select a note or create a new one.")).toBeVisible();
  await page.locator("body").press("Alt+KeyN");
  const editor = page.locator(editorSelector);
  await expect(editor).toBeFocused();
  await page.keyboard.type("Alt N jotting about bicycles");
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue(
    "Alt N jotting about bicycles",
  );
  await saved(page);
  await page.reload();
  await expect(page.locator(editorSelector)).toContainText("Alt N jotting about bicycles");

  // Alt+N inside the note body doesn't create another note.
  const url = page.url();
  await page.locator(editorSelector).click();
  await page.keyboard.press("Alt+KeyN");
  await expect(page).toHaveURL(url);
});

test("a captured thought lands in Uncategorized with its text, and survives a reload", async ({
  page,
}) => {
  await page.goto("/");
  const capture = page.getByRole("textbox", { name: "Quick capture" });
  await capture.click();
  await capture.fill("Buy stamps for the wedding invitations");
  await capture.press("Enter");
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Saved “Buy stamps for the wedding invitations” to Uncategorized" }),
  ).toBeVisible();
  // Nothing was opened: the empty state is still there.
  await expect(page.getByText("Select a note or create a new one.")).toBeVisible();
  await page.getByRole("button", { name: "Open" }).click();
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue(
    "Buy stamps for the wedding invitations",
  );
  await expect(page.locator(editorSelector)).toContainText(
    "Buy stamps for the wedding invitations",
  );

  // The captured note is editable like any other, and lives in Uncategorized.
  await page.locator(editorSelector).locator("p").first().click();
  await page.keyboard.press("End");
  await page.keyboard.type(" (40 of them)");
  await saved(page);
  await page.reload();
  await expect(page.locator(editorSelector)).toContainText(
    "Buy stamps for the wedding invitations (40 of them)",
  );
  const sidebar = page.getByRole("navigation", { name: "Documents" });
  const folder = sidebar.locator("li[data-folder-id]").filter({
    has: page.getByRole("button", { name: "Folder: Uncategorized" }),
  });
  await expect(
    folder.getByRole("button", { name: "Buy stamps for the wedding invitations", exact: true }),
  ).toBeVisible();

  // An empty capture does nothing.
  await capture.click();
  await capture.press("Enter");
  await expect(page.getByRole("status").filter({ hasText: "Saved “" })).toHaveCount(0);
});

test("the palette creates a titled note that keeps its title as you write", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Select a note or create a new one.")).toBeVisible();
  await page.keyboard.press("Control+KeyK");
  await page.keyboard.type("Pottery class schedule");
  await page.keyboard.press("Enter");
  const title = page.getByRole("textbox", { name: "Note title" });
  await expect(title).toHaveValue("Pottery class schedule");
  await expect(page.locator(editorSelector)).toBeFocused();
  // Its title was chosen, so the first line doesn't replace it.
  await page.keyboard.type("Tuesdays at seven");
  await saved(page);
  await page.reload();
  await expect(title).toHaveValue("Pottery class schedule");
  await expect(page.locator(editorSelector)).toContainText("Tuesdays at seven");
});
