import { expect, test, type Locator, type Page } from "@playwright/test";
import { proxyToBackend } from "./backend";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

test("keeps editor controls aligned and limits review to highlighted blocks", async ({ page }) => {
  await page.goto("/");
  const document = await createEditorDocument(page);
  const paragraph = document.children.find((block) => block.type === "paragraph");
  if (!paragraph) throw new Error("The test note has no paragraph block");

  let linkCheckCalls = 0;
  await page.route("**/api/block-documents/*/link-checks", async (route) => {
    linkCheckCalls += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        links: [
          {
            url: "https://broken.example.test/",
            status: "404 Not Found",
            checked_at: "2026-09-29T12:00:00Z",
            block_ids: [paragraph.id],
          },
        ],
      }),
    });
  });
  let reviewBody: Record<string, unknown> | null = null;
  await page.route("**/api/block-documents/*/review-context", async (route) => {
    reviewBody = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        kind: "factcheck",
        context: "selected",
        summary: "Checked",
        items: [],
      }),
    });
  });

  const noteLevelRelatedRequest = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return (
      url.pathname.endsWith(`/notes/${document.id}/related`) && !url.searchParams.has("block_id")
    );
  });
  await page.goto(`/?note=${document.id}`);
  await noteLevelRelatedRequest;
  const editor = page.locator('[contenteditable="true"][aria-label="Document content"]');
  await expect(editor).toBeVisible();

  const heading = editor
    .locator(".bn-block-outer")
    .filter({ hasText: "Alignment heading" })
    .first();
  const paragraphBlock = editor
    .locator(".bn-block-outer")
    .filter({ hasText: "Alignment paragraph" })
    .first();
  await expect(heading.locator("h1")).toBeVisible();
  await expect(paragraphBlock.locator("p")).toBeVisible();

  await expect.poll(() => linkCheckCalls).toBe(1);
  await expect(paragraphBlock.locator('a[href="https://broken.example.test/"]')).toHaveAttribute(
    "data-link-status",
    "404 Not Found",
  );

  await expect(page.getByRole("button", { name: /fact-check document|lint/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /tag/i })).toHaveCount(0);

  await assertSideMenuAligned(page, heading);
  await assertSideMenuAligned(page, paragraphBlock);

  const relatedRequest = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return (
      url.pathname.endsWith(`/notes/${document.id}/related`) &&
      url.searchParams.get("block_id") === paragraph.id
    );
  });
  await paragraphBlock.locator("p").click({ position: { x: 100, y: 8 } });
  await relatedRequest;

  await expect(page.getByRole("region", { name: "Fact-check selected blocks" })).toHaveCount(0);
  for (let index = 0; index < 12; index += 1) {
    await page.keyboard.press("Shift+ArrowRight");
  }
  const reviewPanel = page.getByRole("region", { name: "Fact-check selected blocks" });
  await expect(reviewPanel).toBeVisible();
  await expect(reviewPanel).toContainText("1 block selected");
  const reviewRequest = page.waitForRequest((request) => request.url().includes("/review-context"));
  await reviewPanel.getByRole("button", { name: "Fact-check selection" }).click();
  await reviewRequest;
  expect(reviewBody).toMatchObject({
    kind: "factcheck",
    context: "selected",
    block_ids: [paragraph.id],
  });

  await heading.locator("h1").click();
  await expect(reviewPanel).toHaveCount(0);
  await page.getByRole("button", { name: "Help and tips" }).click();
  const help = page.getByRole("dialog", { name: "Quick tips" });
  await expect(help).toBeVisible();
  await expect(help).toContainText("Highlight text");
  await expect(help).toContainText("checked automatically");
  const closeHelp = help.getByRole("button", { name: "Close help" });
  await expect(closeHelp).toBeFocused();
  await page.keyboard.press("Tab");
  await expect.poll(() => help.evaluate((dialog) => dialog.matches(":modal"))).toBe(true);
  expect(
    await help.evaluate(
      (dialog) =>
        dialog.contains(document.activeElement) || document.activeElement === document.body,
    ),
  ).toBe(true);
  await page.keyboard.press("Shift+Tab");
  expect(
    await help.evaluate(
      (dialog) =>
        dialog.contains(document.activeElement) || document.activeElement === document.body,
    ),
  ).toBe(true);
  await page.keyboard.press("Escape");
  await expect(help).toBeHidden();
  await expect(page.getByRole("button", { name: "Help and tips" })).toBeFocused();
});

interface TestBlock {
  id: string;
  type: string;
}

interface TestDocument {
  id: string;
  children: TestBlock[];
}

async function createEditorDocument(page: Page): Promise<TestDocument> {
  return page.evaluate(async () => {
    const response = await fetch("/api/notes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: "Editor usability fixture",
        status: "rough",
        body: "# Alignment heading\n\nAlignment paragraph with [a broken link](https://broken.example.test/)\n\nA second paragraph for text selection.",
      }),
    });
    if (!response.ok) throw new Error(`Could not create test document: ${response.status}`);
    const note = (await response.json()) as { id: string };
    const documentResponse = await fetch(`/api/block-documents/${note.id}`);
    if (!documentResponse.ok) {
      throw new Error(`Could not load test document: ${documentResponse.status}`);
    }
    return (await documentResponse.json()) as TestDocument;
  });
}

async function assertSideMenuAligned(page: Page, block: Locator) {
  const hoveredAt = Date.now();
  await block.hover();
  const sideMenu = page.locator(".bn-side-menu");
  await page.waitForTimeout(500);
  await expect(sideMenu).toBeHidden();
  await expect(sideMenu).toBeVisible();
  expect(Date.now() - hoveredAt).toBeGreaterThanOrEqual(850);
  const geometry = await block.evaluate((element) => {
    const textBlock = element.querySelector("h1, h2, h3, p");
    const menu = document.querySelector(".bn-side-menu");
    if (!textBlock || !menu) throw new Error("Block text or side menu is missing");
    const range = document.createRange();
    range.selectNodeContents(textBlock);
    const rects = Array.from(range.getClientRects()).filter(
      (rect) => rect.width > 0 && rect.height > 0,
    );
    if (rects.length === 0) throw new Error("Block has no visible text line");
    const firstTop = Math.min(...rects.map((rect) => rect.top));
    const firstLine = rects.filter((rect) => Math.abs(rect.top - firstTop) < 1);
    const lineCenter =
      (Math.min(...firstLine.map((rect) => rect.top)) +
        Math.max(...firstLine.map((rect) => rect.bottom))) /
      2;
    const menuRect = menu.getBoundingClientRect();
    const menuCenter = (menuRect.top + menuRect.bottom) / 2;
    return {
      verticalDifference: Math.abs(lineCenter - menuCenter),
      menuRight: menuRect.right,
      textLeft: Math.min(...firstLine.map((rect) => rect.left)),
    };
  });
  expect(geometry.verticalDifference).toBeLessThanOrEqual(3);
  expect(geometry.menuRight).toBeLessThanOrEqual(geometry.textLeft);
}
