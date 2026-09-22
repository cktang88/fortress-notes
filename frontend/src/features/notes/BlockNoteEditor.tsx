import { useEffect, useMemo, useRef, useState } from "react";
import type {
  Block,
  BlocksChanged,
  PartialBlock,
  BlockNoteEditor as BlockNoteEditorInstance,
} from "@blocknote/core";
import { SuggestionMenu } from "@blocknote/core/extensions";
import { SuggestionMenuController, useCreateBlockNote } from "@blocknote/react";
import type { DefaultReactSuggestionItem } from "@blocknote/react";
import { BlockNoteView } from "@blocknote/mantine";
import "@blocknote/core/fonts/inter.css";
import "@blocknote/mantine/style.css";
import { blockApi } from "./api";
import { blockLink, documentLink, matchingLinkTargets } from "./linkSuggestions";
import type {
  BlockDocument,
  BlockLinkTarget,
  BlockNode,
  BlockOperation,
  NoteSummary,
} from "./types";

interface Props {
  document: BlockDocument;
  focusBlockId?: string | null;
  linkTargets: readonly NoteSummary[];
}

type Location = {
  parentId: string | null;
  position: number;
  depth: number;
};

export function BlockNoteEditor({ document: initialDocument, focusBlockId, linkTargets }: Props) {
  const initialContent = useMemo(
    () => initialDocument.children.map(toPartialBlock),
    [initialDocument.children],
  );
  const editor = useCreateBlockNote(
    { initialContent: initialContent.length > 0 ? initialContent : undefined },
    [initialDocument.id],
  );
  const saveQueue = useRef(Promise.resolve());
  const hydrating = useRef(true);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const onChange = (
    changedEditor: BlockNoteEditorInstance,
    context: { getChanges: () => BlocksChanged },
  ) => {
    if (hydrating.current) return;
    const operations = changesToOperations(changedEditor, context.getChanges());
    if (operations.length === 0) return;

    saveQueue.current = saveQueue.current
      .catch(() => undefined)
      .then(async () => {
        setSaving(true);
        try {
          await blockApi.transaction(initialDocument.id, operations);
          setSaveError(null);
        } catch (error) {
          setSaveError(error instanceof Error ? error.message : "Could not save changes");
        } finally {
          setSaving(false);
        }
      });
  };

  useEffect(() => {
    editor.transact(() => {
      for (const node of flattenNodes(initialDocument.children)) {
        if (node.content.blocknote !== undefined) continue;
        const block = editor.getBlock(node.id);
        if (!block || block.type === "divider") continue;
        const parsed = editor.tryParseMarkdownToBlocks(markdownText(node));
        if (parsed[0]?.content !== undefined) {
          editor.updateBlock(block.id, { content: parsed[0].content } as PartialBlock);
        }
      }
    });
    hydrating.current = false;
  }, [editor, initialDocument]);

  useEffect(() => {
    if (!focusBlockId || !editor.getBlock(focusBlockId)) return;
    const frame = requestAnimationFrame(() => {
      editor.setTextCursorPosition(focusBlockId, "start");
      editor.focus();
      const target = Array.from(
        editor.domElement?.querySelectorAll<HTMLElement>("[data-id]") ?? [],
      ).find((element) => element.dataset.id === focusBlockId);
      target?.scrollIntoView({ block: "center" });
    });
    return () => cancelAnimationFrame(frame);
  }, [editor, focusBlockId]);

  const getDocumentLinkItems = async (query: string): Promise<DefaultReactSuggestionItem[]> =>
    matchingLinkTargets(linkTargets, query).map((target) => ({
      key: "paragraph",
      title: target.title,
      subtext: `Document · ${target.id}`,
      group: "Link to document",
      onItemClick: () => {
        editor.insertInlineContent(documentLink(target));
        editor.getExtension(SuggestionMenu)?.closeMenu();
      },
    }));

  const getBlockLinkItems = async (query: string): Promise<DefaultReactSuggestionItem[]> => {
    const targets: BlockLinkTarget[] = await blockApi.linkTargets(query);
    return targets.map((target) => ({
      key: "paragraph",
      title: target.text || `${target.block_type} block`,
      subtext: `${target.document_title} · ${target.block_id}`,
      group: "Link to block",
      onItemClick: () => {
        editor.insertInlineContent(blockLink(target));
        editor.getExtension(SuggestionMenu)?.closeMenu();
      },
    }));
  };

  return (
    <section className="blocknote-shell flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 px-8 pt-4 text-xs text-zinc-400">
        <span className="font-medium uppercase tracking-wide text-zinc-500">
          {editor.document.length} blocks
        </span>
        {saving && <span>Saving…</span>}
        {saveError && <span className="text-red-600">{saveError}</span>}
      </div>
      <BlockNoteView
        editor={editor}
        onChange={onChange}
        className="min-h-0 flex-1 overflow-y-auto px-8 pb-12 pt-2"
      >
        <SuggestionMenuController
          triggerCharacter="[["
          getItems={getDocumentLinkItems}
          minQueryLength={0}
        />
        <SuggestionMenuController
          triggerCharacter="(("
          getItems={getBlockLinkItems}
          minQueryLength={1}
        />
      </BlockNoteView>
    </section>
  );
}

