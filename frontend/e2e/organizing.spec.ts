import { expect, test, type Locator, type Page } from "@playwright/test";
import { proxyToBackend, resetWorkspace } from "./backend";
import { apiUrl, createFolder, createNote, noteTitle, openApp, sidebar, treeRow } from "./notes";

test.beforeEach(async ({ page }, testInfo) => {
  await proxyToBackend(page, testInfo);
  // These specs read and drag rows by position, so start from an empty workspace.
  await resetWorkspace(page, testInfo);
  await page.setViewportSize({ width: 1280, height: 1200 });
});

interface NavNode {
  kind: "folder" | "document";
  id: string;
  name?: string;
  title?: string;
  parent_id?: string | null;
  folder_id?: string | null;
  children?: NavNode[];
}

async function navigation(page: Page, testInfo: Parameters<typeof apiUrl>[0]) {
  const response = await page.request.get(`${apiUrl(testInfo)}/navigation`);
  return ((await response.json()) as { items: NavNode[]; recent: NavNode[] }).items;
}

function find(nodes: NavNode[], label: string): NavNode | undefined {
  for (const node of nodes) {
    if (node.name === label || node.title === label) return node;
    const inner = find(node.children ?? [], label);
    if (inner) return inner;
  }
  return undefined;
}

const folder = (page: Page, name: string) => page.getByRole("button", { name: `Folder: ${name}` });
const menuItem = (page: Page, name: string) =>
  page.getByRole("menu", { name: "Document actions" }).getByRole("menuitem", { name });

async function drag(page: Page, source: Locator, target: Locator) {
  await source.scrollIntoViewIfNeeded();
  const from = await source.boundingBox();
  const to = await target.boundingBox();
  if (!from || !to) throw new Error("Drag source or target is not visible.");
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(from.x + from.width / 2 + 10, from.y + from.height / 2 + 6, { steps: 3 });
  await page.mouse.move(to.x + to.width / 2, to.y + to.height / 2, { steps: 20 });
  await expect(target.locator("xpath=ancestor-or-self::*[@data-drop-target]").first()).toHaveClass(
    /indigo/,
  );
  await page.mouse.up();
}

test("folders are created, renamed and deleted from context menus, and survive a reload", async ({
  page,
}, testInfo) => {
  await openApp(page);
  await expect(sidebar(page)).toContainText("No documents. Right-click to add one.");

  // The + button opens the same menu as right-clicking the sidebar.
  await page.getByRole("button", { name: "Add to top level" }).click();
  await menuItem(page, "New folder").click();
  const name = page.getByRole("textbox", { name: "Folder name" });
  await expect(name).toBeFocused();
  // Blank names aren't saved; Cancel leaves nothing behind.
  await name.press("Enter");
  await expect(name).toBeVisible();
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(name).toHaveCount(0);

  await page.getByRole("button", { name: "Add to top level" }).click();
  await menuItem(page, "New folder").click();
  await name.fill("Work");
  await name.press("Enter");
  await expect(folder(page, "Work")).toBeVisible();

  // A folder inside it, and a note inside that.
  await folder(page, "Work").click({ button: "right" });
  await menuItem(page, "New folder").click();
  await name.fill("Meetings");
  await name.press("Enter");
  await folder(page, "Meetings").click({ button: "right" });
  await menuItem(page, "New note").click();
  await expect(noteTitle(page)).toBeVisible();
  await noteTitle(page).fill("Standup notes");
  await noteTitle(page).press("Enter");
  await expect(treeRow(page, "Standup notes")).toBeVisible();

  // Rename the folder from its menu; Escape on a second rename keeps the name.
  await folder(page, "Meetings").click({ button: "right" });
  await menuItem(page, "Rename").click();
  const rename = page.getByRole("textbox", { name: "Rename Meetings" });
  await expect(rename).toBeFocused();
  await rename.fill("Team meetings");
  await rename.press("Enter");
  await expect(folder(page, "Team meetings")).toBeVisible();
  await folder(page, "Team meetings").dblclick();
  await page.getByRole("textbox", { name: "Rename Team meetings" }).fill("Oops");
  await page.getByRole("textbox", { name: "Rename Team meetings" }).press("Escape");
  await expect(folder(page, "Team meetings")).toBeVisible();
  await expect(folder(page, "Oops")).toHaveCount(0);

  // A folder with something in it can't be deleted.
  await folder(page, "Work").click({ button: "right" });
  await expect(menuItem(page, "Delete folder")).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu", { name: "Document actions" })).toHaveCount(0);

  await page.reload();
  await expect(folder(page, "Work")).toBeVisible();
  await expect(folder(page, "Team meetings")).toBeVisible();
  await expect(treeRow(page, "Standup notes")).toBeVisible();
  const tree = await navigation(page, testInfo);
  expect(find(tree, "Work")?.children?.map((node) => node.name)).toEqual(["Team meetings"]);
  expect(find(tree, "Team meetings")?.children?.map((node) => node.title)).toEqual([
    "Standup notes",
  ]);

  // Collapsing hides the children; expanding shows them again.
  await folder(page, "Work").click();
  await expect(folder(page, "Work")).toHaveAttribute("aria-expanded", "false");
  await expect(folder(page, "Team meetings")).toHaveCount(0);
  await folder(page, "Work").click();
  await expect(folder(page, "Team meetings")).toBeVisible();

  // Trashing the note empties the folder, which can then be deleted.
  await treeRow(page, "Standup notes").click({ button: "right" });
  await menuItem(page, "Move to Trash").click();
  await expect(treeRow(page, "Standup notes")).toHaveCount(0);
  await expect(
    page.getByRole("status").filter({ hasText: "Moved “Standup notes” to Trash" }),
  ).toBeVisible();
  await folder(page, "Team meetings").click({ button: "right" });
  await menuItem(page, "Delete folder").click();
  await expect(folder(page, "Team meetings")).toHaveCount(0);
  await folder(page, "Work").click({ button: "right" });
  await menuItem(page, "Delete folder").click();
  await expect(folder(page, "Work")).toHaveCount(0);

  // Restoring the note whose folder is gone puts it back at the top level.
  await page.getByRole("button", { name: "Workspace menu" }).click();
  await page.getByRole("menuitem", { name: "Trash" }).click();
  const trash = page.getByRole("dialog", { name: "Trash" });
  await trash
    .getByRole("listitem")
    .filter({ hasText: "Standup notes" })
    .getByRole("button", { name: "Restore" })
    .click();
  await trash.getByRole("button", { name: "Close" }).click();
  await expect(treeRow(page, "Standup notes")).toBeVisible();
  expect(find(await navigation(page, testInfo), "Standup notes")?.folder_id).toBeNull();
  await page.reload();
  await expect(treeRow(page, "Standup notes")).toBeVisible();
  await expect(folder(page, "Work")).toHaveCount(0);
});

