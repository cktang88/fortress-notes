import { expect, test, type Locator, type Page } from "@playwright/test";
import { proxyToBackend } from "./backend";

const editorSelector = '[contenteditable="true"][aria-label="Document content"]';

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

async function newTitledNote(page: Page, title: string) {
  await page.goto("/");
  await page.getByRole("button", { name: "+ New", exact: true }).click();
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

/** A small PNG drawn on a canvas, as raw bytes (base64). */
async function pngBase64(page: Page, width: number, height: number, colour: string) {
  return page.evaluate(
    async ({ width, height, colour }) => {
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext("2d")!;
      context.fillStyle = colour;
      context.fillRect(0, 0, width, height);
      const blob = await new Promise<Blob>((resolve) =>
        canvas.toBlob((b) => resolve(b!), "image/png"),
      );
      const bytes = new Uint8Array(await blob.arrayBuffer());
      let binary = "";
      for (const byte of bytes) binary += String.fromCharCode(byte);
      return btoa(binary);
    },
    { width, height, colour },
  );
}

async function expectLoadedImage(image: Locator, width: number) {
  await expect(image).toBeVisible();
  await expect
    .poll(() => image.evaluate((element) => (element as HTMLImageElement).naturalWidth))
    .toBe(width);
  const src = (await image.getAttribute("src"))!;
  expect(src).toContain("/media/");
}

test("an image uploaded from the slash menu is stored and shows after a reload", async ({
  page,
}) => {
  const editor = await newTitledNote(page, "Garden photos");
  await page.keyboard.type("Before the rain:");
  await page.keyboard.press("Enter");
  await page.keyboard.type("/image");
  await page
    .getByRole("option", { name: /^Image/ })
    .first()
    .click();

  const bytes = Buffer.from(await pngBase64(page, 6, 4, "#2a7"), "base64");
  const chooser = page.waitForEvent("filechooser");
  await page.getByRole("button", { name: /upload image/i }).click();
  await (await chooser).setFiles({ name: "garden.png", mimeType: "image/png", buffer: bytes });

  await expectLoadedImage(editor.locator("img"), 6);
  await expect(editor).toContainText("Before the rain:");
  await allSaved(page);

  await page.reload();
  await expectLoadedImage(page.locator(editorSelector).locator("img"), 6);
});

test("pasting an image file uploads it, and copying it back embeds the picture", async ({
  page,
}) => {
  const editor = await newTitledNote(page, "Pasted screenshot");
  await page.keyboard.type("Screenshot of the bug:");
  await page.keyboard.press("Enter");
  const base64 = await pngBase64(page, 5, 3, "#c33");
  await page.evaluate(async (data) => {
    const bytes = Uint8Array.from(atob(data), (character) => character.charCodeAt(0));
    await navigator.clipboard.write([
      new ClipboardItem({ "image/png": new Blob([bytes], { type: "image/png" }) }),
    ]);
  }, base64);
  const uploaded = page.waitForResponse(
    (response) => response.url().endsWith("/api/files") && response.ok(),
  );
  await page.keyboard.press("Control+v");
  await uploaded;
  await expectLoadedImage(editor.locator("img"), 5);
  await allSaved(page);

  await page.reload();
  const restored = page.locator(editorSelector);
  await expectLoadedImage(restored.locator("img"), 5);

  await restored.locator("p").first().click();
  await page.keyboard.press("Control+a");
  await page.keyboard.press("Control+a");
  await page.keyboard.press("Control+c");
  const html = await page.evaluate(async () => {
    const [item] = await navigator.clipboard.read();
    return item?.types.includes("text/html") ? (await item.getType("text/html")).text() : "";
  });
  expect(html).toContain("Screenshot of the bug:");
  expect(html).toMatch(/<img[^>]+src="data:image\/png;base64,/i);
});

test("a failed upload says so and leaves no empty image behind", async ({ page }) => {
  const editor = await newTitledNote(page, "Upload failure");
  await page.keyboard.type("Text stays");
  await page.keyboard.press("Enter");
  await page.route("**/api/files", (route) =>
    route.fulfill({ status: 500, body: "Internal Server Error" }),
  );
  const base64 = await pngBase64(page, 3, 3, "#33c");
  await page.evaluate(async (data) => {
    const bytes = Uint8Array.from(atob(data), (character) => character.charCodeAt(0));
    await navigator.clipboard.write([
      new ClipboardItem({ "image/png": new Blob([bytes], { type: "image/png" }) }),
    ]);
  }, base64);
  await page.keyboard.press("Control+v");
  await expect(page.getByRole("alert").filter({ hasText: /Could not upload/ })).toBeVisible();
  await expect(editor.locator('[data-content-type="image"]')).toHaveCount(0);
  await expect(editor).toContainText("Text stays");
  await allSaved(page);
  await page.reload();
  await expect(page.locator(editorSelector).locator('[data-content-type="image"]')).toHaveCount(0);
});
