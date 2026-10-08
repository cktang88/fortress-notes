import { useEffect, useRef, useState } from "react";
import { BackupsDialog } from "./BackupsDialog";
import { useImportMarkdown } from "./hooks";
import { TrashDialog } from "./TrashDialog";
import type { Notice } from "./Toast";

interface Props {
  onNotice: (notice: Omit<Notice, "id">) => void;
  onOpenNote: (id: string) => void;
}

type DialogName = "trash" | "backups" | null;

/** Import, trash and backups: the things you do to the whole workspace. */
export function WorkspaceMenu({ onNotice, onOpenNote }: Props) {
  const [open, setOpen] = useState(false);
  const [dialog, setDialog] = useState<DialogName>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const importMarkdown = useImportMarkdown();

  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLElement>("[role='menuitem']")?.focus();
    const closeOutside = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!menuRef.current?.contains(target) && !buttonRef.current?.contains(target))
        setOpen(false);
    };
    const closeEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
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

  function choose(action: () => void) {
    setOpen(false);
    action();
  }

  async function importFiles(files: File[]) {
    if (files.length === 0) return;
    try {
      const result = await importMarkdown.mutateAsync({ files });
      const count = result.imported.length;
      const failed = result.errors.map((error) => error.name).join(", ");
      onNotice({
        tone: count === 0 ? "error" : "info",
        message:
          (count > 0 ? `Imported ${count} ${count === 1 ? "note" : "notes"}` : "Nothing imported") +
          (failed ? ` · couldn't read ${failed}` : ""),
      });
      if (result.imported[0]) onOpenNote(result.imported[0].id);
    } catch (error) {
      onNotice({
        tone: "error",
        message: error instanceof Error ? error.message : "Import failed",
      });
    }
  }

  return (
    <div className="relative shrink-0">
      <button
        ref={buttonRef}
        type="button"
        aria-label="Workspace menu"
        title="Import, trash and backups"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="rounded-md border border-zinc-300 px-2.5 py-1.5 text-sm text-zinc-600 hover:bg-zinc-100"
      >
        ⋯
      </button>
      {open && (
        <div
          ref={menuRef}
          role="menu"
          aria-label="Workspace"
          onKeyDown={(event) => {
            if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
            event.preventDefault();
            const items = Array.from(
              event.currentTarget.querySelectorAll<HTMLElement>("[role='menuitem']"),
            );
            const index = items.indexOf(document.activeElement as HTMLElement);
            const next =
              (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
            items[next]?.focus();
          }}
          className="absolute right-0 z-40 mt-1 w-56 rounded-md border border-zinc-200 bg-white py-1 shadow-lg"
        >
          <Item onClick={() => choose(() => fileRef.current?.click())}>Import Markdown files…</Item>

          <div className="my-1 border-t border-zinc-100" />
          <Item onClick={() => choose(() => setDialog("trash"))}>Trash</Item>
          <Item onClick={() => choose(() => setDialog("backups"))}>Backups…</Item>
        </div>
      )}
      <input
        ref={fileRef}
        type="file"
        accept=".md,.markdown,.txt,text/markdown,text/plain"
        multiple
        hidden
        data-testid="markdown-import-input"
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []);
          event.target.value = "";
          void importFiles(files);
        }}
      />
      <TrashDialog
        open={dialog === "trash"}
        onClose={() => setDialog(null)}
        onRestored={(id, title) =>
          onNotice({
            message: `Restored “${title}”`,
            action: { label: "Open", run: () => onOpenNote(id) },
          })
        }
      />
      <BackupsDialog
        open={dialog === "backups"}
        onClose={() => setDialog(null)}
        onRestored={(message) => {
          setDialog(null);
          onNotice({ message });
        }}
      />
    </div>
  );
}

function Item({ children, onClick }: { children: React.ReactNode; onClick: () => void }) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className="block w-full px-3 py-2 text-left text-sm text-zinc-700 hover:bg-zinc-100 focus:bg-zinc-100 focus:outline-none"
    >
      {children}
    </button>
  );
}