test("notes are renamed from the tree and the Recent list, and moved to the top level", async ({
  page,
}, testInfo) => {
  const archive = await createFolder(page, testInfo, "Archive");
  const note = await createNote(page, testInfo, {
    title: "Old plan",
    body: "Draft.",
    folderId: archive,
  });
  await openApp(page);
  const recent = page.getByRole("region", { name: "Recent documents" });
  await expect(recent.getByRole("button", { name: "Old plan" })).toBeVisible();

  // Rename from the Recent list: the tree row and the open note follow.
  await page.goto(`/?note=${note.id}`);
  await expect(noteTitle(page)).toHaveValue("Old plan");
  await recent.getByRole("button", { name: "Old plan" }).dblclick();
  await page.getByRole("textbox", { name: "Rename Old plan" }).fill("New plan");
  await page.getByRole("textbox", { name: "Rename Old plan" }).press("Enter");
  await expect(treeRow(page, "New plan")).toBeVisible();
  await expect(recent.getByRole("button", { name: "New plan" })).toBeVisible();
  await expect(noteTitle(page)).toHaveValue("New plan");

  // Rename from the tree's context menu.
  await treeRow(page, "New plan").click({ button: "right" });
  await menuItem(page, "Rename").click();
  await page.getByRole("textbox", { name: "Rename New plan" }).fill("Final plan");
  await page.getByRole("textbox", { name: "Rename New plan" }).press("Enter");
  await expect(treeRow(page, "Final plan")).toBeVisible();
  await expect(noteTitle(page)).toHaveValue("Final plan");

  // "Move to top level" takes it out of the folder.
  await treeRow(page, "Final plan").click({ button: "right" });
  await menuItem(page, "Move to top level").click();
  await expect
    .poll(async () => find(await navigation(page, testInfo), "Final plan")?.folder_id)
    .toBeNull();
  await expect(page.locator(`[data-folder-id="${archive}"] [data-document-id]`)).toHaveCount(0);

  await page.reload();
  await expect(noteTitle(page)).toHaveValue("Final plan");
  await expect(treeRow(page, "Final plan")).toBeVisible();
  await expect(page.locator(`[data-folder-id="${archive}"] [data-document-id]`)).toHaveCount(0);
});

