import { describe, expect, it } from "vitest";
import { htmlToMarkdown, markdownToHtml } from "./markdown";

describe("markdown round-trip", () => {
  it("renders headings and lists to html", () => {
    const html = markdownToHtml("# Title\n\n- a\n- b");
    expect(html).toContain("<h1>Title</h1>");
    expect(html).toContain("<li>a</li>");
  });

  it("converts html back to markdown", () => {
    const md = htmlToMarkdown("<h2>Sub</h2><p>Hello <strong>world</strong></p>");
    expect(md).toContain("## Sub");
    expect(md).toContain("**world**");
  });

  it("preserves core structure across a round-trip", () => {
    const original = "# Coffee\n\nEspresso is **strong**.\n\n- bean\n- water";
    const back = htmlToMarkdown(markdownToHtml(original));
    expect(back).toContain("# Coffee");
    expect(back).toContain("**strong**");
    expect(back).toContain("-   bean");
  });
});
