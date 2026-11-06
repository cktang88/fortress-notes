import { expect, test } from "@playwright/test";
import { proxyToBackend } from "./backend";
import { uniqueWord } from "./notes";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

test("trash with undo, saved searches and backups work together", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 1000 });
  // A unique title: earlier runs leave their own packing lists and saved searches behind.
  const packing = `Packing ${uniqueWord("wsp")}`;
  await page.goto("/");
  await expect(page.getByText("Select a note or create a new one.")).toBeVisible();

  // Alt+N creates a note; give it a recognizable title.
  await page.locator("body").press("Alt+KeyN");
  const title = page.getByPlaceholder("Untitled");
  await expect(title).toBeVisible();
  await title.fill(packing);
  const sidebar = page.getByRole("navigation", { name: "Documents" });
  const row = sidebar.locator("[data-document-id]").filter({ hasText: packing }).first();
  await expect(row).toBeVisible();

  // Delete moves it to Trash, and Undo brings it straight back.
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Moved" })).toContainText(
    `Moved “${packing}” to Trash`,
  );
  await expect(sidebar.getByText(packing)).toHaveCount(0);
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(title).toHaveValue(packing);
  await expect(sidebar.getByText(packing).first()).toBeVisible();

  // Delete again and restore from the Trash dialog instead.
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await page.getByRole("button", { name: "Workspace menu" }).click();
  await page.getByRole("menuitem", { name: "Trash" }).click();
  const trash = page.getByRole("dialog", { name: "Trash" });
  // Other specs may leave notes in Trash too; act on this one only.
  const packingRow = trash.getByRole("listitem").filter({ hasText: packing });
  await expect(packingRow).toBeVisible();
  await packingRow.getByRole("button", { name: "Restore" }).click();
  await expect(packingRow).toHaveCount(0);
  await trash.getByRole("button", { name: "Close" }).click();
  await expect(sidebar.getByText(packing).first()).toBeVisible();

  // Search, save it, and re-run it from the chip.
  await page.getByRole("searchbox", { name: "Search notes" }).click();
  await page.keyboard.type(packing);
  await page.getByRole("button", { name: "☆ Save search" }).click();
  await expect(page.getByRole("button", { name: "★ Saved" })).toBeDisabled();
  await page.getByRole("searchbox", { name: "Search notes" }).press("Escape");
  await page.getByRole("group", { name: "Saved searches" }).getByText(`★ ${packing}`).click();
  await expect(page.getByRole("searchbox", { name: "Search notes" })).toHaveValue(packing);
  await page.getByRole("searchbox", { name: "Search notes" }).press("Escape");
  // Forget it again, so saved searches don't pile up across runs.
  await page.getByRole("button", { name: `Forget saved search ${packing}` }).click();
  await expect(page.getByText(`★ ${packing}`)).toHaveCount(0);

  // A manual backup downloads and then appears in the list.
  await page.getByRole("button", { name: "Workspace menu" }).click();
  await page.getByRole("menuitem", { name: "Backups…" }).click();
  const backups = page.getByRole("dialog", { name: "Backups" });
  const backupDownload = page.waitForEvent("download");
  await backups.getByRole("button", { name: "Back up now & download" }).click();
  expect((await backupDownload).suggestedFilename()).toMatch(/^fortress-.*-manual\.sqlite3$/);
  // Other specs may have made manual backups too.
  await expect(backups.getByText(/Manual ·/).first()).toBeVisible();
  await page.screenshot({ path: "test-results/workspace-backups.png" });
  await backups.getByRole("button", { name: "Close" }).click();
});
