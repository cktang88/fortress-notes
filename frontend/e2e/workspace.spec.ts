import { expect, test } from "@playwright/test";
import { proxyToBackend } from "./backend";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

test("trash with undo, saved searches and backups work together", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 1000 });
  await page.goto("/");
  await expect(page.getByText("Select a note or create a new one.")).toBeVisible();

  // Alt+N creates a note; give it a recognizable title.
  await page.locator("body").press("Alt+KeyN");
  const title = page.getByPlaceholder("Untitled");
  await expect(title).toBeVisible();
  await title.fill("Packing list");
  const sidebar = page.getByRole("navigation", { name: "Documents" });
  const row = sidebar.locator("[data-document-id]").filter({ hasText: "Packing list" }).first();
  await expect(row).toBeVisible();

  // Delete moves it to Trash, and Undo brings it straight back.
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Moved" })).toContainText(
    "Moved “Packing list” to Trash",
  );
  await expect(sidebar.getByText("Packing list")).toHaveCount(0);
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(title).toHaveValue("Packing list");
  await expect(sidebar.getByText("Packing list").first()).toBeVisible();

  // Delete again and restore from the Trash dialog instead.
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await page.getByRole("button", { name: "Workspace menu" }).click();
  await page.getByRole("menuitem", { name: "Trash" }).click();
  const trash = page.getByRole("dialog", { name: "Trash" });
  await expect(trash.getByText("Packing list")).toBeVisible();
  await trash.getByRole("button", { name: "Restore" }).click();
  await expect(trash.getByText("Trash is empty.")).toBeVisible();
  await trash.getByRole("button", { name: "Close" }).click();
  await expect(sidebar.getByText("Packing list").first()).toBeVisible();

  // Search, save it, and re-run it from the chip.
  await page.getByRole("searchbox", { name: "Search notes" }).click();
  await page.keyboard.type("Packing");
  await page.getByRole("button", { name: "☆ Save search" }).click();
  await expect(page.getByRole("button", { name: "★ Saved" })).toBeDisabled();
  await page.getByRole("searchbox", { name: "Search notes" }).press("Escape");
  await page.getByRole("group", { name: "Saved searches" }).getByText("★ Packing").click();
  await expect(page.getByRole("searchbox", { name: "Search notes" })).toHaveValue("Packing");
  await page.getByRole("searchbox", { name: "Search notes" }).press("Escape");

  // A manual backup downloads and then appears in the list.
  await page.getByRole("button", { name: "Workspace menu" }).click();
  await page.getByRole("menuitem", { name: "Backups…" }).click();
  const backups = page.getByRole("dialog", { name: "Backups" });
  const backupDownload = page.waitForEvent("download");
  await backups.getByRole("button", { name: "Back up now & download" }).click();
  expect((await backupDownload).suggestedFilename()).toMatch(/^fortress-.*-manual\.sqlite3$/);
  await expect(backups.getByText(/Manual ·/)).toBeVisible();
  await page.screenshot({ path: "test-results/workspace-backups.png" });
  await backups.getByRole("button", { name: "Close" }).click();
});
