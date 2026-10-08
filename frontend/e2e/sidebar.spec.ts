import { expect, test, type Page } from "@playwright/test";
import { proxyToBackend, resetWorkspace } from "./backend";

test.beforeEach(async ({ page }, testInfo) => {
  await proxyToBackend(page, testInfo);
  // Drag gestures depend on the sidebar's layout, so start from an empty workspace.
  await resetWorkspace(page, testInfo);
});

interface NavigationNode {
  kind: "folder" | "document";
  id: string;
  name?: string;
  title?: string;
  parent_id?: string | null;
  folder_id?: string | null;
  children?: NavigationNode[];
}

test("manages nested notes with context menus and drag and drop across reloads", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 1200 });
  await page.goto("/");
  const sidebar = page.getByRole("navigation", { name: "Documents" });

  await sidebar.click({ button: "right", position: { x: 24, y: 24 } });
  await page.getByRole("menuitem", { name: "New folder" }).click();
  await page.getByRole("textbox", { name: /Folder name/ }).fill("Projects");
  await page.getByRole("textbox", { name: /Folder name/ }).press("Enter");

  const projects = page.getByRole("button", { name: "Folder: Projects" });
  await expect(projects).toBeVisible();
  await projects.click({ button: "right" });
  await page.getByRole("menuitem", { name: "New folder" }).click();
  await page.getByRole("textbox", { name: /Folder name/ }).fill("Research");
  await page.getByRole("textbox", { name: /Folder name/ }).press("Enter");

  const research = page.getByRole("button", { name: "Folder: Research" });
  await expect(research).toBeVisible();
  await research.click({ button: "right" });
  await page.getByRole("menuitem", { name: "New note" }).click();

  // The new note opens straight away; rename its row in the folder tree.
  // (the Recent list above the tree shows it too, so take the tree's row).
  const note = page.locator('[data-document-id][aria-current="page"]').last();
  await expect(note).toBeVisible();
  await note.dblclick();
  const renameInput = page.getByRole("textbox", { name: /^Rename / });
  await renameInput.fill("Research notes");
  await renameInput.press("Enter");
  await expect(
    page.locator('[data-document-id][aria-label="Research notes"]').first(),
  ).toBeVisible();

  await page.reload();
  const restoredProjects = page.getByRole("button", { name: "Folder: Projects" });
  const restoredResearch = page.getByRole("button", { name: "Folder: Research" });
  await expect(restoredProjects).toBeVisible();
  await expect(restoredResearch).toBeVisible();
  await expect(
    page.locator("[data-document-id]").filter({ hasText: "Research notes" }).first(),
  ).toBeVisible();
  let tree = await readNavigation(page);
  let projectsNode = tree.find((node) => node.name === "Projects");
  let researchNode = projectsNode?.children?.find((node) => node.name === "Research");
  expect(researchNode?.children?.some((node) => node.title === "Research notes")).toBe(true);

  await page.getByRole("button", { name: /All notes/ }).click({ button: "right" });
  await page.getByRole("menuitem", { name: "New folder" }).click();
  await page.getByRole("textbox", { name: /Folder name/ }).fill("Inbox");
  await page.getByRole("textbox", { name: /Folder name/ }).press("Enter");
  const inbox = page.getByRole("button", { name: "Folder: Inbox" });
  await expect(inbox).toHaveAttribute("aria-expanded", "true");

  const moveRequests: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/move")) {
      moveRequests.push(request.url());
    }
  });
  const noteRow = page.locator("[data-document-id]").filter({ hasText: "Research notes" }).last();
  await noteRow.scrollIntoViewIfNeeded();
  await dragPointer(page, noteRow, inbox);
  await expect(inbox).toHaveAttribute("aria-expanded", "true");
  await expect
    .poll(async () => {
      const refreshed = await readNavigation(page);
      const inboxNode = refreshed.find((node) => node.name === "Inbox");
      return inboxNode?.children?.some((node) => node.title === "Research notes");
    })
    .toBe(true);

  const nestedNote = page
    .locator("[data-document-id]")
    .filter({ hasText: "Research notes" })
    .last();
  await nestedNote.focus();
  await nestedNote.press("Space");
  await expect(nestedNote).toHaveAttribute("aria-pressed", "true");
  await nestedNote.press("Escape");
  await expect(nestedNote).toHaveAttribute("aria-pressed", "false");
  tree = await readNavigation(page);
  expect(
    tree
      .find((node) => node.name === "Inbox")
      ?.children?.some((node) => node.title === "Research notes"),
  ).toBe(true);

  const moveCountBeforeInvalidDrops = moveRequests.length;
  await dragPointer(page, restoredProjects, restoredResearch);
  await dragPointer(page, restoredResearch, restoredResearch);
  await dragPointer(page, restoredResearch, restoredProjects);
  expect(moveRequests).toHaveLength(moveCountBeforeInvalidDrops);
  await expect
    .poll(async () => {
      const refreshed = await readNavigation(page);
      return refreshed
        .find((node) => node.name === "Projects")
        ?.children?.some((node) => node.name === "Research");
    })
    .toBe(true);

  const researchInProjects = page.getByRole("button", { name: "Folder: Research" });
  await dragPointer(page, researchInProjects, inbox);
  await expect(inbox).toHaveAttribute("aria-expanded", "true");
  await expect
    .poll(async () => {
      const refreshed = await readNavigation(page);
      return refreshed
        .find((node) => node.name === "Inbox")
        ?.children?.some((node) => node.name === "Research");
    })
    .toBe(true);

  const researchInInbox = page.getByRole("button", { name: "Folder: Research" });
  await dragPointer(page, researchInInbox, page.getByRole("button", { name: /All notes/ }));
  await expect
    .poll(async () => {
      const refreshed = await readNavigation(page);
      return refreshed.find((node) => node.name === "Research")?.parent_id;
    })
    .toBe(null);

  await page.reload();
  tree = await readNavigation(page);
  researchNode = tree.find((node) => node.name === "Research");
  const inboxNode = tree.find((node) => node.name === "Inbox");
  expect(researchNode?.parent_id).toBe(null);
  expect(inboxNode?.children?.some((node) => node.title === "Research notes")).toBe(true);
});

async function readNavigation(page: Page): Promise<NavigationNode[]> {
  return page.evaluate(async () => {
    const response = await fetch("/api/navigation");
    if (!response.ok) throw new Error(`Navigation request failed: ${response.status}`);
    const result = (await response.json()) as { items: NavigationNode[] };
    return result.items;
  });
}

async function dragPointer(
  page: Page,
  source: ReturnType<Page["locator"]>,
  target: ReturnType<Page["locator"]>,
) {
  await target.scrollIntoViewIfNeeded();
  await source.scrollIntoViewIfNeeded();
  const sourceBox = await source.boundingBox();
  const targetBox = await target.boundingBox();
  if (!sourceBox || !targetBox) throw new Error("Drag source or destination is not visible.");
  await page.mouse.move(sourceBox.x + sourceBox.width / 2, sourceBox.y + sourceBox.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(250);
  await page.mouse.move(
    sourceBox.x + sourceBox.width / 2 + 12,
    sourceBox.y + sourceBox.height / 2 + 8,
    { steps: 3 },
  );
  await page.mouse.move(targetBox.x + targetBox.width / 2, targetBox.y + targetBox.height / 2, {
    steps: 24,
  });
  await page.waitForTimeout(250);
  await page.mouse.up();
}
