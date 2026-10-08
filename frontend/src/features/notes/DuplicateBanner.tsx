import { useState } from "react";
import { useDebouncedValue, useSimilarDocuments } from "./hooks";
import type { SimilarDocument } from "./types";

interface Props {
  documentId: string;
  /** Changes with every save, so the check reruns as the note grows. */
  revision: number;
  onOpen: (documentId: string) => void;
  onMoveInto: (target: SimilarDocument) => void;
  moving: boolean;
}

const DISMISSED_KEY = "fortress-notes:not-duplicates";

/** "This looks a lot like …": offers to fold a repeated note into the existing one. */
export function DuplicateBanner({ documentId, revision, onOpen, onMoveInto, moving }: Props) {
  // Check once writing pauses, not on every save.
  const settledRevision = useDebouncedValue(revision, 1500);
  const similar = useSimilarDocuments(documentId, settledRevision);
  const [dismissed, setDismissed] = useState(readDismissed);
  const [confirming, setConfirming] = useState(false);
  const match = (Array.isArray(similar.data) ? similar.data : []).find(
    (item) => !dismissed.has(pairKey(documentId, item.document_id)),
  );
  if (!match) return null;

  const percent = Math.round(match.overlap * 100);
  return (
    <div
      role="region"
      aria-label="Possible duplicate"
      className="mx-8 mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900"
    >
      {confirming ? (
        <>
          <span className="flex-1">
            Move everything in this note to the end of “{match.title}” and put this note in Trash?
          </span>
          <button
            type="button"
            disabled={moving}
            onClick={() => onMoveInto(match)}
            className="rounded-md bg-amber-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-amber-500 disabled:opacity-50"
          >
            {moving ? "Moving…" : "Move"}
          </button>
          <button
            type="button"
            onClick={() => setConfirming(false)}
            className="rounded-md px-2 py-1 text-xs hover:bg-amber-100"
          >
            Cancel
          </button>
        </>
      ) : (
        <>
          <span className="flex-1">
            This looks a lot like <strong>“{match.title}”</strong> — {percent}% of this note's words
            are already there.
          </span>
          <button
            type="button"
            onClick={() => onOpen(match.document_id)}
            className="rounded-md px-2 py-1 text-xs hover:bg-amber-100"
          >
            Open
          </button>
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="rounded-md border border-amber-300 bg-white px-2.5 py-1 text-xs font-medium hover:bg-amber-100"
          >
            Move this into “{match.title}”…
          </button>
          <button
            type="button"
            onClick={() => {
              const next = new Set(dismissed).add(pairKey(documentId, match.document_id));
              setDismissed(next);
              writeDismissed(next);
            }}
            className="rounded-md px-2 py-1 text-xs text-amber-700 hover:bg-amber-100"
          >
            Not a duplicate
          </button>
        </>
      )}
    </div>
  );
}

function pairKey(a: string, b: string) {
  return [a, b].sort().join("|");
}

function readDismissed(): Set<string> {
  try {
    const raw = window.localStorage.getItem(DISMISSED_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(parsed) ? parsed.filter((v) => typeof v === "string") : []);
  } catch {
    return new Set();
  }
}

function writeDismissed(pairs: Set<string>) {
  try {
    window.localStorage.setItem(DISMISSED_KEY, JSON.stringify([...pairs]));
  } catch {
    // Without storage the dismissal lasts for this visit.
  }
}
