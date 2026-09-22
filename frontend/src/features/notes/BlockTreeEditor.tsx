import { useState, type FocusEvent, type KeyboardEvent } from "react";
import { blockApi } from "./api";
import { htmlToMarkdown, markdownToHtml } from "./markdown";
import type { BlockDocument, BlockNode, BlockOperation } from "./types";

interface Props {
  document: BlockDocument;
}

export function BlockTreeEditor({ document: initialDocument }: Props) {
  const [document, setDocument] = useState(initialDocument);
  const [saving, setSaving] = useState(false);

  const transact = async (operations: BlockOperation[]) => {
    setSaving(true);
    try {
      setDocument(await blockApi.transaction(document.id, operations));
    } finally {
      setSaving(false);
    }
  };

  const updateText = (block: BlockNode, text: string) => {
    if (text === block.text) return;
    void transact([
      {
        operation: "update",
        block_id: block.id,
        text,
        content: { ...block.content, markdown: text },
        expected_updated_at: block.updated_at,
      },
    ]);
  };

  const insertAfter = (block: BlockNode) => {
    void transact([
      {
        operation: "insert",
        parent_id: block.parent_id,
        position: block.position + 1,
        type: "paragraph",
        text: "",
        content: { markdown: "" },
      },
    ]);
  };

  const move = (block: BlockNode, offset: number) => {
    const nextPosition = block.position + offset;
    if (nextPosition < 0) return;
    void transact([
      {
        operation: "move",
        block_id: block.id,
        parent_id: block.parent_id,
        position: nextPosition,
        expected_updated_at: block.updated_at,
      },
    ]);
  };

  const remove = (block: BlockNode) => {
    if (!window.confirm("Delete this block and its children?")) return;
    void transact([
      {
        operation: "delete",
        block_id: block.id,
        expected_updated_at: block.updated_at,
      },
    ]);
  };

  return (
    <div className="block-editor px-8 py-5">
      <div className="mb-3 flex items-center gap-2 text-xs text-zinc-400">
        <span className="font-medium uppercase tracking-wide text-zinc-500">Blocks</span>
        {saving && <span>Saving…</span>}
        <span className="ml-auto">{document.children.length} top-level blocks</span>
      </div>
      <div className="space-y-1">
        {document.children.map((block) => (
          <BlockView
            key={block.id}
            block={block}
            onText={updateText}
            onInsert={insertAfter}
            onMove={move}
            onDelete={remove}
          />
        ))}
      </div>
      {document.children.length === 0 && (
        <button
          onClick={() => void transact([{ operation: "insert", type: "paragraph", text: "" }])}
          className="rounded-md border border-dashed border-zinc-300 px-3 py-2 text-sm text-zinc-500 hover:bg-zinc-50"
        >
          Add the first block
        </button>
      )}
    </div>
  );
}

function BlockView({
  block,
  onText,
  onInsert,
  onMove,
  onDelete,
}: {
  block: BlockNode;
  onText: (block: BlockNode, text: string) => void;
  onInsert: (block: BlockNode) => void;
  onMove: (block: BlockNode, offset: number) => void;
  onDelete: (block: BlockNode) => void;
}) {
  const [focused, setFocused] = useState(false);
  const className =
    block.type === "heading"
      ? "block-heading leading-8"
      : block.type === "code"
        ? "block-code rounded-md bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100"
        : block.type === "quote"
          ? "block-quote border-l-2 border-zinc-300 pl-3 text-zinc-600"
          : "min-h-7 leading-7";

  return (
    <div
      data-block-id={block.id}
      className={`group relative rounded-md border px-2 py-1 transition-colors ${
        focused ? "border-indigo-200 bg-indigo-50/30" : "border-transparent hover:border-zinc-200"
      }`}
    >
      <div className="absolute -left-8 top-2 flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
        <button
          title="Block menu"
          onClick={() => onInsert(block)}
          className="cursor-grab px-1 text-zinc-400 hover:text-zinc-700"
        >
          ⠿
        </button>
      </div>
      <div className="flex items-start gap-2">
        <EditableBlock
          className={className}
          markdown={block.text}
          onFocus={() => setFocused(true)}
          onBlur={(text) => {
            setFocused(false);
            onText(block, text);
          }}
          onEnter={() => onInsert(block)}
          onEmptyBackspace={() => onDelete(block)}
        />
        <div className="flex shrink-0 items-center gap-1 pt-1 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
          <button
            title="Move up"
            onClick={() => onMove(block, -1)}
            className="text-xs text-zinc-400 hover:text-zinc-700"
          >
            ↑
          </button>
          <button
            title="Move down"
            onClick={() => onMove(block, 1)}
            className="text-xs text-zinc-400 hover:text-zinc-700"
          >
            ↓
          </button>
          <button
            title="Insert below"
            onClick={() => onInsert(block)}
            className="text-xs text-zinc-400 hover:text-zinc-700"
          >
            +
          </button>
          <button
            title="Delete block"
            onClick={() => onDelete(block)}
            className="text-xs text-zinc-400 hover:text-red-600"
          >
            ×
          </button>
        </div>
      </div>
      {block.children.length > 0 && (
        <div className="ml-5 border-l border-zinc-200 pl-3">
          {block.children.map((child) => (
            <BlockView
              key={child.id}
              block={child}
              onText={onText}
              onInsert={onInsert}
              onMove={onMove}
              onDelete={onDelete}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function EditableBlock({
  className,
  markdown,
  onFocus,
  onBlur,
  onEnter,
  onEmptyBackspace,
}: {
  className: string;
  markdown: string;
  onFocus: () => void;
  onBlur: (text: string) => void;
  onEnter: () => void;
  onEmptyBackspace: () => void;
}) {
  const props = {
    contentEditable: true,
    suppressContentEditableWarning: true,
    role: "textbox",
    tabIndex: 0,
    className,
    onFocus,
    onBlur: (event: FocusEvent<HTMLElement>) =>
      onBlur(htmlToMarkdown(event.currentTarget.innerHTML).trim()),
    onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        onEnter();
      } else if (event.key === "Backspace" && !event.currentTarget.textContent) {
        event.preventDefault();
        onEmptyBackspace();
      }
    },
  };
  return <div {...props} dangerouslySetInnerHTML={{ __html: markdownToHtml(markdown) }} />;
}
