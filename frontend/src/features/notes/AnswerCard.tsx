import type { AskResponse } from "./types";

interface Props {
  question: string;
  loading: boolean;
  error: string | null;
  response: AskResponse | undefined;
  onOpen: (documentId: string, blockId: string) => void;
  onDismiss: () => void;
}

/** An answer built only from your notes; every sentence links to where it came from. */
export function AnswerCard({ question, loading, error, response, onOpen, onDismiss }: Props) {
  // Number each cited paragraph once, in order of first use.
  const cited: { block_id: string; document_id: string; document_title: string }[] = [];
  const numberOf = (blockId: string) => cited.findIndex((c) => c.block_id === blockId) + 1;
  for (const sentence of response?.answer ?? []) {
    for (const citation of sentence.citations) {
      if (!numberOf(citation.block_id)) cited.push(citation);
    }
  }

  return (
    <section
      aria-label="Answer from your notes"
      className="mx-3 mt-2 mb-1 rounded-lg border border-indigo-200 bg-indigo-50/50 p-3 text-sm"
    >
      <div className="mb-1 flex items-start justify-between gap-2">
        <span className="text-[11px] font-semibold tracking-wide text-indigo-700 uppercase">
          From your notes
        </span>
        <button
          type="button"
          aria-label="Dismiss answer"
          onClick={onDismiss}
          className="-mt-1 rounded px-1 text-zinc-400 hover:bg-white hover:text-zinc-700"
        >
          ×
        </button>
      </div>
      {loading && <p className="text-zinc-500">Reading your notes about “{question}”…</p>}
      {error && <p className="text-red-600">{error}</p>}
      {response?.status === "not_configured" && (
        <p className="text-zinc-600">
          Answers need an AI key: set <code>OPENROUTER_API_KEY</code> in <code>backend/.env</code>.
        </p>
      )}
      {response?.status === "timeout" && (
        <p className="text-zinc-600">
          That took too long to answer. The closest passages are listed below.
        </p>
      )}
      {response?.status === "not_found" && (
        <p className="text-zinc-600">
          Your notes don't seem to answer that. The closest passages are listed below.
        </p>
      )}
      {response?.status === "answered" && (
        <>
          <p className="leading-relaxed text-zinc-800">
            {response.answer.map((sentence, index) => (
              <span key={index}>
                {sentence.text}
                {sentence.citations.map((citation) => (
                  <button
                    key={citation.block_id}
                    type="button"
                    title={`“${citation.quote}” — ${citation.document_title}`}
                    onClick={() => onOpen(citation.document_id, citation.block_id)}
                    className="mx-0.5 rounded bg-white px-1 align-super text-[10px] font-semibold text-indigo-700 ring-1 ring-indigo-200 hover:bg-indigo-100"
                  >
                    {numberOf(citation.block_id)}
                  </button>
                ))}{" "}
              </span>
            ))}
          </p>
          <ol className="mt-2 space-y-0.5 text-xs text-zinc-500">
            {cited.map((citation, index) => (
              <li key={citation.block_id}>
                <button
                  type="button"
                  onClick={() => onOpen(citation.document_id, citation.block_id)}
                  className="text-left hover:text-indigo-700 hover:underline"
                >
                  {index + 1}. {citation.document_title}
                </button>
              </li>
            ))}
          </ol>
        </>
      )}
      {response && response.status !== "not_configured" && (
        <p className="mt-2 text-[11px] text-zinc-400">
          {[
            ...(response.steps ?? []).map((step) =>
              step.action === "grep" ? `Searched for “${step.detail}”` : `Read “${step.detail}”`,
            ),
            response.elapsed_ms !== undefined
              ? `${(response.elapsed_ms / 1000).toFixed(1)} s`
              : null,
          ]
            .filter(Boolean)
            .join(" · ")}
        </p>
      )}
    </section>
  );
}
