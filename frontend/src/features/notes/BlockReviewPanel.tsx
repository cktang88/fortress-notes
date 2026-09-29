import { useState } from "react";
import { blockApi } from "./api";
import type {
  BlockDocument,
  BlockNode,
  BlockReviewContext,
  BlockReviewContextResponse,
  BlockReviewResponse,
  DocumentHealthFindings,
} from "./types";

interface Props {
  documentId: string;
  blockId: string;
  selectedBlockIds?: string[];
  canApplySuggestion?: (change: { blockId: string; before: string; after: string }) => boolean;
  onApplySuggestion?: (change: { blockId: string; before: string; after: string }) => boolean;
}

type ReviewDecision = "accepted" | "rejected";
type ReviewDecisions = Record<string, ReviewDecision>;

function decisionsStorageKey(documentId: string) {
  return `fortress-notes:review-decisions:${documentId}`;
}

export function readReviewDecisions(documentId: string): ReviewDecisions {
  try {
    const raw = window.localStorage.getItem(decisionsStorageKey(documentId));
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed).filter(
        (entry): entry is [string, ReviewDecision] =>
          entry[1] === "accepted" || entry[1] === "rejected",
      ),
    );
  } catch {
    return {};
  }
}

export function BlockReviewPanel({
  documentId,
  blockId,
  selectedBlockIds,
  canApplySuggestion,
  onApplySuggestion,
}: Props) {
  const [review, setReview] = useState<
    BlockReviewResponse | BlockReviewContextResponse | null
  >(null);
  const [reviewContext, setReviewContext] = useState<BlockReviewContext>("current");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<(DocumentHealthFindings & { document: BlockDocument }) | null>(null);
  const [healthLoading, setHealthLoading] = useState(false);
  const [decisions, setDecisions] = useState<ReviewDecisions>(() =>
    readReviewDecisions(documentId),
  );

  const recordDecision = (key: string, decision: ReviewDecision) => {
    const next = { ...decisions, [key]: decision };
    setDecisions(next);
    try {
      window.localStorage.setItem(decisionsStorageKey(documentId), JSON.stringify(next));
    } catch {
      setError("Could not save this AI review decision in browser storage");
    }
  };

  const runDocumentChecks = async () => {
    setHealthLoading(true);
    setError(null);
    try {
      const [findings, document] = await Promise.all([
        blockApi.healthFindings(documentId),
        blockApi.get(documentId),
      ]);
      setHealth({ ...findings, document });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Document checks failed");
    } finally {
      setHealthLoading(false);
    }
  };

  const runReview = async () => {
    setLoading(true);
    setError(null);
    try {
      setReview(await blockApi.review(documentId, blockId));
    } catch (cause) {
      setReview(null);
      setError(cause instanceof Error ? cause.message : "Block review failed");
    } finally {
      setLoading(false);
    }
  };

  const runContextReview = async () => {
    setLoading(true);
    setError(null);
    try {
      let blockIds = [blockId];
      if (reviewContext === "document") blockIds = [];
      if (reviewContext === "selected" && selectedBlockIds?.length) {
        blockIds = selectedBlockIds;
      }
      setReview(await blockApi.reviewContext(documentId, reviewContext, blockIds));
    } catch (cause) {
      setReview(null);
      setError(cause instanceof Error ? cause.message : "Context review failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <section aria-label="Block AI review" className="mx-8 mt-2 rounded border p-3 text-sm">
      <div className="flex items-center gap-3">
        <strong>AI review</strong>
        <span className="text-xs text-zinc-500">Block {blockId}</span>
        <button
          type="button"
          className="ml-auto rounded border px-2 py-1 text-xs"
          disabled={loading}
          onClick={() => void runReview()}
        >
          {loading ? "Reviewing…" : "Review block"}
        </button>
        <button type="button" className="rounded border px-2 py-1 text-xs" disabled={healthLoading} onClick={() => void runDocumentChecks()}>
          {healthLoading ? "Checking document…" : "Check document"}
        </button>
      </div>
      <div className="mt-2 flex items-center gap-2">
        <label htmlFor="block-review-context" className="text-xs text-zinc-600">
          Review context
        </label>
        <select
          id="block-review-context"
          value={reviewContext}
          onChange={(event) => setReviewContext(event.target.value as BlockReviewContext)}
          className="rounded border px-2 py-1 text-xs"
        >
          <option value="current">Current block</option>
          <option value="selected">Selected blocks</option>
          <option value="document">Whole document</option>
          <option value="linked">Linked blocks</option>
        </select>
        <button
          type="button"
          className="rounded border px-2 py-1 text-xs"
          disabled={loading}
          onClick={() => void runContextReview()}
        >
          {loading ? "Reviewing…" : "Review context"}
        </button>
      </div>
      {error && <p role="alert" className="mt-2 text-red-600">{error}</p>}
      {review && (
        <div className="mt-2 space-y-2">
          <p>{review.summary}</p>
          {"context" in review && (
            <p className="text-xs text-zinc-500">Context: {review.context}</p>
          )}
          {review.items.length === 0 && <p className="text-zinc-500">No findings.</p>}
          {review.items.map((item, index) => (
            <article key={`${item.block_id}-${index}`} className="rounded bg-zinc-50 p-2">
              <div className="text-xs text-zinc-500">
                {item.label || item.severity} · block {item.block_id}
              </div>
              <blockquote className="my-1 border-l-2 pl-2">{item.quote}</blockquote>
              <p>{item.detail}</p>
            </article>
          ))}
        </div>
      )}
      {health && <DocumentFindings findings={health} decisions={decisions} onDecision={recordDecision} canApplySuggestion={canApplySuggestion} onApplySuggestion={onApplySuggestion} />}
    </section>
  );
}

function DocumentFindings({ findings, decisions, onDecision, canApplySuggestion, onApplySuggestion }: {
  findings: DocumentHealthFindings & { document: BlockDocument };
  decisions: ReviewDecisions;
  onDecision: (key: string, decision: ReviewDecision) => void;
  canApplySuggestion?: Props["canApplySuggestion"];
  onApplySuggestion?: Props["onApplySuggestion"];
}) {
  const blocks: BlockNode[] = [];
  const visit = (nodes: BlockNode[]) => nodes.forEach((node) => { blocks.push(node); visit(node.children); });
  visit(findings.document.children);
  const matched = (text: string) => blocks.filter((block) => text.trim() && block.text.includes(text.trim()));
  const issues = findings.consistency.issues.flatMap((issue) => {
    const targets = matched(issue.claim);
    return targets.map((block) => ({ key: JSON.stringify(["consistency", block.id, issue.related_note_id, issue.claim, issue.conflict]), block, title: "Consistency", claim: issue.claim, detail: issue.conflict }));
  });
  const stale = findings.healing.stale_facts.flatMap((fact) => matched(fact.claim).map((block) => ({ key: JSON.stringify(["stale", block.id, fact.claim, fact.suggestion, fact.finding]), block, claim: fact.claim, suggestion: fact.suggestion, detail: fact.finding })));
  const links = findings.healing.dead_links.flatMap((link) => matched(link.url).map((block) => ({ key: JSON.stringify(["link", block.id, link.url, link.status]), block, url: link.url, status: link.status })));
  const visible = [...issues, ...stale, ...links].filter((item) => !decisions[item.key]);
  return <div className="mt-3 space-y-2 border-t pt-3">
    <strong>Document findings</strong>
    <p className="text-xs text-zinc-500">Findings are attached only when their quoted claim or link matches text in a saved block.</p>
    {!visible.length && <p className="text-zinc-500">No findings matched saved blocks.</p>}
    {issues.filter((item) => !decisions[item.key]).map((item) => <article key={item.key} className="rounded bg-zinc-50 p-2"><div className="text-xs text-zinc-500">Consistency · block {item.block.id}</div><p><b>Claim:</b> {item.claim}</p><p><b>Conflict:</b> {item.detail}</p><DismissButton onClick={() => onDecision(item.key, "rejected")} /></article>)}
    {stale.filter((item) => !decisions[item.key]).map((item) => {
      const change = { blockId: item.block.id, before: item.claim, after: item.suggestion };
      const canApply = canApplyStaleSuggestion(change, item.block.text, canApplySuggestion);
      return <StaleFinding key={item.key} blockId={item.block.id} blockText={item.block.text} claim={item.claim} suggestion={item.suggestion} detail={item.detail} canApply={canApply} onApply={() => onApplySuggestion?.(change) ?? false} onAccept={() => onDecision(item.key, "accepted")} onDismiss={() => onDecision(item.key, "rejected")} />;
    })}
    {links.filter((item) => !decisions[item.key]).map((item) => <article key={item.key} className="rounded bg-zinc-50 p-2"><div className="text-xs text-zinc-500">Broken link · block {item.block.id}</div><p>{item.url} · {item.status}</p><DismissButton onClick={() => onDecision(item.key, "rejected")} /></article>)}
  </div>;
}

export function canApplyStaleSuggestion(
  change: { blockId: string; before: string; after: string },
  blockText: string,
  canApplySuggestion?: Props["canApplySuggestion"],
) {
  return change.before === blockText && !!canApplySuggestion?.(change);
}

export function StaleFinding({ blockId, blockText, claim, suggestion, detail, canApply, onApply, onAccept, onDismiss }: {
  blockId: string;
  blockText: string;
  claim: string;
  suggestion: string;
  detail: string;
  canApply: boolean;
  onApply: () => boolean;
  onAccept: () => void;
  onDismiss: () => void;
}) {
  const exactWholeBlock = claim === blockText;
  return <article className="rounded bg-zinc-50 p-2">
    <div className="text-xs text-zinc-500">Possibly outdated · block {blockId}</div>
    <p><b>Before:</b> {claim}</p><p><b>After:</b> {suggestion}</p><p>{detail}</p>
    {canApply && exactWholeBlock && <button type="button" className="mt-2 rounded border px-2 py-1 text-xs" onClick={() => { if (onApply()) onAccept(); }}>Accept suggestion</button>}
    {!exactWholeBlock && <p className="text-xs text-zinc-500">Accept is available only when the claim is the full block text.</p>}
    <DismissButton onClick={onDismiss} />
  </article>;
}

function DismissButton({ onClick }: { onClick: () => void }) {
  return <button type="button" className="mt-2 rounded border px-2 py-1 text-xs" onClick={onClick}>Reject finding</button>;
}
