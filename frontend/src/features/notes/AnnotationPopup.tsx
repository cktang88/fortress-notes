import type { Annotation } from "./annotations";

interface Props {
  annotation: Annotation;
  x: number;
  y: number;
  onDismiss: () => void;
}

const TITLES: Record<Annotation["type"], string> = {
  review: "AI review",
};

const SEVERITY: Record<Annotation["severity"], { label: string; cls: string }> = {
  high: { label: "Incorrect", cls: "bg-red-100 text-red-700" },
  medium: { label: "Misleading", cls: "bg-yellow-100 text-yellow-700" },
  low: { label: "Minor", cls: "bg-zinc-100 text-zinc-500" },
};

export function AnnotationPopup({ annotation, x, y, onDismiss }: Props) {
  return (
    <div
      className="fixed z-50 w-72 rounded-lg border border-zinc-200 bg-white p-3 shadow-xl"
      style={{ left: Math.min(x, window.innerWidth - 300), top: y + 12 }}
    >
      <div className="mb-1 flex items-center gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
          {TITLES[annotation.type]}
        </span>
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${SEVERITY[annotation.severity].cls}`}
        >
          {SEVERITY[annotation.severity].label}
        </span>
      </div>
      <p className="mb-2 text-sm text-zinc-700">{annotation.message}</p>
      <div className="flex justify-end gap-2">
        <button
          onClick={onDismiss}
          className="rounded-md px-2.5 py-1 text-xs text-zinc-500 hover:bg-zinc-100"
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}
