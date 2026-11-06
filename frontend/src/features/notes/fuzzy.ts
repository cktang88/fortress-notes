/**
 * Score how well `query` matches `text` as an in-order subsequence, or null when
 * it doesn't. Higher is better: contiguous runs, word starts, and an exact prefix
 * all count extra, and shorter texts win ties.
 */
export function fuzzyScore(query: string, text: string): number | null {
  const needle = query.trim().toLowerCase();
  if (!needle) return 0;
  const haystack = text.toLowerCase();
  let score = 0;
  let from = 0;
  let previous = -2;
  for (const character of needle) {
    if (character === " ") continue;
    const index = haystack.indexOf(character, from);
    if (index === -1) return null;
    score += 1;
    if (index === previous + 1) score += 3;
    if (index === 0 || /[\s\-_/.(]/.test(haystack[index - 1])) score += 4;
    previous = index;
    from = index + 1;
  }
  if (haystack.startsWith(needle)) score += 10;
  else if (haystack.includes(needle)) score += 6;
  return score - haystack.length / 100;
}

/** Items matching `query`, best first; ties keep their original order. */
export function fuzzyFilter<T>(items: readonly T[], query: string, text: (item: T) => string): T[] {
  return items
    .map((item, index) => ({ item, index, score: fuzzyScore(query, text(item)) }))
    .filter((entry): entry is { item: T; index: number; score: number } => entry.score !== null)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .map((entry) => entry.item);
}
