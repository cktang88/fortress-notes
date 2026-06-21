import { describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { extractImageFiles, uploadAndInsert } from "./paste";
import { ImageResize } from "./ImageResize";

function pngFile(name = "a.png"): File {
  return new File([new Uint8Array([137, 80, 78, 71])], name, { type: "image/png" });
}

// Minimal DataTransfer-like stub (jsdom's is incomplete for our needs).
function dt(opts: { items?: { kind: string; type: string; file: File | null }[]; files?: File[] }) {
  return {
    items: (opts.items ?? []).map((i) => ({
      kind: i.kind,
      type: i.type,
      getAsFile: () => i.file,
    })),
    files: opts.files ?? [],
  } as unknown as DataTransfer;
}

describe("extractImageFiles", () => {
  it("returns [] for null", () => {
    expect(extractImageFiles(null)).toEqual([]);
  });

  it("extracts image file from clipboard items", () => {
    const f = pngFile();
    const out = extractImageFiles(dt({ items: [{ kind: "file", type: "image/png", file: f }] }));
    expect(out).toEqual([f]);
  });

  it("ignores non-image and non-file items", () => {
    const out = extractImageFiles(
      dt({
        items: [
          { kind: "string", type: "text/plain", file: null },
          { kind: "file", type: "application/pdf", file: pngFile("x.pdf") },
        ],
      }),
    );
    expect(out).toEqual([]);
  });

  it("falls back to dataTransfer.files", () => {
    const f = pngFile();
    expect(extractImageFiles(dt({ files: [f] }))).toEqual([f]);
  });

  it("de-dupes when the same file appears in items and files", () => {
    const f = pngFile();
    const out = extractImageFiles(
      dt({ items: [{ kind: "file", type: "image/png", file: f }], files: [f] }),
    );
    expect(out).toEqual([f]);
  });
});

describe("uploadAndInsert", () => {
  function makeEditor() {
    return new Editor({ extensions: [StarterKit, ImageResize] });
  }

  it("uploads each file and inserts an <img> with the returned url", async () => {
    const editor = makeEditor();
    const upload = vi.fn(async (_f: File) => ({ url: "/media/abc.png", text: "a cat\nmore" }));

    await uploadAndInsert(editor, [pngFile()], upload);

    expect(upload).toHaveBeenCalledOnce();
    const html = editor.getHTML();
    expect(html).toContain('src="/media/abc.png"');
    expect(html).toContain('alt="a cat"');
    editor.destroy();
  });

  it("skips a file whose upload throws, without breaking others", async () => {
    const editor = makeEditor();
    const upload = vi
      .fn()
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValueOnce({ url: "/media/ok.png", text: "" });

    await uploadAndInsert(editor, [pngFile("bad.png"), pngFile("good.png")], upload);

    const html = editor.getHTML();
    expect(html).not.toContain("bad");
    expect(html).toContain('src="/media/ok.png"');
    editor.destroy();
  });

  it("is a no-op with a null editor", async () => {
    const upload = vi.fn();
    await expect(uploadAndInsert(null, [pngFile()], upload)).resolves.toBeUndefined();
    expect(upload).not.toHaveBeenCalled();
  });
});
