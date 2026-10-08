import { useState } from "react";
import { workspaceApi } from "./api";
import { triggerDownload } from "./download";
import { Modal } from "./Modal";
import { useBackups, useCreateBackup, useRestoreBackup } from "./hooks";
import type { Backup } from "./types";

interface Props {
  open: boolean;
  onClose: () => void;
  onRestored: (message: string) => void;
}

const kindLabels: Record<Backup["kind"], string> = {
  auto: "Daily",
  manual: "Manual",
  "pre-restore": "Before restore",
};

export function BackupsDialog({ open, onClose, onRestored }: Props) {
  const backups = useBackups(open);
  const createBackup = useCreateBackup();
  const restoreBackup = useRestoreBackup();
  const [confirming, setConfirming] = useState<string | null>(null);
  const items = backups.data ?? [];
  const error = createBackup.error ?? restoreBackup.error;

  return (
    <Modal
      open={open}
      title="Backups"
      wide
      onClose={() => {
        setConfirming(null);
        onClose();
      }}
    >
      <p className="mb-3 text-sm text-zinc-500">
        A snapshot of every note is saved automatically once a day. Restoring first saves your
        current notes, so a restore can be undone.
      </p>
      <div className="mb-3 flex gap-2">
        <button
          type="button"
          disabled={createBackup.isPending}
          onClick={() =>
            createBackup.mutate(undefined, {
              onSuccess: (backup) =>
                triggerDownload(workspaceApi.backupUrl(backup.name), backup.name),
            })
          }
          className="rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
        >
          {createBackup.isPending ? "Backing up…" : "Back up now & download"}
        </button>
      </div>
      {error && (
        <p role="alert" className="mb-2 text-sm text-red-600">
          {error.message}
        </p>
      )}
      {backups.isLoading && <p className="text-sm text-zinc-400">Loading…</p>}
      {!backups.isLoading && items.length === 0 && (
        <p className="rounded-md bg-zinc-50 px-3 py-6 text-center text-sm text-zinc-400">
          No backups yet.
        </p>
      )}
      {items.length > 0 && (
        <ul className="max-h-72 divide-y divide-zinc-100 overflow-y-auto rounded-md border border-zinc-200">
          {items.map((backup) => (
            <li key={backup.name} className="flex items-center gap-3 px-3 py-2">
              <div className="min-w-0 flex-1">
                <div className="text-sm text-zinc-800">
                  {new Date(backup.created_at).toLocaleString()}
                </div>
                <div className="text-xs text-zinc-400">
                  {kindLabels[backup.kind]} · {formatSize(backup.size)}
                </div>
              </div>
              {confirming === backup.name ? (
                <>
                  <button
                    type="button"
                    onClick={() => setConfirming(null)}
                    className="rounded-md px-2 py-1 text-xs text-zinc-600 hover:bg-zinc-100"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    disabled={restoreBackup.isPending}
                    onClick={() =>
                      restoreBackup.mutate(backup.name, {
                        onSuccess: () => {
                          setConfirming(null);
                          onRestored(
                            `Restored notes from ${new Date(backup.created_at).toLocaleString()}`,
                          );
                        },
                      })
                    }
                    className="rounded-md bg-amber-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-amber-500 disabled:opacity-50"
                  >
                    {restoreBackup.isPending ? "Restoring…" : "Replace current notes"}
                  </button>
                </>
              ) : (
                <>
                  <a
                    href={workspaceApi.backupUrl(backup.name)}
                    download={backup.name}
                    className="rounded-md px-2 py-1 text-xs text-zinc-500 hover:bg-zinc-100"
                  >
                    Download
                  </a>
                  <button
                    type="button"
                    onClick={() => setConfirming(backup.name)}
                    className="rounded-md border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50"
                  >
                    Restore…
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
