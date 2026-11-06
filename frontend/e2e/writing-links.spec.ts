import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { proxyToBackend } from "./backend";
import { uniqueWord } from "./notes";

const editorSelector = '[contenteditable="true"][aria-label="Document content"]';

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

interface ApiBlock {
  id: string;
  text: string;
}

async function createNote(page: Page, testInfo: TestInfo, title: string, body: string) {
  const api = `${String(testInfo.config.metadata.backendUrl)}/api`;
  const note = (await (
    await page.request.post(`${api}/notes`, { data: { title, body } })
  ).json()) as { id: string };
  const document = (await (await page.request.get(`${api}/block-documents/${note.id}`)).json()) as {
    children: ApiBlock[];
  };
  return { id: note.id, blocks: document.children };
}

async function newTitledNote(page: Page, title: string) {
  const previous = new URL(page.url()).searchParams.get("note");
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  // Wait for the new note: until it opens, the title box is the previous note's.
  await expect.poll(() => new URL(page.url()).searchParams.get("note")).not.toBe(previous);
  const titleInput = page.getByRole("textbox", { name: "Note title" });
  await titleInput.fill(title);
  await titleInput.press("Enter");
  const editor = page.locator(editorSelector);
  await expect(editor).toBeFocused();
  return editor;
}

async function allSaved(page: Page) {
  const noteId = new URL(page.url()).searchParams.get("note");
  await expect
    .poll(() =>
      page.evaluate(
        (id) => localStorage.getItem(`fortress-notes:pending-block-transactions:${id}`),
        noteId,
      ),
    )
    .toBeNull();
  await expect(page.getByRole("status").filter({ hasText: "Saving" })).toHaveCount(0);
}

function backlinks(page: Page) {
  return page.getByText("Backlinks", { exact: true }).locator("..");
}

test("[[ links to another note and the target's backlinks show it", async ({ page }, testInfo) => {
  const suffix = uniqueWord("lnk");
  const target = await createNote(
    page,
    testInfo,
    `Greenhouse wiring ${suffix}`,
    "Run the heater off its own circuit.",
  );
  await page.goto(`/?note=${target.id}`);
  await expect(backlinks(page)).toContainText("No backlinks yet.");

  const editor = await newTitledNote(page, `Winter prep ${suffix}`);
  await page.keyboard.type("Before frost, check ");
  await page.keyboard.type("[[");
  await page.keyboard.type(`Greenhouse wiring ${suffix}`);
  const option = page.getByRole("option", { name: new RegExp(`Greenhouse wiring ${suffix}`) });
  await expect(option).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("option")).toHaveCount(0);
  await expect(editor).toContainText(`Greenhouse wiring ${suffix}`);
  await expect(editor).not.toContainText(`[[Greenhouse wiring ${suffix}`);
  await page.keyboard.type(" again.");
  await allSaved(page);

  await page.reload();
  await expect(page.locator(editorSelector)).toContainText(`Greenhouse wiring ${suffix}`);
  await expect(page.locator(editorSelector)).toContainText("again.");

  await page.goto(`/?note=${target.id}`);
  const panel = backlinks(page);
  await expect(panel).toContainText(`Winter prep ${suffix}`);
  // Following the backlink opens the note that links here.
  await panel.getByRole("button", { name: new RegExp(`Winter prep ${suffix}`) }).click();
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue(
    `Winter prep ${suffix}`,
  );
});

test("(( references a block, previews it on hover, and records a backlink", async ({
  page,
}, testInfo) => {
  const suffix = uniqueWord("lnk");
  const target = await createNote(
    page,
    testInfo,
    `Bread ratios ${suffix}`,
    `Hydration seventy percent for ciabatta ${suffix}\n\nSalt two percent always.`,
  );
  await page.goto("/");
  const editor = await newTitledNote(page, `Baking day ${suffix}`);
  await page.keyboard.type("Today's dough: ");
  await page.keyboard.type("((");
  await page.keyboard.type(`ciabatta ${suffix}`);
  const option = page.getByRole("option", {
    name: new RegExp(`Hydration seventy percent for ciabatta ${suffix}`),
  });
  await expect(option).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("option")).toHaveCount(0);
  await allSaved(page);

  // Hovering the reference previews the referenced paragraph.
  const reference = editor.locator("a").filter({ hasText: /Hydration seventy percent/ });
  await expect(reference).toBeVisible();
  await reference.hover();
  const preview = page.getByRole("status").filter({ hasText: `Bread ratios ${suffix}` });
  await expect(preview).toContainText(`Hydration seventy percent for ciabatta ${suffix}`);
  await page.mouse.move(5, 5);
  await expect(preview).toHaveCount(0);

  await page.reload();
  await expect(
    page
      .locator(editorSelector)
      .locator("a")
      .filter({ hasText: /Hydration seventy percent/ }),
  ).toBeVisible();
  await page.goto(`/?note=${target.id}`);
  await expect(backlinks(page)).toContainText(`Baking day ${suffix}`);
});

test("a link that fails its check is marked, and the mark survives a reload", async ({
  page,
}, testInfo) => {
  const suffix = uniqueWord("lnk");
  const note = await createNote(
    page,
    testInfo,
    `Reading links ${suffix}`,
    "See [the old wiki](https://wiki.gone.example.test/page) and [the docs](https://docs.example.test/).",
  );
  const blockId = note.blocks[0]!.id;
  await page.route("**/api/block-documents/*/link-checks", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        links: [
          {
            url: "https://wiki.gone.example.test/page",
            status: "404",
            checked_at: "2026-10-01T00:00:00Z",
            block_ids: [blockId],
          },
          {
            url: "https://docs.example.test/",
            status: "ok",
            checked_at: "2026-10-01T00:00:00Z",
            block_ids: [blockId],
          },
        ],
      }),
    }),
  );
  await page.goto(`/?note=${note.id}`);
  const editor = page.locator(editorSelector);
  const broken = editor.locator('a[href="https://wiki.gone.example.test/page"]');
  const fine = editor.locator('a[href="https://docs.example.test/"]');
  await expect(broken).toHaveAttribute("data-link-status", "404");
  await expect(broken).toHaveAttribute("title", "Link check: 404");
  await expect(fine).not.toHaveAttribute("data-link-status", /.*/);

  // Editing the paragraph keeps the warning on the broken link.
  await editor.locator("p").first().click();
  await page.keyboard.press("End");
  await page.keyboard.type(" Updated.");
  await expect(broken).toHaveAttribute("data-link-status", "404");
  await allSaved(page);
  await page.reload();
  await expect(page.locator(editorSelector)).toContainText("Updated.");
  await expect(
    page.locator(editorSelector).locator('a[href="https://wiki.gone.example.test/page"]'),
  ).toHaveAttribute("data-link-status", "404");
});
