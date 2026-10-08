import { expect, test } from "@playwright/test";
import { proxyToBackend } from "./backend";

const editorSelector = '[contenteditable="true"][aria-label="Document content"]';
const PLAN =
  "Kitchen renovation plan: replace cabinets with oak, install quartz countertops, " +
  "move the sink under the window, add pendant lighting above the island, " +
  "budget twelve thousand dollars, contractor Miguel starts in March.";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

async function createNote(
  page: import("@playwright/test").Page,
  url: string,
  title: string,
  body: string,
) {
  const response = await page.request.post(`${url}/api/notes`, { data: { title, body } });
  return (await response.json()).id as string;
}

test("related paragraphs wait behind a quiet chip and link on request", async ({
  page,
}, testInfo) => {
  const backendUrl = String(testInfo.config.metadata.backendUrl);
  const sourceId = await createNote(
    page,
    backendUrl,
    "Sourdough notes",
    "Feed the sourdough starter twice a day with rye flour and water.",
  );
  await page.goto("/");
  await page.evaluate(() => localStorage.removeItem("fortress-notes:writing-suggestions"));
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  const editor = page.locator(editorSelector);
  await editor.click();
  await page.keyboard.type("My sourdough starter needs rye flour every morning");

  // Nothing pops over the text; a chip appears on the status line instead.
  const chip = page.getByRole("button", { name: /related/ });
  await expect(chip).toBeVisible();
  await expect(page.getByRole("dialog", { name: "Seen before" })).toHaveCount(0);
  await expect(editor).toBeFocused();

  await chip.click();
  const panel = page.getByRole("dialog", { name: "Seen before" });
  const linkSaved = page.waitForResponse(
    (response) => response.url().endsWith("/transactions") && response.ok(),
  );
  await panel
    .getByRole("listitem")
    .filter({ hasText: "Sourdough notes" })
    .getByRole("button", { name: "Link" })
    .click();
  await linkSaved;
  await expect(editor.locator("a")).toHaveText("Sourdough notes");
  await expect(panel).toHaveCount(0);

  const draftUrl = page.url();
  await page.goto(`/?note=${sourceId}`);
  await expect(page.getByText("Backlinks", { exact: true }).locator("..")).toContainText(
    "Untitled",
  );
  await page.goto(draftUrl);
});

test("a near-copy turns the chip amber and can be folded in", async ({ page }, testInfo) => {
  const backendUrl = String(testInfo.config.metadata.backendUrl);
  const original = await createNote(
    page,
    backendUrl,
    "Kitchen reno",
    `${PLAN}\n\nPaint colour undecided.`,
  );
  const repeat = await createNote(
    page,
    backendUrl,
    "Reno thoughts",
    PLAN.replace("March", "April"),
  );
  const unrelated = await createNote(
    page,
    backendUrl,
    "Bathroom reno",
    `${PLAN.replace("Kitchen", "Bathroom")}\n\nTiles from the outlet store.`,
  );

  // "Not a duplicate" is remembered across reloads.
  await page.goto(`/?note=${unrelated}`);
  await page.evaluate(() => localStorage.removeItem("fortress-notes:not-duplicates"));
  const amber = page.getByRole("button", { name: /Looks like/ });
  // It repeats both kitchen notes: dismissing one shows the other in the open panel.
  await amber.click();
  const notDuplicate = page
    .getByRole("dialog", { name: "Seen before" })
    .getByRole("button", { name: "Not a duplicate" });
  await notDuplicate.click();
  await expect(amber).toBeVisible();
  await notDuplicate.click();
  await expect(amber).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue("Bathroom reno");
  await page.waitForTimeout(2000);
  await expect(amber).toHaveCount(0);

  await page.goto(`/?note=${repeat}`);
  await expect(amber).toHaveText(/Looks like “Kitchen reno”/);
  await amber.click();
  const panel = page.getByRole("dialog", { name: "Seen before" });
  await panel.getByRole("button", { name: "Move this into it…" }).click();
  await panel.getByRole("button", { name: "Move", exact: true }).click();

  await expect(page).toHaveURL(new RegExp(`note=${original}`));
  const editor = page.locator(editorSelector);
  await expect(editor).toContainText("Paint colour undecided.");
  await expect(editor).toContainText("contractor Miguel starts in April");
  await expect(page.getByRole("status").filter({ hasText: "Moved into" })).toBeVisible();
});
