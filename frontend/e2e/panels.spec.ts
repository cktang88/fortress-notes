import { expect, test, type Page } from "@playwright/test";
import { proxyToBackend } from "./backend";
import { createNote, focusedBlockId, noteTitle, openApp, uniqueWord } from "./notes";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

/** A sidebar panel, found by its heading text (Outline, Backlinks, Related …). */
const panel = (page: Page, heading: RegExp) =>
  page
    .locator("aside > div.border-t")
    .filter({ has: page.locator("div", { hasText: heading }) })
    .last();

test("the outline lists the note's headings and jumps to each one", async ({ page }, testInfo) => {
  const word = uniqueWord("out");
  const trouble = uniqueWord("trb");
  const filler = Array.from({ length: 30 }, (_, index) => `Filler paragraph ${index + 1}.`);
  const note = await createNote(page, testInfo, {
    title: `Handbook ${word}`,
    body: [
      "# Getting started",
      ...filler.slice(0, 15),
      "## Installing",
      ...filler.slice(15),
      `# ${trouble}`,
      "Turn it off and on again.",
    ].join("\n\n"),
  });
  const headingIds = note.blocks.filter((block) => block.type === "heading").map((b) => b.id);
  // Related to the note as a whole, but not to its last heading (a word no other note has).
  await createNote(page, testInfo, {
    title: `Handbook draft ${word}`,
    body: `Handbook ${word} filler paragraph.`,
  });
  await openApp(page);
  const outline = panel(page, /^Outline$/);
  await expect(outline).toContainText("Open a note to see its outline.");

  await page.goto(`/?note=${note.id}`);
  await expect(noteTitle(page)).toHaveValue(note.title);
  await expect(outline.getByRole("button")).toHaveText(["Getting started", "Installing", trouble]);

  // The last heading is far below the fold; the outline scrolls it into view.
  const last = page.locator(`.bn-block[data-id="${headingIds[2]}"]`);
  await expect(last).not.toBeInViewport();
  await outline.getByRole("button", { name: trouble }).click();
  await expect(last).toBeInViewport();
  await expect.poll(() => focusedBlockId(page)).toBe(headingIds[2]);
  // The jump is kept in the URL, so the link can be shared.
  await expect(page).toHaveURL(new RegExp(`#block=${headingIds[2]}`));

  // Clicking another entry like a person does (a short press) still lands, even
  // though leaving the editor changes what the Related panel below shows.
  await expect(panel(page, /^Related to this block$/)).toContainText("Nothing related yet.");
  await outline.getByRole("button", { name: "Getting started" }).click({ delay: 150 });
  await expect.poll(() => focusedBlockId(page)).toBe(headingIds[0]);

  // A heading added in the editor shows up in the outline.
  await page.keyboard.press("End");
  await page.keyboard.press("Enter");
  await page.keyboard.type("## Fresh section");
  await expect(outline.getByRole("button", { name: "Fresh section" })).toBeVisible();

  // A note without headings says so.
  const plain = await createNote(page, testInfo, { title: `Plain ${word}`, body: "Just text." });
  await page.goto(`/?note=${plain.id}`);
  await expect(outline).toContainText("No headings yet.");
});

test("backlinks show who links here and open the linking paragraph", async ({ page }, testInfo) => {
  const word = uniqueWord("bkl");
  const target = await createNote(page, testInfo, {
    title: `Glossary ${word}`,
    body: "Definitions live here.",
  });
  const source = await createNote(page, testInfo, {
    title: `Essay ${word}`,
    body: `Opening thoughts.\n\nAs defined in [[${target.id}|the glossary]], words matter.`,
  });
  await openApp(page, `/?note=${target.id}`);
  const backlinks = panel(page, /^Backlinks$/);
  const link = backlinks.getByRole("button").filter({ hasText: source.title });
  await expect(link).toContainText("the glossary");
  await link.click();
  await expect(noteTitle(page)).toHaveValue(source.title);
  await expect.poll(() => focusedBlockId(page)).toBe(source.blocks[1].id);
  // The essay itself has no backlinks.
  await expect(backlinks).toContainText("No backlinks yet.");

  // Back returns to the glossary, with its backlink still listed.
  await page.goBack();
  await expect(noteTitle(page)).toHaveValue(target.title);
  await expect(link).toBeVisible();
});

test("related notes follow the note and the focused paragraph", async ({ page }, testInfo) => {
  const word = uniqueWord("rel");
  const other = uniqueWord("oth");
  const base = await createNote(page, testInfo, {
    title: `Sourdough ${word}`,
    body: `${word} starter.\n\n${other} oven.`,
  });
  const starter = await createNote(page, testInfo, {
    title: `Starter care ${word}`,
    body: `Intro.\n\nRye for the ${word} starter.`,
  });
  const oven = await createNote(page, testInfo, {
    title: `Oven notes ${other}`,
    body: `Preheat ${other} oven.`,
  });
  await openApp(page, `/?note=${base.id}`);
  const related = panel(page, /^Related (notes|to this block)$/);
  await expect(related.getByRole("button").filter({ hasText: starter.title })).toBeVisible();

  // With the cursor in the oven paragraph, the oven note comes first.
  await page.locator(`.bn-block[data-id="${base.blocks[1].id}"]`).click();
  await expect(related).toContainText("Related to this block");
  await expect(related.getByRole("button").first()).toContainText(oven.title);

  // With the cursor in the starter paragraph, the starter note comes first;
  // clicking opens it at the matching paragraph.
  await page.locator(`.bn-block[data-id="${base.blocks[0].id}"]`).click();
  const first = related.getByRole("button").first();
  await expect(first).toContainText(starter.title);
  await expect(first).toContainText("Matched: Rye for the");
  await first.click();
  await expect(noteTitle(page)).toHaveValue(starter.title);
  await expect.poll(() => focusedBlockId(page)).toBe(starter.blocks[1].id);
});
