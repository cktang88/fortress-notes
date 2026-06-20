import { useRelated } from "./hooks";

interface Props {
  noteId: string | null;
  onSelect: (id: string) => void;
}

export function RelatedNotes({ noteId, onSelect }: Props) {
  const { data, isLoading } = useRelated(noteId);

  return (
    <div className="border-t border-zinc-200 bg-zinc-50/60">
      <div className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
        Related notes
      </div>
      <div className="max-h-44 overflow-y-auto pb-2">
        {!noteId && <Empty>Open a note to see related ones.</Empty>}
        {noteId && isLoading && <Empty>Finding related…</Empty>}
        {noteId && !isLoading && (data?.length ?? 0) === 0 && <Empty>Nothing related yet.</Empty>}
        {data?.map((r) => (
          <button
            key={r.note.id}
            onClick={() => onSelect(r.note.id)}
            className="flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left hover:bg-white"
          >
            <span className="truncate text-xs text-zinc-700">{r.note.title}</span>
            <span className="shrink-0 text-[10px] text-zinc-400">{r.score.toFixed(2)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="px-3 py-2 text-xs text-zinc-400">{children}</div>;
}
