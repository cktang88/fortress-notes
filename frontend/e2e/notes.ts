import { expect, type Page, type TestInfo } from "@playwright/test";

/** The test backend's API root (requests from Node go straight to it). */
export function apiUrl(testInfo: TestInfo) {
  return `${String(testInfo.config.metadata.backendUrl)}/api`;
}

let counter = 0;
/** A word no other note contains, so specs sharing the backend don't see each other. */
export function uniqueWord(prefix = "zq") {
  counter += 1;
  const letters = (Date.now() * 100 + counter)
    .toString(26)
    .replace(/\d/g, (digit) => "qrstuvwxyz"[Number(digit)]);
  return `${prefix}${letters}`;
}

export interface CreatedNote {
  id: string;
  title: string;
  blocks: { id: string; type: string; text: string }[];
}

/** Create a note through the API; `body` is Markdown, one block per paragraph. */
export async function createNote(
  page: Page,
  testInfo: TestInfo,
  note: { title: string; body?: string; folderId?: string | null; status?: string },
): Promise<CreatedNote> {
  const api = apiUrl(testInfo);
  const response = await page.request.post(`${api}/notes`, {
    data: {
      title: note.title,
      body: note.body ?? "",
      folder_id: note.folderId ?? null,
      status: note.status ?? "rough",
    },
  });
  expect(response.ok()).toBe(true);
  const { id } = (await response.json()) as { id: string };
  return { id, title: note.title, blocks: await readBlocks(page, testInfo, id) };
}

export async function readBlocks(page: Page, testInfo: TestInfo, id: string) {
  const tree = (await (
    await page.request.get(`${apiUrl(testInfo)}/block-documents/${id}`)
  ).json()) as {
    children: { id: string; type: string; text: string }[];
  };
  return tree.children.map(({ id: blockId, type, text }) => ({ id: blockId, type, text }));
}

export async function createFolder(
  page: Page,
  testInfo: TestInfo,
  name: string,
  parentId: string | null = null,
): Promise<string> {
  const response = await page.request.post(`${apiUrl(testInfo)}/folders`, {
    data: { name, parent_id: parentId },
  });
  expect(response.ok()).toBe(true);
  return ((await response.json()) as { id: string }).id;
}

/** The id of the editor block holding the text cursor, or null. */
export async function focusedBlockId(page: Page): Promise<string | null> {
  return page.evaluate(() => {
    const node = window.getSelection()?.anchorNode ?? null;
    const element = node instanceof Element ? node : (node?.parentElement ?? null);
    const editor = element?.closest('[aria-label="Document content"]');
    if (!editor || !document.activeElement?.closest('[aria-label="Document content"]')) return null;
    return element?.closest<HTMLElement>("[data-id]")?.dataset.id ?? null;
  });
}

export function noteTitle(page: Page) {
  return page.getByRole("textbox", { name: "Note title" });
}

export function sidebar(page: Page) {
  return page.getByRole("navigation", { name: "Documents" });
}

/** The note's row in the folder tree (not the Recent list). */
export function treeRow(page: Page, title: string) {
  return sidebar(page).locator(`button[data-document-id][aria-label="${title}"]`);
}

/** Open the app and wait until it has rendered (and its keyboard shortcuts are live). */
export async function openApp(page: Page, path = "/") {
  await page.goto(path);
  await expect(page.getByRole("searchbox", { name: "Search notes" })).toBeVisible();
  await expect(sidebar(page)).toBeVisible();
}

/** Poll the server until the note's blocks contain `text` (the editor saves after a pause). */
export async function expectSavedText(page: Page, testInfo: TestInfo, id: string, text: string) {
  await expect
    .poll(async () => (await readBlocks(page, testInfo, id)).map((block) => block.text).join("\n"))
    .toContain(text);
}
