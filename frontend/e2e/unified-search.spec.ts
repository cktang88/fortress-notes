import { expect, test } from "@playwright/test";
import { proxyToBackend } from "./backend";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

test("one search box finds notes by words and titles and jumps to the passage", async ({
  page,
}, testInfo) => {
  const backendUrl = String(testInfo.config.metadata.backendUrl);
  for (const [title, body] of [
    ["Porto weekend", "Try the francesinha near the river."],
    ["Reading list", "Finish the novel about the lighthouse keeper."],
  ]) {
    await page.request.post(`${backendUrl}/api/notes`, { data: { title, body } });
  }
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Embedding" })).toHaveCount(0);

  const search = page.getByRole("searchbox", { name: "Search notes" });
  await search.fill("lighthouse");
  const result = page.locator("[data-results] [data-list-item]").first();
  await expect(result).toContainText("Reading list");
  await expect(result.locator("mark")).toHaveText("lighthouse");

  // A word that only appears in a title still finds the note.
  await search.fill("porto");
  await expect(page.locator("[data-results] [data-list-item]").first()).toContainText(
    "Porto weekend",
  );
  await search.press("Enter");
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue("Porto weekend");
});

test("Enter never opens a result left over from the previous query", async ({ page }, testInfo) => {
  const backendUrl = String(testInfo.config.metadata.backendUrl);
  for (const [title, body] of [
    ["Kumquat jam", "Boil the kumquat slowly with sugar."],
    ["Persimmon tart", "Bake the persimmon tart at 180 degrees."],
  ]) {
    await page.request.post(`${backendUrl}/api/notes`, { data: { title, body } });
  }
  await page.goto("/");
  const search = page.getByRole("searchbox", { name: "Search notes" });
  await search.fill("kumquat");
  const first = page.locator("[data-results] [data-list-item]").first();
  await expect(first).toContainText("Kumquat jam");

  // Answer the new query slowly, so the old results are still on screen when Enter is pressed.
  await page.route("**/api/unified-search?*persimmon*", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 600));
    await route.fallback();
  });
  await search.fill("persimmon");
  await search.press("Enter");
  await expect(first).toContainText("Persimmon tart");
  // Opening a note puts it in the URL straight away; nothing was opened.
  expect(new URL(page.url()).searchParams.get("note")).toBeNull();

  await search.press("Enter");
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue("Persimmon tart");
});
