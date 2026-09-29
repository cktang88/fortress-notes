import { expect, test, type Page } from "@playwright/test";

const embeddedImage =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/nXcAAAAASUVORK5CYII=";
const shortcut = process.platform === "darwin" ? "Meta" : "Control";
const editorSelector = '[contenteditable="true"][aria-label="Document content"]';

async function createBlankNote(page: Page) {
  await page.goto("/");
  await page.reload();
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  const editor = page.locator(editorSelector);
  await expect(editor).toBeVisible();
  return editor;
}

async function readClipboardHtml(page: Page) {
  return page.evaluate(async () => {
    const [item] = await navigator.clipboard.read();
    if (!item?.types.includes("text/html")) return "";
    return (await item.getType("text/html")).text();
  });
}

test.beforeEach(async ({ page }, testInfo) => {
  const backendUrl = String(testInfo.config.metadata.backendUrl);
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    return route.continue({ url: `${backendUrl}${url.pathname}${url.search}` });
  });
  await page.route("**/media/**", async (route) => {
    const url = new URL(route.request().url());
    const response = await route.fetch({ url: `${backendUrl}${url.pathname}${url.search}` });
    return route.fulfill({ response });
  });
});

test("edits a blank note with bold and italic shortcuts and copies rich HTML", async ({ page }) => {
  const browserErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));
  const editor = await createBlankNote(page);
  await expect(editor).toHaveAttribute("aria-label", "Document content");

  await editor.click();
  await page.keyboard.type("A blank note becomes useful.");
  await page.keyboard.press("Shift+Home");
  await expect
    .poll(() => page.evaluate(() => window.getSelection()?.toString()))
    .toBe("A blank note becomes useful.");
  await expect(page.getByRole("group", { name: "Selected block actions" })).toHaveCount(0);

  await page.keyboard.press(`${shortcut}+b`);
  await expect(editor.locator("strong")).toContainText("A blank note becomes useful.");
  const formattingGroup = page.getByRole("group", { name: "Text formatting" });
  await expect(formattingGroup).toBeVisible();
  await expect(formattingGroup.getByRole("button", { name: /bold/i })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await page.keyboard.press(`${shortcut}+i`);
  await expect(editor.locator("em")).toContainText("A blank note becomes useful.");
  expect(browserErrors).toEqual([]);

  await editor.click();
  await page.keyboard.press(`${shortcut}+a`);
  await page.keyboard.press(`${shortcut}+c`);
  const copiedHtml = await readClipboardHtml(page);
  expect(copiedHtml).toContain("A blank note becomes useful.");
  expect(copiedHtml).toMatch(/<(strong|b)(\s|>)/);
  expect(copiedHtml).toMatch(/<(em|i)(\s|>)/);
});

test("pastes rich HTML with a data URL image and restores it through reload, undo, and redo", async ({
  page,
}) => {
  const editor = await createBlankNote(page);
  await editor.click();
  await page.evaluate(async (imageUrl) => {
    await navigator.clipboard.write([
      new ClipboardItem({
        "text/html": new Blob(
          [`<p><strong>Rich clipboard text</strong></p><img src="${imageUrl}" alt="tiny test image">`],
          { type: "text/html" },
        ),
        "text/plain": new Blob(["Rich clipboard text"], { type: "text/plain" }),
      }),
    ]);
  }, embeddedImage);
  await page.keyboard.press(`${shortcut}+v`);

  await expect(editor.locator("strong")).toContainText("Rich clipboard text");
  await expect(editor.locator(`img[src="${embeddedImage}"]`)).toHaveCount(1);
  await editor.click();
  await page.keyboard.press(`${shortcut}+a`);
  await page.keyboard.press(`${shortcut}+c`);
  const copiedHtml = await readClipboardHtml(page);
  expect(copiedHtml).toContain("Rich clipboard text");
  expect(copiedHtml).toContain(embeddedImage);

  await expect(page.getByRole("button", { name: "Undo saved change" })).toBeEnabled();
  await page.reload();
  const restoredEditor = page.locator(editorSelector);
  await expect(restoredEditor.locator("strong")).toContainText("Rich clipboard text");
  await expect(restoredEditor.locator(`img[src="${embeddedImage}"]`)).toHaveCount(1);

  await page.getByRole("button", { name: "Undo saved change" }).click();
  await expect(restoredEditor).not.toContainText("Rich clipboard text");
  await page.getByRole("button", { name: "Redo saved change" }).click();
  await expect(restoredEditor.locator("strong")).toContainText("Rich clipboard text");
  await expect(restoredEditor.locator(`img[src="${embeddedImage}"]`)).toHaveCount(1);
});

test("copies a loaded local image as embedded clipboard bytes", async ({ page }) => {
  const editor = await createBlankNote(page);
  const imageUrl = await page.evaluate(async (dataUrl) => {
    const imageBytes = Uint8Array.from(atob(dataUrl.split(",")[1]!), (character) =>
      character.charCodeAt(0),
    );
    const form = new FormData();
    form.append("file", new File([imageBytes], "clipboard.png", { type: "image/png" }));
    const response = await fetch("/api/files", { method: "POST", body: form });
    if (!response.ok) throw new Error(`Image upload failed: ${response.status}`);
    const result = (await response.json()) as { props: { url: string } };
    return result.props.url;
  }, embeddedImage);

  await page.evaluate(async (url) => {
    await navigator.clipboard.write([
      new ClipboardItem({
        "text/html": new Blob(
          [`<p><strong>Page with image</strong></p><img src="${url}" alt="clipboard test">`],
          { type: "text/html" },
        ),
        "text/plain": new Blob(["Page with image"], { type: "text/plain" }),
      }),
    ]);
  }, imageUrl);
  await editor.click();
  await page.keyboard.press(`${shortcut}+v`);

  const image = editor.locator("img");
  await expect(image).toHaveCount(1);
  await expect.poll(() => image.evaluate((element) => (element as HTMLImageElement).naturalWidth)).toBe(1);
  const renderedImageUrl = await image.getAttribute("src");
  expect(new URL(renderedImageUrl!, page.url()).origin).toBe(new URL(page.url()).origin);
  expect(new URL(renderedImageUrl!, page.url()).pathname).toContain("/media/");
  await editor.locator("p").click();
  await page.keyboard.press(`${shortcut}+a`);
  await page.keyboard.press(`${shortcut}+c`);
  const copiedHtml = await readClipboardHtml(page);
  expect(copiedHtml).toContain("Page with image");
  expect(copiedHtml).toMatch(/<img[^>]+src="data:image\/png;base64,/i);
  expect(copiedHtml).not.toMatch(/<img[^>]+src="[^"]*\/media\//i);
});
