import type { Editor } from "@tiptap/core";

/** Pull image File objects out of a paste/drop DataTransfer (items + files, de-duped). */
export function extractImageFiles(dt: DataTransfer | null): File[] {
  if (!dt) return [];
  const fromItems = Array.from(dt.items ?? [])
    .filter((it) => it.kind === "file" && it.type.startsWith("image/"))
    .map((it) => it.getAsFile());
  const fromFiles = Array.from(dt.files ?? []).filter((f) => f.type.startsWith("image/"));
  const all = [...fromItems, ...fromFiles].filter((f): f is File => f != null);
  return all.filter((f, i) => all.findIndex((g) => g === f) === i);
}

export type ImageUploader = (file: File) => Promise<{ url: string; text: string }>;

/** Upload each file and insert it into the editor as an image node. */
export async function uploadAndInsert(
  editor: Editor | null,
  files: File[],
  upload: ImageUploader,
): Promise<void> {
  if (!editor) return;
  for (const file of files) {
    try {
      const { url, text } = await upload(file);
      const alt = (text.split("\n")[0] || "image").slice(0, 80);
      editor.chain().focus().setImage({ src: url, alt }).run();
    } catch {
      // upload failed — skip this image
    }
  }
}
