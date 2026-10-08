import { useEffect, useId, useRef, useState } from "react";
import { fuzzyFilter } from "./fuzzy";
import type { NoteSummary } from "./types";
import type { WorkspaceAction } from "./WorkspaceMenu";

interface Props {
  open: boolean;
  onClose: () => void;
  notes: readonly NoteSummary[];
  /** IDs of recently edited notes, most recent first (shown before typing). */
  recentIds: readonly string[];
  onOpenNote: (id: string) => void;
  onCreateNote: (title: string) => void;
  onSearchEverywhere: (query: string) => void;
  onWorkspaceAction: (action: WorkspaceAction) => void;
}

type Item = {
  key: string;
  label: string;
  hint: string;
  run: () => void;
};

const NOTE_LIMIT = 8;

/** ⌘K: jump to a note by title, create one, search everywhere, or run a command. */
export function CommandPalette(props: Props) {
  const { open, onClose } = props;
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
      inputRef.current?.focus();
    }
    if (!open && dialog.open) {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }
  }, [open]);

  const items = buildItems(props, query);
  const current = Math.min(active, Math.max(items.length - 1, 0));

  function close() {
    setQuery("");
    setActive(0);
    onClose();
  }

  function run(item: Item | undefined) {
    if (!item) return;
    close();
    item.run();
  }

  return (
    <dialog
      ref={dialogRef}
      aria-label="Go to a note or run a command"
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) close();
      }}
      className="mx-auto mt-[12vh] w-full max-w-lg rounded-xl bg-white p-0 shadow-2xl backdrop:bg-black/30"
    >
      {open && (
        <div className="flex flex-col">
          <input
            ref={inputRef}
            role="combobox"
            aria-expanded="true"
            aria-controls={listId}
            aria-activedescendant={items[current] ? `${listId}-${current}` : undefined}
            aria-label="Go to a note or run a command"
            value={query}
            placeholder="Go to a note, or type a title to create one…"
            onChange={(event) => {
              setQuery(event.target.value);
              setActive(0);
            }}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setActive((current + 1) % Math.max(items.length, 1));
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                setActive((current - 1 + items.length) % Math.max(items.length, 1));
              } else if (event.key === "Enter") {
                event.preventDefault();
                run(items[current]);
              }
            }}
            className="border-b border-zinc-200 px-4 py-3 text-base outline-none"
          />
          <ul id={listId} role="listbox" className="max-h-80 overflow-y-auto py-1">
            {items.length === 0 && (
              <li className="px-4 py-3 text-sm text-zinc-400">Nothing matches.</li>
            )}
            {items.map((item, index) => (
              <li
                key={item.key}
                id={`${listId}-${index}`}
                role="option"
                aria-selected={index === current}
                onMouseMove={() => setActive(index)}
                onClick={() => run(item)}
                className={`flex cursor-pointer items-center justify-between gap-3 px-4 py-2 text-sm ${
                  index === current ? "bg-zinc-100" : ""
                }`}
              >
                <span className="truncate text-zinc-800">{item.label}</span>
                <span className="shrink-0 text-xs text-zinc-400">{item.hint}</span>
              </li>
            ))}
          </ul>
          <div className="flex gap-3 border-t border-zinc-100 px-4 py-2 text-[11px] text-zinc-400">
            <span>↑↓ to move</span>
            <span>Enter to open</span>
            <span>Esc to close</span>
          </div>
        </div>
      )}
    </dialog>
  );
}

export function buildItems(props: Props, query: string): Item[] {
  const trimmed = query.trim();
  const byId = new Map(props.notes.map((note) => [note.id, note]));
  const noteItem = (note: NoteSummary): Item => ({
    key: `note:${note.id}`,
    label: note.title || "Untitled",
    hint: note.status === "polished" ? "Polished" : "Note",
    run: () => props.onOpenNote(note.id),
  });

  const actions: Item[] = [
    {
      key: "action:new",
      label: "New note",
      hint: "Alt+N",
      run: () => props.onCreateNote(""),
    },
    {
      key: "action:import",
      label: "Import Markdown files…",
      hint: "Action",
      run: () => props.onWorkspaceAction("import"),
    },
    {
      key: "action:trash",
      label: "Open Trash",
      hint: "Action",
      run: () => props.onWorkspaceAction("trash"),
    },
    {
      key: "action:backups",
      label: "Backups…",
      hint: "Action",
      run: () => props.onWorkspaceAction("backups"),
    },
  ];

  if (!trimmed) {
    const recent = props.recentIds
      .map((id) => byId.get(id))
      .filter((note): note is NoteSummary => note !== undefined)
      .slice(0, NOTE_LIMIT - 2);
    return [...recent.map(noteItem), ...actions];
  }

  const notes = fuzzyFilter(props.notes, trimmed, (note) => note.title).slice(0, NOTE_LIMIT);
  const exact = props.notes.some(
    (note) => note.title.trim().toLowerCase() === trimmed.toLowerCase(),
  );
  const extras: Item[] = [
    ...(exact
      ? []
      : [
          {
            key: "action:create",
            label: `Create note “${trimmed}”`,
            hint: "New",
            run: () => props.onCreateNote(trimmed),
          },
        ]),
    {
      key: "action:search",
      label: `Search everywhere for “${trimmed}”`,
      hint: "Full text",
      run: () => props.onSearchEverywhere(trimmed),
    },
  ];
  // A command that plainly names what you typed ("trash") beats creating a note with
  // that title; a looser match ("new ideas" ~ "New note") must not.
  const matching = fuzzyFilter(actions, trimmed, (action) => action.label);
  const named = matching.filter((action) =>
    action.label.toLowerCase().includes(trimmed.toLowerCase()),
  );
  const loose = matching.filter((action) => !named.includes(action));
  return [...notes.map(noteItem), ...named, ...extras, ...loose];
}
