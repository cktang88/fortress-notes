import { expect, test } from "@playwright/test";
import { proxyToBackend } from "./backend";

const PLAN =
  "Kitchen renovation plan: replace cabinets with oak, install quartz countertops, " +
  "move the sink under the window, add pendant lighting above the island, " +
  "budget twelve thousand dollars, contractor Miguel starts in March.";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

test("offers to fold a repeated note into the existing one", async ({ page }, testInfo) => {
  const backendUrl = String(testInfo.config.metadata.backendUrl);
  const create = async (title: string, body: string) =>
    (await (await page.request.post(`${backendUrl}/api/notes`, { data: { title, body } })).json())
      .id as string;
  const original = await create("Kitchen reno", `${PLAN}\n\nPaint colour undecided.`);
  const repeat = await create("Reno thoughts", PLAN.replace("March", "April"));
  const unrelated = await create(
    "Bathroom reno",
    `${PLAN.replace("Kitchen", "Bathroom")}\n\nTiles from the outlet store.`,
  );

  // "Not a duplicate" is remembered.
  await page.goto(`/?note=${unrelated}`);
  await page.evaluate(() => localStorage.removeItem("fortress-notes:not-duplicates"));
  const banner = page.getByRole("region", { name: "Possible duplicate" });
  // It repeats both kitchen notes; dismissing one shows the other.
  await expect(banner).toBeVisible();
  await banner.getByRole("button", { name: "Not a duplicate" }).click();
  await expect(banner).toBeVisible();
  await banner.getByRole("button", { name: "Not a duplicate" }).click();
  await expect(banner).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue("Bathroom reno");
  await page.waitForTimeout(2000);
  await expect(banner).toHaveCount(0);

  await page.goto(`/?note=${repeat}`);
  await expect(banner).toContainText("Kitchen reno");
  await banner.getByRole("button", { name: /Move this into “Kitchen reno”/ }).click();
  await banner.getByRole("button", { name: "Move", exact: true }).click();

  const title = page.getByRole("textbox", { name: "Note title" });
  await expect(title).toHaveValue("Kitchen reno");
  await expect(page).toHaveURL(new RegExp(`note=${original}`));
  const editor = page.locator('[contenteditable="true"][aria-label="Document content"]');
  await expect(editor).toContainText("Paint colour undecided.");
  await expect(editor).toContainText("contractor Miguel starts in April");
  await expect(page.getByRole("status").filter({ hasText: "Moved into" })).toBeVisible();

  // The emptied note went to Trash.
  await page.getByRole("button", { name: "Workspace menu" }).click();
  await page.getByRole("menuitem", { name: "Trash" }).click();
  await expect(page.getByRole("dialog", { name: "Trash" })).toContainText("Reno thoughts");
});