function toPartialBlock(node: BlockNode): PartialBlock {
  const storedContent = node.content.blocknote;
  const content = storedContent === undefined ? markdownText(node) : storedContent;
  const children = node.children.map(toPartialBlock);

  if (node.type === "heading") {
    const match = markdownText(node).match(/^\s*(#{1,6})\s+/);
    return {
      id: node.id,
      type: "heading",
      props: { level: Number(node.attrs.level ?? match?.[1].length ?? 1) },
      content: match ? markdownText(node).slice(match[0].length) : content,
      children,
    } as PartialBlock;
  }
  if (node.type === "list") {
    const checked = node.attrs.checked;
    const type =
      typeof checked === "boolean"
        ? "checkListItem"
        : node.attrs.ordered === true
          ? "numberedListItem"
          : "bulletListItem";
    return {
      id: node.id,
      type,
      props: typeof checked === "boolean" ? { checked } : undefined,
      content: markdownText(node)
        .split("\n")
        .map((line) => line.replace(/^\s*(?:[-+*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?/, ""))
        .join("\n"),
      children,
    } as PartialBlock;
  }
  if (node.type === "quote") {
    return {
      id: node.id,
      type: "quote",
      content: markdownText(node)
        .split("\n")
        .map((line) => line.replace(/^\s*>\s?/, ""))
        .join("\n"),
      children,
    } as PartialBlock;
  }
  if (node.type === "code") {
    return {
      id: node.id,
      type: "codeBlock",
      content: markdownText(node)
        .replace(/^\s*(```|~~~)[^\n]*\n?/, "")
        .replace(/\n\s*(```|~~~)\s*$/, ""),
      children,
    } as PartialBlock;
  }
  if (node.type === "thematic_break") {
    return { id: node.id, type: "divider", children } as PartialBlock;
  }
  return { id: node.id, type: "paragraph", content, children } as PartialBlock;
}

function markdownText(node: BlockNode): string {
  return String(node.content.markdown ?? node.text ?? "");
}

function flattenNodes(nodes: readonly BlockNode[]): BlockNode[] {
  return nodes.flatMap((node) => [node, ...flattenNodes(node.children)]);
}

function changesToOperations(
  editor: BlockNoteEditorInstance,
  changes: BlocksChanged,
): BlockOperation[] {
  const locations = new Map<string, Location>();
  indexLocations(editor.document, locations);
  const operations: BlockOperation[] = [];

  for (const change of changes) {
    if (change.type === "delete") {
      if (!hasDeletedAncestor(change.block.id, changes)) {
        operations.push({ operation: "delete", block_id: change.block.id });
      }
      continue;
    }

    const location = locations.get(change.block.id);
    if (!location) continue;

    if (change.type === "insert") {
      operations.push(toOperation(editor, change.block, "insert", location));
    } else if (change.type === "move") {
      operations.push({
        operation: "move",
        block_id: change.block.id,
        parent_id: location.parentId,
        position: location.position,
      });
      operations.push(toOperation(editor, change.block, "update", location));
    } else {
      operations.push(toOperation(editor, change.block, "update", location));
    }
  }

  return operations;
}

function toOperation(
  editor: BlockNoteEditorInstance,
  block: Block,
  operation: "insert" | "update",
  location: Location,
): BlockOperation {
  return {
    operation,
    block_id: block.id,
    parent_id: operation === "insert" ? location.parentId : undefined,
    position: operation === "insert" ? location.position : undefined,
    type: block.type,
    attrs: block.props,
    content: {
      blocknote: block.content,
      markdown: editor.blocksToMarkdownLossy([block]),
    },
    text: inlineText(block.content),
  };
}

function indexLocations(blocks: readonly Block[], output: Map<string, Location>): void {
  const visit = (children: readonly Block[], parentId: string | null, depth: number) => {
    children.forEach((block, position) => {
      output.set(block.id, { parentId, position, depth });
      visit(block.children, block.id, depth + 1);
    });
  };
  visit(blocks, null, 0);
}

function inlineText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((item) => {
      if (typeof item === "string") return item;
      if (!item || typeof item !== "object") return "";
      const value = item as { text?: unknown; content?: unknown };
      if (typeof value.text === "string") return value.text;
      return inlineText(value.content);
    })
    .join("");
}

function hasDeletedAncestor(id: string, changes: BlocksChanged): boolean {
  const block = changes.find((change) => change.block.id === id)?.block;
  if (!block) return false;
  return changes.some(
    (change) =>
      change.type === "delete" && change.block.id !== id && containsDescendant(change.block, id),
  );
}

function containsDescendant(block: Block, id: string): boolean {
  return block.children.some((child) => child.id === id || containsDescendant(child, id));
}
