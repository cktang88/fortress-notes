import { expect, test, type Page } from "@playwright/test";
import { proxyToBackend } from "./backend";
import { createNote, focusedBlockId, noteTitle, uniqueWord } from "./notes";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

const searchBox = (page: Page) => page.getByRole("searchbox", { name: "Search notes" });
const results = (page: Page) => page.locator("[data-results] [data-list-item]");

test("search jumps to the matching passage, and words, titles and meaning are labelled", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("kay");
  const note = await createNote(page, testInfo, {
    title: `Lake trip ${word}`,
    body: [
      "Pack the tent and the stove.",
      "Bring sunscreen for the afternoon.",
      `Rent a ${word} paddle at the boathouse before noon.`,
      "Dinner at the lodge.",
    ].join("\n\n"),
  });
  const paddle = note.blocks[2];
  await page.goto("/");

  // Exact words: the passage is shown with the word highlighted.
  await searchBox(page).fill(`${word} paddle`);
  // Earlier runs leave similar notes behind; look at this note's passage.
  const passage = `${word} paddle at the boathouse`;
  const hit = results(page).filter({ hasText: passage });
  await expect(hit).toHaveCount(1);
  await expect(hit).toContainText("exact words");
  await expect(hit.locator("mark")).toHaveText([word, "paddle"]);

  // Clicking opens the note with the cursor in that paragraph, not at the top.
  await hit.click();
  await expect(noteTitle(page)).toHaveValue(note.title);
  await expect(page).toHaveURL(new RegExp(`#block=${paddle.id}`));
  await expect.poll(() => focusedBlockId(page)).toBe(paddle.id);

  // A word that isn't in the passage still finds it by meaning (some words overlap).
  await searchBox(page).fill(`${word} canoe`);
  const loose = results(page).filter({ hasText: passage });
  await expect(loose).toContainText("similar meaning");
  await expect(loose).not.toContainText("exact words");

  // Only the title matches.
  await searchBox(page).fill(`lake trip ${word}`);
  // (Other notes with "trip" in their title may rank alongside it, so look at this one's.)
  const titleHit = results(page).filter({ hasText: note.title }).first();
  await expect(titleHit).toContainText("in title");
  await expect(titleHit).not.toContainText("exact words");
});

test("punctuation, quotes, operators and other scripts never break search", async ({
  page,
}, testInfo) => {
  const word = uniqueWord("pun");
  await createNote(page, testInfo, {
    title: `Café notes ${word}`,
    body: `Crème brûlée at the café ${word}.\n\nNaïve “quotes” and C++ and 東京 ${word}.`,
  });
  const failures: string[] = [];
  page.on("response", (response) => {
    if (response.url().includes("/api/unified-search") && !response.ok())
      failures.push(`${response.status()} ${response.url()}`);
  });
  await page.goto("/");
  const search = searchBox(page);

  for (const query of [
    `"${word}`,
    `${word}"`,
    `'${word}'`,
    `${word} AND`,
    `${word} OR NOT`,
    `${word}*`,
    `(${word}`,
    `${word} -`,
    `${word}:`,
    `${word} ^ NEAR(`,
    `“${word}”`,
    `%${word}_`,
    `${word}\\`,
  ]) {
    await search.fill(query);
    await expect(results(page).first(), query).toContainText(`Café notes ${word}`);
  }
  // Accents and other scripts match as typed.
  await search.fill(`brûlée ${word}`);
  await expect(results(page).first().locator("mark").first()).toHaveText(/brûlée/i);
  await search.fill(`東京 ${word}`);
  await expect(
    results(page)
      .filter({ hasText: `東京 ${word}` })
      .locator("mark"),
  ).toHaveText(["東京", word]);

  // Nothing but punctuation is a calm "Nothing matches", not an error.
  for (const query of [`"`, "'''", "***", "()", "-", "“”", "\\"]) {
    await search.fill(query);
    await expect(page.getByText("Nothing matches.")).toBeVisible();
  }
  await expect(page.getByText("Search is unavailable right now.")).toHaveCount(0);
  expect(failures).toEqual([]);
});

test("results always match the final query when typing fast", async ({ page }, testInfo) => {
  const first = uniqueWord("fst");
  const second = uniqueWord("snd");
  await createNote(page, testInfo, { title: `Alpha ${first}`, body: `About ${first}.` });
  await createNote(page, testInfo, { title: `Beta ${second}`, body: `About ${second}.` });
  // Make the search for the first word slow, so its reply comes back last.
  await page.route("**/api/unified-search**", async (route) => {
    if (new URL(route.request().url()).searchParams.get("q") === first)
      await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.fallback();
  });
  await page.goto("/");
  const search = searchBox(page);
  await search.fill(first);
  await page.waitForRequest((request) => request.url().includes(`q=${first}`));
  await search.fill("");
  await search.pressSequentially(second, { delay: 15 });
  await expect(results(page).first()).toContainText(`Beta ${second}`);
  // Wait until the slow reply has arrived, then make sure it didn't take over.
  await page.waitForResponse((response) => response.url().includes(`q=${first}`));
  await expect(results(page).filter({ hasText: `Alpha ${first}` })).toHaveCount(0);
  await expect(results(page).first()).toContainText(`Beta ${second}`);

  // Pressing Enter right after typing opens the result for what was typed.
  await search.fill("");
  await search.pressSequentially(first, { delay: 5 });
  await search.press("Enter");
  await expect(noteTitle(page)).toHaveValue(`Alpha ${first}`);
});

