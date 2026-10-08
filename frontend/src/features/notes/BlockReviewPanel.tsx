import { useState } from "react";
import { blockApi } from "./api";
import type { BlockReviewContextResponse } from "./types";

interface Props {
  documentId: string;
  selectedBlockIds: string[];
}

export function BlockReviewPanel({ documentId, selectedBlockIds }: Props) {
  const [review, setReview] = useState<BlockReviewContextResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function factCheckSelection() {
    setLoading(true);
    setError(null);
    try {
      setReview(await blockApi.reviewContext(documentId, "selected", selectedBlockIds));
    } catch (cause) {
      setReview(null);
      setError(cause instanceof Error ? cause.message : "Block review failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section
      aria-label="Fact-check selected blocks"
      className="mx-8 mt-2 rounded border p-3 text-sm"
    >
      <div className="flex items-center gap-3">
        <strong>Fact-check selection</strong>
        <span className="text-xs text-zinc-500">
          {selectedBlockIds.length} block{selectedBlockIds.length === 1 ? "" : "s"} selected
        </span>
        <button
          type="button"
          className="ml-auto rounded border px-2 py-1 text-xs"
          disabled={loading || selectedBlockIds.length === 0}
          onClick={() => void factCheckSelection()}
        >
          {loading ? "Checking…" : "Fact-check selection"}
        </button>
      </div>
      {error && (
        <p role="alert" className="mt-2 text-red-600">
          {error}
        </p>
      )}
      {review && <ReviewFindings review={review} />}
    </section>
  );
}

/** Review summary plus each finding, with an optional jump to its block. */
export function ReviewFindings({
  review,
  onFocusBlock,
}: {
  review: Pick<BlockReviewContextResponse, "summary" | "items">;
  onFocusBlock?: (blockId: string) => void;
}) {
  return (
    <div className="mt-2 space-y-2">
      <p>{review.summary}</p>
      {review.items.length === 0 && <p className="text-zinc-500">No findings.</p>}
      {review.items.map((item, index) => (
        <article key={`${item.block_id}-${index}`} className="rounded bg-zinc-50 p-2">
          <div className="text-xs text-zinc-500">{item.label || item.severity}</div>
          {item.quote && (
            <blockquote className="my-1 border-l-2 pl-2">
              {onFocusBlock ? (
                <button
                  type="button"
                  title="Show in note"
                  onClick={() => onFocusBlock(item.block_id)}
                  className="text-left hover:underline"
                >
                  {item.quote}
                </button>
              ) : (
                item.quote
              )}
            </blockquote>
          )}
          <p>{item.detail}</p>
        </article>
      ))}
    </div>
  );
}
