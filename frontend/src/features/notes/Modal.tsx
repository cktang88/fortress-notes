import { useEffect, useId, useRef } from "react";

interface Props {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  wide?: boolean;
}

/** A native modal dialog: focus is trapped, Escape and the × button close it. */
export function Modal({ open, title, onClose, children, wide = false }: Props) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
      // Prefer the field marked for typing over the first button (Close).
      dialog.querySelector<HTMLElement>("[data-autofocus]")?.focus();
    }
    if (!open && dialog.open) {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        // Clicking the backdrop (the dialog element itself) closes it.
        if (event.target === event.currentTarget) onClose();
      }}
      className={`m-auto w-full ${wide ? "max-w-xl" : "max-w-md"} rounded-lg bg-white p-0 shadow-xl backdrop:bg-black/30`}
    >
      {open && (
        <div className="p-5">
          <div className="mb-3 flex items-center justify-between gap-4">
            <h2 id={titleId} className="text-base font-semibold text-zinc-900">
              {title}
            </h2>
            <button
              type="button"
              aria-label="Close"
              onClick={onClose}
              className="rounded px-2 py-1 text-zinc-500 hover:bg-zinc-100"
            >
              ×
            </button>
          </div>
          {children}
        </div>
      )}
    </dialog>
  );
}
