/**
 * Replaces a block only when all of its inline content is plain text with one
 * shared style. Mixed styles and links need a range-aware edit, so they stay
 * review-only instead of losing formatting or changing link meaning.
 */
export function safeSuggestionReplacement(
  content: unknown,
  before: string,
  after: string,
): unknown[] | null {
  if (!Array.isArray(content) || content.length === 0) return null;

  const runs = content as Array<{
    type?: unknown;
    text?: unknown;
    styles?: unknown;
    [key: string]: unknown;
  }>;
  if (runs.some((run) => run.type !== "text" || typeof run.text !== "string")) {
    return null;
  }
  if (runs.map((run) => run.text).join("") !== before) return null;

  const firstStyles = runs[0].styles;
  if (!isStyleRecord(firstStyles)) return null;
  if (runs.some((run) => !sameStyles(firstStyles, run.styles))) return null;

  return [{ ...runs[0], text: after }];
}

function isStyleRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function sameStyles(left: Record<string, unknown>, right: unknown): boolean {
  if (!isStyleRecord(right)) return false;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length &&
    keys.every((key) => left[key] === right[key]);
}
