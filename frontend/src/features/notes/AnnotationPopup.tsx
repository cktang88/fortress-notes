import type { Annotation } from "./annotations";

interface Props {
  annotation: Annotation;
  x: number;
  y: number;
  onAccept: () => void;
  onReject: () => void;
}

const TITLES: Record<Annotation["type"], string> = {
  review: "AI review",
  consistency: "Inconsistency",
  stale: "Possibly outdated",
};

export function AnnotationPopup({ annotation, x, y, onAccept, onReject }: Props) {
  return (
    <div
      className="fixed z-50 w-72 rounded-lg border border-zinc-200 bg-white p-3 shadow-xl"
      style={{ left: Math.min(x, window.innerWidth - 300), top: y + 12 }}
    >
      <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-red-500">
        {TITLES[annotation.type]}
      </div>
      <p className="mb-2 text-sm text-zinc-700">{annotation.message}</p>
      {annotation.suggestion && (
        <p className="mb-2 rounded bg-emerald-50 p-2 text-xs text-emerald-800">
          <span className="font-medium">Suggested:</span> {annotation.suggestion}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <button
          onClick={onReject}
          className="rounded-md px-2.5 py-1 text-xs text-zinc-500 hover:bg-zinc-100"
        >
          Reject
        </button>
        <button
          onClick={onAccept}
          className="rounded-md bg-zinc-900 px-2.5 py-1 text-xs font-medium text-white hover:bg-zinc-700"
        >
          {annotation.suggestion ? "Accept" : "Dismiss"}
        </button>
      </div>
    </div>
  );
}
