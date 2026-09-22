import { FormEvent, useState } from "react";
import { StatusBadge } from "./StatusBadge";
import type { NavigationFolder, NavigationNode } from "./types";

export type { NavigationDocument, NavigationFolder, NavigationNode } from "./types";

export interface DocumentSidebarProps {
  nodes: NavigationNode[];
  selectedId: string | null;
  loading?: boolean;
  onSelect: (id: string) => void;
  onCreateFolder: (name: string) => Promise<unknown>;
  onRenameFolder: (id: string, name: string) => Promise<unknown>;
  onDeleteFolder: (id: string) => Promise<unknown>;
  onMoveDocument: (id: string, folderId: string | null) => Promise<unknown>;
}

export function DocumentSidebar({
  nodes,
  selectedId,
  loading = false,
  onSelect,
  onCreateFolder,
  onRenameFolder,
  onDeleteFolder,
  onMoveDocument,
}: DocumentSidebarProps) {
  const [expandedFolders, setExpandedFolders] = useState<Record<string, boolean>>(() =>
    collectFolderIds(nodes).reduce<Record<string, boolean>>((expanded, id) => {
      expanded[id] = true;
      return expanded;
    }, {}),
  );
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [folderName, setFolderName] = useState("");
  const [editingFolderId, setEditingFolderId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const folders = flattenFolders(nodes);

  function toggleFolder(id: string) {
    setExpandedFolders((current) => ({ ...current, [id]: !current[id] }));
  }

  async function runAction(action: () => Promise<unknown>) {
    setActionError(null);
    try {
      await action();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Unable to update documents.");
      throw error;
    }
  }

  function submitNewFolder(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = folderName.trim();
    if (!name) return;
    void runAction(async () => {
      await onCreateFolder(name);
      setFolderName("");
      setCreatingFolder(false);
    }).catch(() => undefined);
  }

  return (
    <nav aria-label="Documents" className="flex flex-1 flex-col overflow-y-auto">
      <div className="border-b border-zinc-100 p-2">
        {creatingFolder ? (
          <form className="flex gap-1" onSubmit={submitNewFolder}>
            <input
              aria-label="Folder name"
              autoFocus
              value={folderName}
              onChange={(event) => setFolderName(event.target.value)}
              className="min-w-0 flex-1 rounded border border-zinc-300 px-2 py-1 text-sm"
              placeholder="Folder name"
            />
            <button type="submit" className="rounded px-2 text-sm text-zinc-700 hover:bg-zinc-100">
              Add
            </button>
            <button
              type="button"
              onClick={() => {
                setCreatingFolder(false);
                setFolderName("");
              }}
              className="rounded px-2 text-sm text-zinc-500 hover:bg-zinc-100"
            >
              Cancel
            </button>
          </form>
        ) : (
          <button
            type="button"
            onClick={() => setCreatingFolder(true)}
            className="w-full rounded px-2 py-1 text-left text-sm font-medium text-zinc-700 hover:bg-zinc-100"
          >
            + New folder
          </button>
        )}
      </div>
      {actionError && (
        <p role="alert" className="px-3 pt-2 text-xs text-red-600">
          {actionError}
        </p>
      )}
      {loading ? (
        <div className="p-4 text-sm text-zinc-400">Loading documents…</div>
      ) : nodes.length === 0 ? (
        <div className="p-4 text-sm text-zinc-400">No documents.</div>
      ) : (
        <ul className="py-1">
          {nodes.map((node) => (
            <NavigationRow
              key={node.id}
              node={node}
              depth={0}
              expandedFolders={expandedFolders}
              selectedId={selectedId}
              folders={folders}
              editingFolderId={editingFolderId}
              onSelect={onSelect}
              onToggleFolder={toggleFolder}
              onRenameFolder={async (id, name) => {
                await runAction(() => onRenameFolder(id, name));
                setEditingFolderId(null);
              }}
              onDeleteFolder={(id) => runAction(() => onDeleteFolder(id))}
              onMoveDocument={(id, folderId) => runAction(() => onMoveDocument(id, folderId))}
              onStartRename={setEditingFolderId}
              onCancelRename={() => setEditingFolderId(null)}
            />
          ))}
        </ul>
      )}
    </nav>
  );
}

interface NavigationRowProps {
  node: NavigationNode;
  depth: number;
  expandedFolders: Record<string, boolean>;
  selectedId: string | null;
  folders: FolderOption[];
  editingFolderId: string | null;
  onSelect: (id: string) => void;
  onToggleFolder: (id: string) => void;
  onRenameFolder: (id: string, name: string) => Promise<void>;
  onDeleteFolder: (id: string) => Promise<void>;
  onMoveDocument: (id: string, folderId: string | null) => Promise<void>;
  onStartRename: (id: string) => void;
  onCancelRename: () => void;
}

function NavigationRow({
  node,
  depth,
  expandedFolders,
  selectedId,
  folders,
  editingFolderId,
  onSelect,
  onToggleFolder,
  onRenameFolder,
  onDeleteFolder,
  onMoveDocument,
  onStartRename,
  onCancelRename,
}: NavigationRowProps) {
  if (node.kind === "document") {
    const selected = selectedId === node.id;
    return (
      <li>
        <button
          type="button"
          data-document-id={node.id}
          aria-current={selected ? "page" : undefined}
          onClick={() => onSelect(node.id)}
          className={`flex w-full flex-col gap-1 border-b border-zinc-100 px-3 py-2.5 text-left hover:bg-zinc-50 ${
            selectedId === node.id ? "bg-zinc-100" : ""
          }`}
          style={{ paddingLeft: `${12 + depth * 16}px` }}
        >
          <span className="truncate text-sm font-medium text-zinc-800">{node.title}</span>
          <span className="flex items-center gap-2 text-[11px] text-zinc-400">
            <StatusBadge status={node.status} />
            <span aria-label={`Updated ${formatRecent(node.updated_at)}`}>
              Edited {formatRecent(node.updated_at)}
            </span>
          </span>
        </button>
        {selected && (
          <label
            className="block px-3 pb-2 text-xs text-zinc-500"
            style={{ paddingLeft: `${12 + depth * 16}px` }}
          >
            Move to
            <select
              aria-label={`Move ${node.title}`}
              defaultValue={node.folder_id ?? ""}
              onChange={(event) => {
                const previous = node.folder_id ?? "";
                const next = event.target.value;
                void onMoveDocument(node.id, next || null).catch(() => {
                  event.target.value = previous;
                });
              }}
              className="ml-2 rounded border border-zinc-300 bg-white px-1 py-0.5 text-xs text-zinc-700"
            >
              <option value="">Root</option>
              {folders.map((folder) => (
                <option key={folder.id} value={folder.id}>
                  {`${"— ".repeat(folder.depth)}${folder.name}`}
                </option>
              ))}
            </select>
          </label>
        )}
      </li>
    );
  }

  const expanded = expandedFolders[node.id] ?? true;
  const childrenId = `folder-panel-${encodeURIComponent(node.id)}`;

  return (
    <li>
      <div className="flex items-center">
        {editingFolderId === node.id ? (
          <FolderRenameForm
            folder={node}
            depth={depth}
            onSave={onRenameFolder}
            onCancel={onCancelRename}
          />
        ) : (
          <>
            <button
              type="button"
              aria-expanded={expanded}
              aria-controls={childrenId}
              onClick={() => onToggleFolder(node.id)}
              className="flex min-w-0 flex-1 items-center gap-2 px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-zinc-500 hover:bg-zinc-50 hover:text-zinc-800"
              style={{ paddingLeft: `${12 + depth * 16}px` }}
            >
              <span aria-hidden="true" className="w-3 text-zinc-400">
                {expanded ? "▾" : "▸"}
              </span>
              <span className="truncate">{node.name}</span>
            </button>
            <button
              type="button"
              aria-label={`Rename ${node.name}`}
              onClick={() => onStartRename(node.id)}
              className="rounded px-2 py-1 text-xs text-zinc-500 hover:bg-zinc-100 hover:text-zinc-800"
            >
              Rename
            </button>
            <button
              type="button"
              aria-label={`Delete ${node.name}`}
              disabled={node.children.length > 0}
              title={
                node.children.length > 0 ? "Move or delete this folder's contents first" : undefined
              }
              onClick={() => void onDeleteFolder(node.id).catch(() => undefined)}
              className="rounded px-2 py-1 text-xs text-zinc-500 hover:bg-zinc-100 hover:text-red-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Delete
            </button>
          </>
        )}
      </div>
      {expanded && (
        <ul id={childrenId}>
          {node.children.map((child) => (
            <NavigationRow
              key={child.id}
              node={child}
              depth={depth + 1}
              expandedFolders={expandedFolders}
              selectedId={selectedId}
              folders={folders}
              editingFolderId={editingFolderId}
              onSelect={onSelect}
              onToggleFolder={onToggleFolder}
              onRenameFolder={onRenameFolder}
              onDeleteFolder={onDeleteFolder}
              onMoveDocument={onMoveDocument}
              onStartRename={onStartRename}
              onCancelRename={onCancelRename}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

function FolderRenameForm({
  folder,
  depth,
  onSave,
  onCancel,
}: {
  folder: NavigationFolder;
  depth: number;
  onSave: (id: string, name: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [name, setName] = useState(folder.name);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextName = name.trim();
    if (nextName) void onSave(folder.id, nextName).catch(() => undefined);
  }

  return (
    <form
      className="flex min-w-0 flex-1 gap-1 py-1 pr-2"
      onSubmit={submit}
      style={{ paddingLeft: `${12 + depth * 16}px` }}
    >
      <input
        aria-label={`Rename ${folder.name}`}
        autoFocus
        value={name}
        onChange={(event) => setName(event.target.value)}
        className="min-w-0 flex-1 rounded border border-zinc-300 px-2 py-1 text-sm"
      />
      <button type="submit" className="rounded px-2 text-xs text-zinc-700 hover:bg-zinc-100">
        Save
      </button>
      <button
        type="button"
        onClick={onCancel}
        className="rounded px-2 text-xs text-zinc-500 hover:bg-zinc-100"
      >
        Cancel
      </button>
    </form>
  );
}

function collectFolderIds(nodes: NavigationNode[]): string[] {
  return nodes.flatMap((node) =>
    node.kind === "folder" ? [node.id, ...collectFolderIds(node.children)] : [],
  );
}

interface FolderOption {
  id: string;
  name: string;
  depth: number;
}

function flattenFolders(nodes: NavigationNode[], depth = 0): FolderOption[] {
  return nodes.flatMap((node) =>
    node.kind === "folder"
      ? [{ id: node.id, name: node.name, depth }, ...flattenFolders(node.children, depth + 1)]
      : [],
  );
}

function formatRecent(value: string): string {
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return "recently";

  const elapsed = Math.max(0, Date.now() - timestamp);
  const minutes = Math.floor(elapsed / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;

  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;

  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(timestamp);
}
