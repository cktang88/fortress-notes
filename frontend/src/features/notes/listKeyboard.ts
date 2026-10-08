import type { KeyboardEvent } from "react";

/** Arrow-key movement between the buttons of a result list. */
export function moveListFocus(event: KeyboardEvent<HTMLElement>) {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const items = Array.from(event.currentTarget.querySelectorAll<HTMLElement>("[data-list-item]"));
  if (items.length === 0) return;
  event.preventDefault();
  const index = items.indexOf(document.activeElement as HTMLElement);
  const next =
    event.key === "ArrowDown" ? Math.min(items.length - 1, index + 1) : index <= 0 ? -1 : index - 1;
  if (next === -1) {
    document.getElementById(SEARCH_INPUT_ID)?.focus();
    return;
  }
  items[next].focus();
}

/** Focus the first result of the list that follows the search box. */
export function focusFirstResult(): HTMLElement | null {
  const first = document.querySelector<HTMLElement>("[data-results] [data-list-item]");
  first?.focus();
  return first;
}

export const SEARCH_INPUT_ID = "workspace-search";

/** True when typing should go to a field instead of triggering shortcuts. */
export function isEditableTarget(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.isContentEditable ||
    target.closest("input, textarea, select, [contenteditable='true']") !== null
  );
}
