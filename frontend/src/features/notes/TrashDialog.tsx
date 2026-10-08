import { useState } from "react";
import { Modal } from "./Modal";
import { useDeleteForever, useEmptyTrash, useRestoreDocument, useTrash } from "./hooks";

interface Props {
  open: boolean;
  onClose: () => void;
  onRestored: (id: string, title: string) => void;
}

export function TrashDialog({ open, onClose, onRestored }: Props) {
  const trash = useTrash(open);
  const restore = useRestoreDocument();
  const deleteForever = useDeleteForever();
  const emptyTrash = useEmptyTrash();
  const [confirmEmpty, setConfirmEmpty] = useState(false);
  const items = trash.data ?? [];
  const error = restore.error ?? deleteForever.error ?? emptyTrash.error;

  return (
    <Modal
      open={open}
      title="Trash"
      wide
      onClose={() => {
        setConfirmEmpty(false);
        onClose();
      }}
    >
      <p className="mb-3 text-sm text-zinc-500">
        Deleted notes stay here until you delete them forever.
      </p>
      {error && (
        <p role="alert" className="mb-2 text-sm text-red-600">
          {error.message}
        </p>
      )}
      {trash.isLoading && <p className="text-sm text-zinc-400">Loading…</p>}
      {!trash.isLoading && items.length === 0 && (
        <p className="rounded-md bg-zinc-50 px-3 py-6 text-center text-sm text-zinc-400">
          Trash is empty.
        </p>
      )}
      {items.length > 0 && (
        <ul className="max-h-80 divide-y divide-zinc-100 overflow-y-auto rounded-md border border-zinc-200">
          {items.map((item) => (
            <li key={item.id} className="flex items-center gap-3 px-3 py-2">
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium text-zinc-800">{item.title}</div>
                <div className="truncate text-xs text-zinc-400">
                  Deleted {new Date(item.deleted_at).toLocaleString()}
                  {item.snippet && ` · ${item.snippet}`}
                </div>
              </div>
              <button
                type="button"
                onClick={() =>
                  restore.mutate(item.id, { onSuccess: () => onRestored(item.id, item.title) })
                }
                className="shrink-0 rounded-md border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50"
              >
                Restore
              </button>
              <button
                type="button"
                aria-label={`Delete ${item.title} forever`}
                onClick={() => deleteForever.mutate(item.id)}
                className="shrink-0 rounded-md px-2 py-1 text-xs text-zinc-400 hover:bg-red-50 hover:text-red-600"
              >
                Delete forever
              </button>
            </li>
          ))}
        </ul>
      )}
      {items.length > 0 && (
        <div className="mt-4 flex items-center justify-end gap-2">
          {confirmEmpty ? (
            <>
              <span className="text-sm text-zinc-600">
                Permanently delete {items.length} {items.length === 1 ? "note" : "notes"}?
              </span>
              <button
                type="button"
                onClick={() => setConfirmEmpty(false)}
                className="rounded-md px-2.5 py-1 text-sm text-zinc-600 hover:bg-zinc-100"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() =>
                  emptyTrash.mutate(undefined, { onSuccess: () => setConfirmEmpty(false) })
                }
                className="rounded-md bg-red-600 px-3 py-1 text-sm font-medium text-white hover:bg-red-500"
              >
                Empty trash
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmEmpty(true)}
              className="rounded-md px-2.5 py-1 text-sm text-red-600 hover:bg-red-50"
            >
              Empty trash…
            </button>
          )}
        </div>
      )}
    </Modal>
  );
}
