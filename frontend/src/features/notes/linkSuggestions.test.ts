import { BlockNoteEditor } from "@blocknote/core";
import { describe, expect, it } from "vite-plus/test";
import {
  blockLink,
  documentLink,
  documentLinkContent,
  documentLinksAsWikiText,
  linkifyDocumentLinks,
  matchingLinkTargets,
  parseBlockReference,
  parseDocumentLink,
} from "./linkSuggestions";
import type { NoteSummary } from "./types";

const notes: NoteSummary[] = [
  { id: "zeta", title: "Zeta", status: "rough", tags: [], updated_at: "", snippet: "" },
  { id: "alpha", title: "Alpha", status: "rough", tags: [], updated_at: "", snippet: "" },
  {
    id: "project-plan",
    title: "Project Plan",
    status: "polished",
    tags: [],
    updated_at: "",
    snippet: "",
  },
];

describe("document link suggestions", () => {
  it("prioritizes title prefixes and limits the menu", () => {
    expect(matchingLinkTargets(notes, "p", 2).map((note) => note.title)).toEqual([
      "Project Plan",
      "Alpha",
    ]);
  });

  it("matches document IDs as well as titles", () => {
    expect(matchingLinkTargets(notes, "project-").map((note) => note.id)).toEqual(["project-plan"]);
  });

  it("serializes a stable ID with a readable label", () => {
    expect(documentLink({ id: "alpha", title: "Alpha | Draft]" })).toBe("[[alpha|Alpha Draft]]");
  });

  it("serializes a stable block ID with a safe label", () => {
    expect(blockLink({ block_id: "block-1", text: 'A "quoted" block' })).toBe(
      '(( block-1 "A quoted block" ))',
    );
  });
});

describe("block reference parsing", () => {
  it("extracts the stable ID and optional display label", () => {
    expect(parseBlockReference('(( block-1 "A quoted block" ))')).toEqual({
      blockId: "block-1",
      label: "A quoted block",
    });
    expect(parseBlockReference("(( block-2 ))")).toEqual({ blockId: "block-2", label: null });
  });

  it("rejects ordinary links", () => {
    expect(parseBlockReference("https://example.com")).toBeNull();
  });
});

describe("note links", () => {
  it("parses the stored [[id|Title]] form", () => {
    expect(parseDocumentLink("[[01NOTE|Project Plan]]")).toEqual({
      documentId: "01NOTE",
      label: "Project Plan",
    });
    expect(parseDocumentLink("[[01NOTE]]")).toEqual({ documentId: "01NOTE", label: null });
    expect(parseDocumentLink("(( block-1 ))")).toBeNull();
    expect(parseDocumentLink("https://example.com")).toBeNull();
  });

  it("inserts a link that reads as the title", () => {
    expect(documentLinkContent({ id: "01NOTE", title: "Plan | v2" })).toEqual({
      type: "link",
      href: "[[01NOTE|Plan v2]]",
      content: "Plan v2",
    });
    expect(documentLinkContent({ id: "01NOTE", title: "" }).content).toBe("Untitled note");
  });

  it("shows links saved as plain text as links, keeping the text around them", () => {
    const bold = { bold: true };
    expect(
      linkifyDocumentLinks([
        { type: "text", text: "See [[01A|Alpha]] and [[01B]].", styles: bold },
        { type: "link", href: "https://example.com", content: [] },
      ]),
    ).toEqual([
      { type: "text", text: "See ", styles: bold },
      {
        type: "link",
        href: "[[01A|Alpha]]",
        content: [{ type: "text", text: "Alpha", styles: bold }],
      },
      { type: "text", text: " and ", styles: bold },
      {
        type: "link",
        href: "[[01B]]",
        content: [{ type: "text", text: "Untitled note", styles: bold }],
      },
      { type: "text", text: ".", styles: bold },
      { type: "link", href: "https://example.com", content: [] },
    ]);
  });

  it("leaves code, other text and non-arrays alone", () => {
    const code = [{ type: "text", text: "[[01A|Alpha]]", styles: { code: true } }];
    expect(linkifyDocumentLinks(code)).toBe(code);
    const plain = [{ type: "text", text: "a [[ b", styles: {} }];
    expect(linkifyDocumentLinks(plain)).toBe(plain);
    expect(linkifyDocumentLinks("[[01A|Alpha]]")).toBe("[[01A|Alpha]]");
  });

  it("exports note links to Markdown as [[id|Title]]", () => {
    const editor = BlockNoteEditor.create();
    for (const title of ["Plan", "Plan (v2) *draft*", "A_b [x", "Back\\slash"]) {
      const link = documentLinkContent({ id: "01NOTE", title });
      const markdown = editor.blocksToMarkdownLossy([
        { type: "paragraph", content: ["See ", link, " now."] },
      ] as never);
      expect(documentLinksAsWikiText(markdown).trim()).toBe(`See ${link.href} now.`);
    }
    expect(documentLinksAsWikiText("[site](https://example.com) and [[01A|Alpha]]")).toBe(
      "[site](https://example.com) and [[01A|Alpha]]",
    );
  });
});
