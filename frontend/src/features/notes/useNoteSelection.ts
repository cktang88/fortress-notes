import { useEffect, useState } from "react";

export interface FocusRequest {
  blockId: string;
  /** Changes on every request, so asking for the same block again still moves focus. */
  nonce: number;
}

/**
 * The open note and focused block, kept in the URL (`?note=…#block=…`) so links
 * and the browser's Back/Forward buttons work.
 */
export function useNoteSelection() {
  const [selectedId, setSelectedId] = useState<string | null>(() => noteFromUrl());
  const [focus, setFocus] = useState<FocusRequest | null>(() => focusFromUrl());
  const [activeBlockId, setActiveBlockId] = useState<string | null>(null);

  useEffect(() => {
    const restore = () => {
      setSelectedId(noteFromUrl());
      setFocus(focusFromUrl());
      setActiveBlockId(null);
    };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, []);

  function select(id: string, blockId: string | null = null) {
    const changingNote = id !== selectedId;
    setSelectedId(id);
    setFocus(blockId ? { blockId, nonce: Date.now() } : null);
    if (changingNote) setActiveBlockId(null);
    const url = new URL(window.location.href);
    url.searchParams.set("note", id);
    url.hash = blockId ? `block=${encodeURIComponent(blockId)}` : "";
    // A new note is a new history entry; jumping within a note is not.
    if (changingNote) window.history.pushState(null, "", url);
    else window.history.replaceState(null, "", url);
  }

  function clear() {
    setSelectedId(null);
    setFocus(null);
    setActiveBlockId(null);
    const url = new URL(window.location.href);
    url.searchParams.delete("note");
    url.hash = "";
    window.history.replaceState(null, "", url);
  }

  return { selectedId, focus, activeBlockId, setActiveBlockId, select, clear };
}

function noteFromUrl() {
  return new URLSearchParams(window.location.search).get("note");
}

function focusFromUrl(): FocusRequest | null {
  const hash = window.location.hash;
  if (!hash.startsWith("#block=")) return null;
  try {
    const blockId = decodeURIComponent(hash.slice("#block=".length));
    return blockId ? { blockId, nonce: 0 } : null;
  } catch {
    return null;
  }
}
