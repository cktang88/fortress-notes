import type { NoteSummary } from "./types";

const MAX_LINK_SUGGESTIONS = 8;

export function matchingLinkTargets(
  targets: readonly NoteSummary[],
  query: string,
  limit = MAX_LINK_SUGGESTIONS,
): NoteSummary[] {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  return targets
    .filter((target) => {
      if (!normalizedQuery) return true;
      return (
        target.title.toLocaleLowerCase().includes(normalizedQuery) ||
        target.id.toLocaleLowerCase().includes(normalizedQuery)
      );
    })
    .sort((left, right) => {
      const leftTitle = left.title.toLocaleLowerCase();
      const rightTitle = right.title.toLocaleLowerCase();
      const leftStarts = leftTitle.startsWith(normalizedQuery);
      const rightStarts = rightTitle.startsWith(normalizedQuery);
      if (leftStarts !== rightStarts) return leftStarts ? -1 : 1;
      return leftTitle.localeCompare(rightTitle);
    })
    .slice(0, limit);
}

export function documentLink(target: Pick<NoteSummary, "id" | "title">): string {
  const label = target.title.replace(/[|\]]/g, " ").replace(/\s+/g, " ").trim();
  return `[[${target.id}${label ? `|${label}` : ""}]]`;
}
