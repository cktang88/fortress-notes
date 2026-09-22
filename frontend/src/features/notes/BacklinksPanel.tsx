import { useBacklinks } from "./hooks";

interface Props {
  documentId: string | null;
  onSelect: (documentId: string, blockId: string | null) => void;
}

export function BacklinksPanel({ documentId, onSelect }: Props) {
  const { data, isLoading, isError } = useBacklinks(documentId);

  return (
    <div className="border-t border-zinc-200 bg-zinc-50/60">
      <div className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
        Backlinks
      </div>
      <div className="max-h-44 overflow-y-auto pb-2">
        {!documentId && <Empty>Open a note to see backlinks.</Empty>}
        {documentId && isLoading && <Empty>Finding backlinks…</Empty>}
        {documentId && isError && <Empty>Backlinks unavailable.</Empty>}
        {documentId && !isLoading && !isError && (data?.length ?? 0) === 0 && (
          <Empty>No backlinks yet.</Empty>
        )}
        {data?.map((backlink) => (
          <button
            key={`${backlink.source_block_id}:${backlink.target_block_id ?? "document"}:${backlink.label}`}
            onClick={() => onSelect(backlink.source_document_id, backlink.source_block_id)}
            className="flex w-full flex-col gap-0.5 px-3 py-1.5 text-left hover:bg-white"
          >
            <span className="truncate text-xs font-medium text-zinc-700">
              {backlink.source_document_title}
            </span>
            <span className="line-clamp-2 text-[11px] text-zinc-500">
              {backlink.label || backlink.source_text}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="px-3 py-2 text-xs text-zinc-400">{children}</div>;
}
