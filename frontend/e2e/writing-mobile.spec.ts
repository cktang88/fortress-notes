import { expect, test } from "@playwright/test";
import { proxyToBackend } from "./backend";

const editorSelector = '[contenteditable="true"][aria-label="Document content"]';

test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });
test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

test("on a phone-sized screen you can create a note, write in it and keep it", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  const editor = page.locator(editorSelector);
  await expect(editor).toBeFocused();

  // The writing area is usable, not squeezed beside the sidebar.
  const box = (await editor.boundingBox())!;
  expect(box.width).toBeGreaterThanOrEqual(250);
  expect(box.x + box.width).toBeLessThanOrEqual(390 + 1);

  await page.keyboard.type("Phone shopping list");
  await page.keyboard.press("Enter");
  await page.keyboard.type("- Oat milk");
  await page.keyboard.press("Enter");
  await page.keyboard.type("Lemons");
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue(
    "Phone shopping list",
  );
  await expect(editor.getByText("Lemons")).toBeInViewport();
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
  const noteId = new URL(page.url()).searchParams.get("note");
  await expect
    .poll(() =>
      page.evaluate(
        (id) => localStorage.getItem(`fortress-notes:pending-block-transactions:${id}`),
        noteId,
      ),
    )
    .toBeNull();

  await page.reload();
  const restored = page.locator(editorSelector);
  await expect(restored).toContainText("Phone shopping list");
  await expect(restored.locator('[data-content-type="bulletListItem"]')).toHaveText([
    "Oat milk",
    "Lemons",
  ]);
  // Tapping into the text puts the cursor there and typing works.
  await restored.getByText("Lemons").tap();
  await page.keyboard.press("End");
  await page.keyboard.type(" x3");
  await expect(restored).toContainText("Lemons x3");
  // No sideways scrolling on a phone.
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1),
  ).toBe(true);
});
