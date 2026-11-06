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

/**
 * True while an input method (Japanese, Chinese, …) is composing: Enter then confirms
 * the composition and must not submit or run anything. Safari reports the confirming
 * Enter with keyCode 229 instead of isComposing.
 */
export function isComposing(event: KeyboardEvent) {
  const native = event.nativeEvent;
  return native.isComposing || native.keyCode === 229;
}

/** The open modal dialog shown last (the one on top), or null. */
export function topModalDialog(): HTMLDialogElement | null {
  const open = Array.from(document.querySelectorAll<HTMLDialogElement>("dialog[open]"));
  return open.filter(isModalDialog).at(-1) ?? null;
}

function isModalDialog(dialog: HTMLDialogElement) {
  try {
    return dialog.matches(":modal");
  } catch {
    // Engines without :modal (such as jsdom): every dialog here is shown modally.
    return true;
  }
}