test("filters narrow the results and saved searches remember them", async ({ page }, testInfo) => {
  const word = uniqueWord("flt");
  const rough = await createNote(page, testInfo, {
    title: `Rough ${word}`,
    body: `# Heading ${word}\n\nParagraph ${word}.`,
  });
  await createNote(page, testInfo, {
    title: `Polished ${word}`,
    body: `Polished paragraph ${word}.`,
    status: "polished",
  });
  await page.goto("/");
  const search = searchBox(page);
  await search.fill(word);
  await expect(results(page)).toHaveCount(3);

  await page.getByRole("combobox", { name: "Document status" }).selectOption("polished");
  await expect(results(page)).toHaveCount(1);
  await expect(results(page).first()).toContainText(`Polished ${word}`);

  await page.getByRole("combobox", { name: "Document status" }).selectOption("");
  await page.getByRole("combobox", { name: "Block type" }).selectOption("heading");
  await expect(results(page)).toHaveCount(1);
  await expect(results(page).first()).toContainText(`Heading ${word}`);

  await page.getByRole("combobox", { name: "Block type" }).selectOption("");
  await page
    .getByRole("combobox", { name: "Document", exact: true })
    .selectOption({ label: rough.title });
  await expect(results(page)).toHaveCount(2);
  await expect(results(page).filter({ hasText: "Polished" })).toHaveCount(0);

  // A date range that excludes everything.
  await page.getByLabel("Updated before").fill("2001-01-01");
  await expect(page.getByText("Nothing matches.")).toBeVisible();
  await page.getByLabel("Updated before").fill("");
  await expect(results(page)).toHaveCount(2);

  // Save the filtered search; the chip brings back the query and the filter.
  await page.getByRole("button", { name: "☆ Save search" }).click();
  await expect(page.getByRole("button", { name: "★ Saved" })).toBeDisabled();
  await search.press("Escape");
  await page.getByRole("combobox", { name: "Document", exact: true }).selectOption("");
  await page.reload();
  const chip = page.getByRole("group", { name: "Saved searches" }).getByRole("button", {
    name: `★ ${word}`,
  });
  await chip.click();
  await expect(search).toHaveValue(word);
  await expect(page.getByRole("combobox", { name: "Document", exact: true })).toHaveValue(rough.id);
  await expect(results(page)).toHaveCount(2);
  await expect(page.getByRole("button", { name: "★ Saved" })).toBeDisabled();

  // Forget it: gone after reload too.
  await search.press("Escape");
  await page.getByRole("combobox", { name: "Document", exact: true }).selectOption("");
  await page
    .getByRole("group", { name: "Saved searches" })
    .getByRole("button", { name: `Forget saved search ${word}` })
    .click();
  await expect(chip).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("button", { name: `★ ${word}` })).toHaveCount(0);
});

test("/ and the keyboard drive search without the mouse", async ({ page }, testInfo) => {
  const word = uniqueWord("kbd");
  const one = await createNote(page, testInfo, { title: `One ${word}`, body: `First ${word}.` });
  const two = await createNote(page, testInfo, { title: `Two ${word}`, body: `Second ${word}.` });
  await page.goto(`/?note=${one.id}`);
  await expect(noteTitle(page)).toHaveValue(one.title);

  // "/" focuses search from anywhere outside a text field...
  await page.locator("body").press("/");
  const search = searchBox(page);
  await expect(search).toBeFocused();
  await page.keyboard.type(`Second ${word}`);
  await expect(results(page).first()).toContainText(two.title);

  // ...arrow keys walk the results, Up from the first goes back to the box.
  await page.keyboard.press("ArrowDown");
  await expect(results(page).first()).toBeFocused();
  await page.keyboard.press("ArrowUp");
  await expect(search).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(noteTitle(page)).toHaveValue(two.title);
  await expect.poll(() => focusedBlockId(page)).toBe(two.blocks[0].id);

  // "/" typed inside the note is just a character, not a shortcut.
  await page.keyboard.press("End");
  await page.keyboard.type(" a/b");
  await expect(search).not.toBeFocused();
  await expect(page.locator('[aria-label="Document content"]')).toContainText("a/b");

  // Escape clears the search and returns to the folder tree.
  await page.locator("body").click({ position: { x: 5, y: 5 } });
  await page.keyboard.press("/");
  await expect(search).toBeFocused();
  await expect(search).toHaveValue(`Second ${word}`);
  await page.keyboard.press("Escape");
  await expect(search).toHaveValue("");
  await expect(page.getByRole("navigation", { name: "Documents" })).toBeVisible();
});
