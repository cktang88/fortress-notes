import type { BlockDocument, BlockNode } from "./types";

interface Props {
  document: BlockDocument | null;
  onFocus: (blockId: string) => void;
}

type Heading = { id: string; depth: number; text: string };

export function OutlinePanel({ document, onFocus }: Props) {
  const headings = document ? collectHeadings(document.children) : [];

  return (
    <div className="border-t border-zinc-200 bg-zinc-50/60">
      <div className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
        Outline
      </div>
      <div className="max-h-48 overflow-y-auto pb-2">
        {!document && <Empty>Open a note to see its outline.</Empty>}
        {document && headings.length === 0 && <Empty>No headings yet.</Empty>}
        {headings.map((heading) => (
          <button
            key={heading.id}
            onClick={() => onFocus(heading.id)}
            className="block w-full truncate py-1 text-left text-xs text-zinc-600 hover:bg-white hover:text-zinc-900"
            style={{ paddingLeft: `${12 + heading.depth * 12}px`, paddingRight: 12 }}
          >
            {heading.text || "Untitled heading"}
          </button>
        ))}
      </div>
    </div>
  );
}

function collectHeadings(nodes: readonly BlockNode[], depth = 0): Heading[] {
  return nodes.flatMap((node) => {
    const current =
      node.type === "heading" ? [{ id: node.id, depth, text: headingText(node) }] : [];
    return [...current, ...collectHeadings(node.children, depth + 1)];
  });
}

function headingText(node: BlockNode): string {
  const text = node.text || String(node.content.markdown ?? "");
  return text.replace(/^\s*#{1,6}\s+/, "").trim();
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="px-3 py-2 text-xs text-zinc-400">{children}</div>;
}
