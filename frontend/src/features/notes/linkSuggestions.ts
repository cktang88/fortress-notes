import type { BlockLinkTarget, NoteSummary } from "./types";

const MAX_LINK_SUGGESTIONS = 8;

export function matchingLinkTargets(
  targets: readonly NoteSummary[],
  query: string,
  limit = MAX_LINK_SUGGESTIONS,
): NoteSummary[] {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  return targets
    .filter((target) => {
      if (!normalizedQuery) return true;
      return (
        target.title.toLocaleLowerCase().includes(normalizedQuery) ||
        target.id.toLocaleLowerCase().includes(normalizedQuery)
      );
    })
    .sort((left, right) => {
      const leftTitle = left.title.toLocaleLowerCase();
      const rightTitle = right.title.toLocaleLowerCase();
      const leftStarts = leftTitle.startsWith(normalizedQuery);
      const rightStarts = rightTitle.startsWith(normalizedQuery);
      if (leftStarts !== rightStarts) return leftStarts ? -1 : 1;
      return leftTitle.localeCompare(rightTitle);
    })
    .slice(0, limit);
}

export function documentLink(target: Pick<NoteSummary, "id" | "title">): string {
  const label = target.title.replace(/[|\]]/g, " ").replace(/\s+/g, " ").trim();
  return `[[${target.id}${label ? `|${label}` : ""}]]`;
}

export function blockLink(target: Pick<BlockLinkTarget, "block_id" | "text">): string {
  const label = target.text
    .replaceAll('"', " ")
    .replaceAll(")", " ")
    .replaceAll("]", " ")
    .replace(/\s+/g, " ")
    .trim();
  return `(( ${target.block_id}${label ? ` "${label}"` : ""} ))`;
}

export function parseBlockReference(
  value: string,
): { blockId: string; label: string | null } | null {
  const match = value.trim().match(/^\(\(\s*([^\s)]+)(?:\s+"([\s\S]*?)")?\s*\)\)$/);
  if (!match) return null;
  return { blockId: match[1], label: match[2] ?? null };
}

const DOCUMENT_LINK = /\[\[([A-Za-z0-9_-]+)(?:\|([^\]]+))?\]\]/g;
const UNTITLED = "Untitled note";

/** `[[id|Title]]`: the form a link to another note is stored and exported in. */
export function parseDocumentLink(
  value: string,
): { documentId: string; label: string | null } | null {
  const match = value.trim().match(/^\[\[([A-Za-z0-9_-]+)(?:\|([^\]]+))?\]\]$/);
  if (!match) return null;
  return { documentId: match[1], label: match[2]?.trim() || null };
}

/**
 * A link to another note as editor content: it reads as the note's title, and its
 * `[[id|Title]]` href records a backlink (like a block reference's `(( id ))` href).
 */
export function documentLinkContent(target: Pick<NoteSummary, "id" | "title">) {
  const href = documentLink(target);
  return { type: "link" as const, href, content: parseDocumentLink(href)?.label ?? UNTITLED };
}

type InlineText = { type?: unknown; text?: unknown; styles?: Record<string, unknown> };

/**
 * Show `[[id|Title]]` written as plain text (how note links used to be saved, and how
 * imported Markdown spells them) as links, leaving everything else as it is.
 */
export function linkifyDocumentLinks<T>(content: T): T {
  if (!Array.isArray(content)) return content;
  let changed = false;
  const output: unknown[] = [];
  for (const item of content as unknown[]) {
    const value = item as InlineText | null;
    if (
      !value ||
      value.type !== "text" ||
      typeof value.text !== "string" ||
      value.styles?.code ||
      !value.text.includes("[[")
    ) {
      output.push(item);
      continue;
    }
    const text = value.text;
    let last = 0;
    for (const match of text.matchAll(DOCUMENT_LINK)) {
      const index = match.index ?? 0;
      if (index > last) output.push({ ...value, text: text.slice(last, index) });
      output.push({
        type: "link",
        href: match[0],
        content: [{ type: "text", text: match[2]?.trim() || UNTITLED, styles: value.styles ?? {} }],
      });
      last = index + match[0].length;
    }
    if (last === 0) {
      output.push(item);
      continue;
    }
    changed = true;
    if (last < text.length) output.push({ ...value, text: text.slice(last) });
  }
  return (changed ? output : content) as T;
}

/**
 * BlockNote writes a note link as `[Title]([[id|Title]])`; Markdown export (and a later
 * re-import) keeps the plain `[[id|Title]]` form note links have always had.
 */
export function documentLinksAsWikiText(markdown: string): string {
  return markdown.replace(
    /\[(?:[^\]\\]|\\.)*\]\(<?((?:\\?\[){2}[A-Za-z0-9_\\-]+(?:\\?\|(?:[^\]\\]|\\.)+)?(?:\\?\]){2})>?\)/g,
    (_whole, href: string) => href.replace(/\\(.)/g, "$1"),
  );
}