test("dragging moves notes and folders into folders and back to the top level", async ({
  page,
}, testInfo) => {
  const projects = await createFolder(page, testInfo, "Projects");
  await createFolder(page, testInfo, "Someday");
  await createNote(page, testInfo, { title: "Loose idea", body: "Hmm." });
  await createNote(page, testInfo, { title: "Filed idea", body: "Yes.", folderId: projects });
  await openApp(page);

  // A top-level note into a folder.
  await drag(page, treeRow(page, "Loose idea"), folder(page, "Projects"));
  await expect(page.locator(`[data-folder-id="${projects}"]`)).toContainText("Loose idea");
  await expect
    .poll(async () => find(await navigation(page, testInfo), "Loose idea")?.folder_id)
    .toBe(projects);

  // A folder into another folder carries its notes along.
  await drag(page, folder(page, "Projects"), folder(page, "Someday"));
  await expect
    .poll(async () => find(await navigation(page, testInfo), "Projects")?.parent_id)
    .not.toBeNull();
  const someday = page.locator("[data-folder-id]").filter({ has: folder(page, "Someday") });
  await expect(someday).toContainText("Loose idea");
  await expect(someday).toContainText("Filed idea");

  // A note from the nested folder straight to the top level.
  await drag(page, treeRow(page, "Filed idea"), page.getByRole("button", { name: /^All notes/ }));
  await expect
    .poll(async () => find(await navigation(page, testInfo), "Filed idea")?.folder_id)
    .toBeNull();

  await page.reload();
  const tree = await navigation(page, testInfo);
  expect(find(tree, "Someday")?.children?.map((node) => node.name ?? node.title)).toEqual([
    "Projects",
  ]);
  expect(find(tree, "Projects")?.children?.map((node) => node.title)).toEqual(["Loose idea"]);
  await expect(someday).toContainText("Loose idea");
  await expect(someday).not.toContainText("Filed idea");
  await expect(treeRow(page, "Filed idea")).toBeVisible();
});

test("the sidebar works from the keyboard alone", async ({ page }, testInfo) => {
  await createFolder(page, testInfo, "Reading");
  await openApp(page);

  // Shift+F10 on a folder opens its menu; arrow keys move through it.
  await folder(page, "Reading").focus();
  await page.keyboard.press("Shift+F10");
  const menu = page.getByRole("menu", { name: "Document actions" });
  await expect(menuItem(page, "New folder")).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(menuItem(page, "New note")).toBeFocused();
  await page.keyboard.press("ArrowUp");
  await page.keyboard.press("ArrowUp");
  await expect(menuItem(page, "Delete folder")).toBeFocused();
  // Escape closes the menu and gives focus back to the folder.
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(folder(page, "Reading")).toBeFocused();

  // A note in the folder, made and titled without the mouse.
  await page.keyboard.press("Shift+F10");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(page.locator('[aria-label="Document content"]')).toBeFocused();
  await page.keyboard.type("Books to borrow");
  await expect(treeRow(page, "Books to borrow")).toBeVisible();

  // Rename the folder with the ContextMenu key.
  await folder(page, "Reading").focus();
  await page.keyboard.press("ContextMenu");
  await expect(menuItem(page, "New folder")).toBeFocused();
  for (let step = 0; step < 2; step += 1) await page.keyboard.press("ArrowDown");
  await expect(menuItem(page, "Rename")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("textbox", { name: "Rename Reading" })).toBeFocused();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.type("Library");
  await page.keyboard.press("Enter");
  await expect(folder(page, "Library")).toBeVisible();

  // Enter on a note opens it.
  await treeRow(page, "Books to borrow").focus();
  await page.keyboard.press("Enter");
  await expect(noteTitle(page)).toHaveValue("Books to borrow");

  await page.reload();
  await expect(folder(page, "Library")).toBeVisible();
  await expect(
    page.locator("[data-folder-id]").filter({ has: folder(page, "Library") }),
  ).toContainText("Books to borrow");
});

test("quick capture files thoughts in Uncategorized, newest first, and they can be filed", async ({
  page,
}, testInfo) => {
  await createFolder(page, testInfo, "Errands");
  await openApp(page);
  const capture = page.getByRole("textbox", { name: "Quick capture" });
  await page.keyboard.press("Control+Shift+Space");
  await expect(capture).toBeFocused();
  for (const text of ["Buy stamps", "Return library books"]) {
    await capture.fill(text);
    await capture.press("Enter");
    await expect(capture).toHaveValue("");
  }
  const uncategorized = page
    .locator("[data-folder-id]")
    .filter({ has: folder(page, "Uncategorized") });
  await expect(uncategorized.locator("[data-document-id]")).toHaveText([
    /Return library books/,
    /Buy stamps/,
  ]);
  // The Recent list shows them too.
  await expect(
    page.getByRole("region", { name: "Recent documents" }).getByRole("button", {
      name: "Buy stamps",
    }),
  ).toBeVisible();

  // File one by dragging it; the capture folder keeps the rest.
  await drag(page, treeRow(page, "Buy stamps"), folder(page, "Errands"));
  const errands = page.locator("[data-folder-id]").filter({ has: folder(page, "Errands") });
  await expect(errands).toContainText("Buy stamps");
  await expect(uncategorized.locator("[data-document-id]")).toHaveText([/Return library books/]);

  // A later capture reuses the same Uncategorized folder.
  await capture.fill("Call the plumber");
  await capture.press("Enter");
  await expect(uncategorized.locator("[data-document-id]")).toHaveText([
    /Call the plumber/,
    /Return library books/,
  ]);
  await page.reload();
  await expect(folder(page, "Uncategorized")).toHaveCount(1);
  await expect(errands).toContainText("Buy stamps");
});
