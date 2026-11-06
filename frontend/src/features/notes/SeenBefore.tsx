import { useEffect, useRef, useState } from "react";
import type { RelatedResult, SimilarDocument } from "./types";

interface Props {
  /** Paragraphs from other notes related to the one being written. */
  related: RelatedResult[];
  /** Another note this one largely repeats, if any. */
  duplicate: SimilarDocument | null;
  suggestionsOn: boolean;
  onToggleSuggestions: (on: boolean) => void;
  onLink: (item: RelatedResult) => void;
  onOpen: (documentId: string, blockId: string | null) => void;
  onMoveInto: (target: SimilarDocument) => void;
  onNotDuplicate: (target: SimilarDocument) => void;
  moving: boolean;
}

/**
 * "You wrote something like this before": one quiet chip on the editor's status
 * line. It never opens by itself; click it to see a near-copy of this note (with
 * an offer to fold it in) and related paragraphs from other notes (to link).
 */
export function SeenBefore(props: Props) {
  const { related, duplicate, suggestionsOn } = props;
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const rootRef = useRef<HTMLSpanElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) close();
    };
    const closeEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        close();
        buttonRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeEscape);
    };
  }, [open]);

  function close() {
    setOpen(false);
    setConfirming(false);
  }

  const hasSomething = !!duplicate || related.length > 0;
  if (!hasSomething && suggestionsOn) return null;

  const label = duplicate
    ? `Looks like “${duplicate.title}”`
    : related.length > 0
      ? `↗ ${related.length} related`
      : "↗";
  const tone = duplicate
    ? "border-amber-300 bg-amber-50 text-amber-800 hover:bg-amber-100"
    : "border-zinc-200 text-zinc-500 hover:bg-zinc-100 hover:text-zinc-700";

  return (
    <span ref={rootRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        title={
          hasSomething
            ? "You wrote something like this before"
            : "Related suggestions are off — click to turn them back on"
        }
        onClick={() => (open ? close() : setOpen(true))}
        className={`max-w-56 truncate rounded-full border px-2 py-0.5 ${tone}`}
      >
        {label}
      </button>
      {open && (
        <div
          role="dialog"
          aria-label="Seen before"
          className="absolute right-0 z-40 mt-1 w-80 rounded-lg border border-zinc-200 bg-white p-3 text-left text-sm text-zinc-700 shadow-lg"
        >
          {duplicate && (
            <section className="mb-3 rounded-md bg-amber-50 p-2 text-amber-900">
              {confirming ? (
                <>
                  <p>
                    Move everything in this note to the end of “{duplicate.title}” and put this note
                    in Trash?
                  </p>
                  <div className="mt-2 flex gap-2 text-xs">
                    <button
                      type="button"
                      disabled={props.moving}
                      onClick={() => props.onMoveInto(duplicate)}
                      className="rounded-md bg-amber-600 px-2.5 py-1 font-medium text-white hover:bg-amber-500 disabled:opacity-50"
                    >
                      {props.moving ? "Moving…" : "Move"}
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirming(false)}
                      className="rounded-md px-2 py-1 hover:bg-amber-100"
                    >
                      Cancel
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <p>
                    This note looks a lot like <strong>“{duplicate.title}”</strong> —{" "}
                    {Math.round(duplicate.overlap * 100)}% of its words are already there.
                  </p>
                  <div className="mt-2 flex flex-wrap gap-2 text-xs">
                    <button
                      type="button"
                      onClick={() => {
                        close();
                        props.onOpen(duplicate.document_id, null);
                      }}
                      className="rounded-md px-2 py-1 hover:bg-amber-100"
                    >
                      Open
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirming(true)}
                      className="rounded-md border border-amber-300 bg-white px-2 py-1 font-medium hover:bg-amber-100"
                    >
                      Move this into it…
                    </button>
                    <button
                      type="button"
                      onClick={() => props.onNotDuplicate(duplicate)}
                      className="rounded-md px-2 py-1 text-amber-700 hover:bg-amber-100"
                    >
                      Not a duplicate
                    </button>
                  </div>
                </>
              )}
            </section>
          )}
          {related.length > 0 && (
            <section>
              <h3 className="mb-1 text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
                Related to this paragraph
              </h3>
              <ul className="space-y-2">
                {related.map((item) => (
                  <li key={item.note.id} className="rounded-md bg-zinc-50 p-2">
                    <div className="truncate text-xs font-medium text-zinc-700">
                      {item.note.title}
                    </div>
                    <p className="line-clamp-2 text-xs text-zinc-500">{item.matched_block_text}</p>
                    <div className="mt-1 flex gap-2 text-xs">
                      <button
                        type="button"
                        title="Add a link to this paragraph at the end of the one you're writing"
                        onClick={() => {
                          close();
                          props.onLink(item);
                        }}
                        className="rounded border border-zinc-300 bg-white px-2 py-0.5 text-zinc-700 hover:bg-zinc-100"
                      >
                        Link
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          close();
                          props.onOpen(item.note.id, item.matched_block_id);
                        }}
                        className="rounded px-2 py-0.5 text-zinc-500 hover:bg-zinc-100 hover:text-zinc-800"
                      >
                        Open
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          )}
          <label className="mt-3 flex items-center gap-2 border-t border-zinc-100 pt-2 text-xs text-zinc-500">
            <input
              type="checkbox"
              checked={suggestionsOn}
              onChange={(event) => props.onToggleSuggestions(event.target.checked)}
            />
            Look for related paragraphs while I write
          </label>
        </div>
      )}
    </span>
  );
}
