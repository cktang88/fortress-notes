import { useEffect, useEffectEvent, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { topModalDialog } from "./listKeyboard";

export interface Notice {
  id: number;
  message: string;
  tone?: "info" | "error";
  action?: { label: string; run: () => void };
}

interface Props {
  notice: Notice | null;
  onDismiss: () => void;
}

/** A short-lived message in the corner, with an optional action such as Undo. */
export function Toast({ notice, onDismiss }: Props) {
  const expire = useEffectEvent(onDismiss);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => expire(), notice.action ? 8000 : 4000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  // While a modal dialog is open the rest of the page is inert, and a click there only
  // lands on the dialog's backdrop. Render inside the dialog then, so Undo/Open work.
  const modal = useSyncExternalStore(subscribeToDialogs, topModalDialog, () => null);

  const toast = (
    <div
      aria-live="polite"
      className="pointer-events-none fixed bottom-4 left-1/2 z-50 -translate-x-1/2"
    >
      {notice && (
        <div
          role={notice.tone === "error" ? "alert" : "status"}
          className={`pointer-events-auto flex items-center gap-3 rounded-lg px-4 py-2.5 text-sm shadow-lg ${
            notice.tone === "error" ? "bg-red-600 text-white" : "bg-zinc-900 text-white"
          }`}
        >
          <span>{notice.message}</span>
          {notice.action && (
            <button
              type="button"
              onClick={() => {
                notice.action?.run();
                onDismiss();
              }}
              className="font-semibold text-amber-300 hover:text-amber-200"
            >
              {notice.action.label}
            </button>
          )}
          <button
            type="button"
            aria-label="Dismiss"
            onClick={onDismiss}
            className="text-zinc-400 hover:text-white"
          >
            ×
          </button>
        </div>
      )}
    </div>
  );
  return modal ? createPortal(toast, modal) : toast;
}

function subscribeToDialogs(onChange: () => void) {
  const observer = new MutationObserver(onChange);
  observer.observe(document.body, { subtree: true, attributes: true, attributeFilter: ["open"] });
  return () => observer.disconnect();
}
