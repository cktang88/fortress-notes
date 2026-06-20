import type { ReviewResponse } from "./types";

interface Props {
  review: ReviewResponse | null;
  loading: boolean;
  onClose: () => void;
}

const LABELS: Record<string, string> = {
  factcheck: "Fact-check",
  clarify: "Clarifying questions",
  object: "Objections & inconsistencies",
};

export function ReviewPanel({ review, loading, onClose }: Props) {
  if (!loading && !review) return null;
  return (
    <div className="mx-8 mb-8 rounded-lg border border-zinc-200 bg-zinc-50 p-4">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-zinc-700">
          {review ? LABELS[review.kind] : "Thinking…"}
        </h3>
        <button onClick={onClose} className="text-xs text-zinc-400 hover:text-zinc-600">
          ✕
        </button>
      </div>
      {loading && <p className="text-sm text-zinc-400">Asking the model…</p>}
      {review && (
        <>
          {review.summary && <p className="mb-3 text-sm text-zinc-600">{review.summary}</p>}
          <ul className="flex flex-col gap-2">
            {review.items.map((item, i) => (
              <li key={i} className="text-sm">
                {item.label && (
                  <span className="mr-2 rounded bg-zinc-200 px-1.5 py-0.5 text-[10px] font-medium uppercase text-zinc-600">
                    {item.label}
                  </span>
                )}
                <span className="text-zinc-700">{item.detail}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
