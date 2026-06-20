import type { NoteStatus } from "./types";

export function StatusBadge({ status }: { status: NoteStatus }) {
  const polished = status === "polished";
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
        polished ? "bg-emerald-100 text-emerald-700" : "bg-amber-100 text-amber-700"
      }`}
    >
      {status}
    </span>
  );
}
