import {
  DragDropProvider,
  DragOverlay,
  useDraggable,
  useDroppable,
  type DragEndEvent,
} from "@dnd-kit/react";
import { FormEvent, KeyboardEvent, MouseEvent, useEffect, useRef, useState } from "react";
import { StatusBadge } from "./StatusBadge";
import type { NavigationDocument, NavigationFolder, NavigationNode } from "./types";

export type { NavigationDocument, NavigationFolder, NavigationNode } from "./types";

export interface DocumentSidebarProps {
  nodes: NavigationNode[];
  recent: NavigationDocument[];
  selectedId: string | null;
  loading?: boolean;
  onSelect: (id: string) => void;
  onCreateFolder: (name: string, parentId: string | null) => Promise<unknown>;
  onCreateNote: (folderId: string | null) => Promise<unknown>;
  onRenameFolder: (id: string, name: string) => Promise<unknown>;
  onRenameDocument: (id: string, title: string) => Promise<unknown>;
  onDeleteFolder: (id: string) => Promise<unknown>;
  onDeleteDocument: (id: string) => Promise<unknown>;
  onMoveDocument: (id: string, folderId: string | null) => Promise<unknown>;
  onMoveFolder: (id: string, parentId: string | null) => Promise<unknown>;
}

type MenuTarget =
  | { kind: "root" }
  | { kind: "folder"; id: string }
  | { kind: "document"; id: string; recent?: boolean };
type DragData =
  | { kind: "folder"; id: string; parentId: string | null }
  | { kind: "document"; id: string; parentId: string | null };
type DropData = { kind: "root" } | { kind: "folder"; id: string };

export function DocumentSidebar({
  nodes,
  recent,
  selectedId,
  loading = false,
  onSelect,
  onCreateFolder,
  onCreateNote,
  onRenameFolder,
  onRenameDocument,
  onDeleteFolder,
  onDeleteDocument,
  onMoveDocument,
  onMoveFolder,
}: DocumentSidebarProps) {
  const [expandedFolders, setExpandedFolders] = useState<Record<string, boolean>>(() =>
    collectFolderIds(nodes).reduce<Record<string, boolean>>((expanded, id) => {
      expanded[id] = true;
      return expanded;
    }, {}),
  );
  const [menu, setMenu] = useState<{ target: MenuTarget; x: number; y: number } | null>(null);
  const [creatingParent, setCreatingParent] = useState<string | null | undefined>(undefined);
  const [folderName, setFolderName] = useState("");
  const [editingFolderId, setEditingFolderId] = useState<string | null>(null);
  const [editingDocumentId, setEditingDocumentId] = useState<string | null>(null);
  const [editingRecentId, setEditingRecentId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [dragItem, setDragItem] = useState<DragData | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuOpenerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!menu) return;
    menuRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const closeOutside = (event: globalThis.MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) closeMenu();
    };
    const closeEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeMenu(true);
      }
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeEscape);
    };
    // closeMenu reads the current opener and does not need to restart listeners.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [menu]);

  function closeMenu(restoreFocus = false) {
    setMenu(null);
    if (restoreFocus) menuOpenerRef.current?.focus();
  }

  function showMenu(target: MenuTarget, event: MouseEvent | KeyboardEvent) {
    event.preventDefault();
    if (event.currentTarget instanceof HTMLElement) menuOpenerRef.current = event.currentTarget;
    const x = "clientX" in event ? event.clientX : 12;
    const y = "clientY" in event ? event.clientY : 12;
    setMenu({
      target,
      x: Math.max(0, Math.min(x, window.innerWidth - 200)),
      y: Math.max(0, Math.min(y, window.innerHeight - 260)),
    });
  }

  function keyboardMenu(event: KeyboardEvent, target: MenuTarget) {
    if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10"))
      showMenu(target, event);
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

  function toggleFolder(id: string) {
    setExpandedFolders((current) => ({ ...current, [id]: !current[id] }));
  }

  function startCreateFolder(parentId: string | null) {
    setFolderName("");
    setCreatingParent(parentId);
    if (parentId) setExpandedFolders((current) => ({ ...current, [parentId]: true }));
    closeMenu();
  }

  function submitNewFolder(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = folderName.trim();
    if (!name || creatingParent === undefined) return;
    void runAction(async () => {
      await onCreateFolder(name, creatingParent);
      if (creatingParent) setExpandedFolders((current) => ({ ...current, [creatingParent]: true }));
      setFolderName("");
      setCreatingParent(undefined);
    }).catch(() => undefined);
  }

  async function createNote(folderId: string | null) {
    closeMenu();
    if (folderId) setExpandedFolders((current) => ({ ...current, [folderId]: true }));
    try {
      await runAction(() => onCreateNote(folderId));
    } catch {
      // The visible error is rendered in the sidebar.
    }
  }

  function openContextMenu(event: MouseEvent, target: MenuTarget) {
    event.stopPropagation();
    showMenu(target, event);
  }

  function handleDragEnd(event: DragEndEvent) {
    const item = event.canceled ? null : readDragData(event.operation.source?.data);
    const target = readDropData(event.operation.target?.data);
    setDragItem(null);
    if (!item || !target) return;

    if (target.kind === "root") {
      if (item.parentId === null) return;
      void runAction(() =>
        item.kind === "folder" ? onMoveFolder(item.id, null) : onMoveDocument(item.id, null),
      ).catch(() => undefined);
      return;
    }

    if (item.id === target.id || item.parentId === target.id) return;
    if (item.kind === "folder") {
      const source = findFolder(nodes, item.id);
      if (!source || isDescendantFromNodes(source, target.id)) return;
      void runAction(async () => {
        await onMoveFolder(item.id, target.id);
        setExpandedFolders((current) => ({ ...current, [target.id]: true }));
      }).catch(() => undefined);
      return;
    }

    void runAction(async () => {
      await onMoveDocument(item.id, target.id);
      setExpandedFolders((current) => ({ ...current, [target.id]: true }));
    }).catch(() => undefined);
  }

  function renderNodes(children: NavigationNode[], depth: number) {
    return children.map((node) => (
      <NavigationRow
        key={`${node.kind}:${node.id}`}
        node={node}
        depth={depth}
        expandedFolders={expandedFolders}
        selectedId={selectedId}
        creatingParent={creatingParent}
        folderName={folderName}
        editingFolderId={editingFolderId}
        editingDocumentId={editingDocumentId}
        dragItem={dragItem}
        allNodes={nodes}
        onSelect={onSelect}
        onToggleFolder={toggleFolder}
        onContextMenu={openContextMenu}
        onKeyboardMenu={keyboardMenu}
        onFolderNameChange={setFolderName}
        onSubmitFolder={submitNewFolder}
        onCancelCreate={() => setCreatingParent(undefined)}
        onRenameFolder={async (id, name) => {
          await runAction(() => onRenameFolder(id, name));
          setEditingFolderId(null);
        }}
        onRenameDocument={async (id, title) => {
          await runAction(() => onRenameDocument(id, title));
          setEditingDocumentId(null);
        }}
        onDeleteDocument={(id) => runAction(() => onDeleteDocument(id))}
        onStartRenameFolder={(id) => {
          setEditingDocumentId(null);
          setEditingRecentId(null);
          setEditingFolderId(id);
        }}
        onStartRenameDocument={(id) => {
          setEditingFolderId(null);
          setEditingRecentId(null);
          setEditingDocumentId(id);
        }}
        onCancelRename={() => {
          setEditingFolderId(null);
          setEditingDocumentId(null);
        }}
        onExpandFolder={(id) => setExpandedFolders((current) => ({ ...current, [id]: true }))}
      />
    ));
  }

  const dragLabel =
    dragItem?.kind === "folder"
      ? findFolder(nodes, dragItem.id)?.name
      : dragItem
        ? (findDocument(nodes, dragItem.id) ?? recent.find((item) => item.id === dragItem.id))
            ?.title
        : null;

  return (
    <DragDropProvider
      onDragStart={(event) => setDragItem(readDragData(event.operation.source?.data))}
      onDragEnd={handleDragEnd}
    >
      <nav
        aria-label="Documents"
        className="relative flex flex-1 flex-col overflow-y-auto"
        onContextMenu={(event) => {
          if (!(event.target as HTMLElement).closest("[data-folder-id], [data-document-id]"))
            openContextMenu(event, { kind: "root" });
        }}
      >
        <div className="border-b border-zinc-100 p-2">
          <div className="flex items-center gap-1">
            <RootDropButton
              dragItem={dragItem}
              onContextMenu={openContextMenu}
              onKeyboardMenu={keyboardMenu}
            />
            <button
              type="button"
              aria-label="Add to top level"
              onClick={(event) => openContextMenu(event, { kind: "root" })}
              className="rounded px-2 py-2 text-sm text-zinc-600 hover:bg-zinc-100"
            >
              +
            </button>
          </div>
        </div>
        {actionError && (
          <p role="alert" className="px-3 pt-2 text-xs text-red-600">
            {actionError}
          </p>
        )}
        {!loading && recent.length > 0 && (
          <section aria-label="Recent documents" className="border-b border-zinc-100 py-1">
            <h2 className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
              Recent
            </h2>
            <ul>
              {recent.map((document) => (
                <RecentDocumentRow
                  key={document.id}
                  document={document}
                  selected={selectedId === document.id}
                  editing={editingRecentId === document.id}
                  onSelect={onSelect}
                  onRename={async (id, title) => {
                    await runAction(() => onRenameDocument(id, title));
                    setEditingRecentId(null);
                  }}
                  onStartRename={() => {
                    setEditingFolderId(null);
                    setEditingDocumentId(null);
                    setEditingRecentId(document.id);
                  }}
                  onCancel={() => setEditingRecentId(null)}
                  onContextMenu={openContextMenu}
                  onKeyboardMenu={keyboardMenu}
                />
              ))}
            </ul>
          </section>
        )}
        {loading ? (
          <div className="p-4 text-sm text-zinc-400">Loading documents…</div>
        ) : (
          <ul
            className="min-h-10 flex-1 py-1"
            onContextMenu={(event) => {
              if (!(event.target as HTMLElement).closest("[data-folder-id], [data-document-id]"))
                openContextMenu(event, { kind: "root" });
            }}
          >
            {renderNodes(nodes, 0)}
            {nodes.length === 0 && creatingParent === undefined && (
              <li className="p-4 text-sm text-zinc-400">No documents. Right-click to add one.</li>
            )}
            {creatingParent === null && (
              <li>
                <FolderCreateForm
                  name={folderName}
                  destination="top level"
                  onNameChange={setFolderName}
                  onSubmit={submitNewFolder}
                  onCancel={() => setCreatingParent(undefined)}
                />
              </li>
            )}
          </ul>
        )}
        {menu && (
          <div
            ref={menuRef}
            role="menu"
            aria-label="Document actions"
            className="fixed z-50 min-w-44 rounded-md border border-zinc-200 bg-white py-1 shadow-lg"
            style={{ left: menu.x, top: menu.y }}
          >
            <MenuButton
              onClick={() =>
                startCreateFolder(menu.target.kind === "folder" ? menu.target.id : null)
              }
            >
              New folder
            </MenuButton>
            <MenuButton
              onClick={() => void createNote(menu.target.kind === "folder" ? menu.target.id : null)}
            >
              New note
            </MenuButton>
            {menu.target.kind === "folder" && (
              <>
                <MenuButton
                  onClick={() => {
                    setEditingDocumentId(null);
                    setEditingRecentId(null);
                    setEditingFolderId(menuTargetId(menu.target));
                    closeMenu();
                  }}
                >
                  Rename
                </MenuButton>
                <MenuButton
                  disabled={!!findFolder(nodes, menu.target.id)?.children.length}
                  onClick={() => {
                    const id = menu.target.kind === "folder" ? menu.target.id : "";
                    closeMenu();
                    void runAction(() => onDeleteFolder(id)).catch(() => undefined);
                  }}
                >
                  Delete folder
                </MenuButton>
              </>
            )}
            {menu.target.kind === "document" && (
              <>
                <MenuButton
                  onClick={() => {
                    const id = menuTargetId(menu.target);
                    if (menu.target.kind === "document" && menu.target.recent) {
                      setEditingFolderId(null);
                      setEditingDocumentId(null);
                      setEditingRecentId(id);
                    } else if (menu.target.kind === "document") {
                      setEditingFolderId(null);
                      setEditingRecentId(null);
                      setEditingDocumentId(id);
                    }
                    closeMenu();
                  }}
                >
                  Rename
                </MenuButton>
                <MenuButton
                  onClick={() => {
                    const node = findDocument(nodes, menuTargetId(menu.target));
                    closeMenu();
                    if (node && node.folder_id !== null)
                      void runAction(() => onMoveDocument(node.id, null)).catch(() => undefined);
                  }}
                >
                  Move to top level
                </MenuButton>
                <MenuButton
                  onClick={() => {
                    const id = menuTargetId(menu.target);
                    closeMenu();
                    void runAction(() => onDeleteDocument(id)).catch(() => undefined);
                  }}
                >
                  Delete note
                </MenuButton>
              </>
            )}
          </div>
        )}
      </nav>
      <DragOverlay>
        {dragLabel && (
          <div className="rounded border border-indigo-200 bg-white px-3 py-2 text-sm font-medium text-zinc-800 shadow-lg">
            {dragLabel}
          </div>
        )}
      </DragOverlay>
    </DragDropProvider>
  );
}

interface NavigationRowProps {
  node: NavigationNode;
  depth: number;
  expandedFolders: Record<string, boolean>;
  selectedId: string | null;
  creatingParent: string | null | undefined;
  folderName: string;
  editingFolderId: string | null;
  editingDocumentId: string | null;
  dragItem: DragData | null;
  allNodes: NavigationNode[];
  onSelect: (id: string) => void;
  onToggleFolder: (id: string) => void;
  onContextMenu: (event: MouseEvent, target: MenuTarget) => void;
  onKeyboardMenu: (event: KeyboardEvent, target: MenuTarget) => void;
  onFolderNameChange: (name: string) => void;
  onSubmitFolder: (event: FormEvent<HTMLFormElement>) => void;
  onCancelCreate: () => void;
  onRenameFolder: (id: string, name: string) => Promise<void>;
  onRenameDocument: (id: string, title: string) => Promise<void>;
  onDeleteDocument: (id: string) => Promise<void>;
  onStartRenameFolder: (id: string) => void;
  onStartRenameDocument: (id: string) => void;
  onCancelRename: () => void;
  onExpandFolder: (id: string) => void;
}

function NavigationRow(props: NavigationRowProps) {
  const { node } = props;
  if (node.kind === "document") return <DocumentRow {...props} node={node} />;
  return <FolderRow {...props} node={node} />;
}

function RootDropButton({
  dragItem,
  onContextMenu,
  onKeyboardMenu,
}: {
  dragItem: DragData | null;
  onContextMenu: (event: MouseEvent, target: MenuTarget) => void;
  onKeyboardMenu: (event: KeyboardEvent, target: MenuTarget) => void;
}) {
  const { ref, isDropTarget } = useDroppable({
    id: "root-target",
    data: { kind: "root" },
    disabled: !dragItem || dragItem.parentId === null,
  });
  const target: MenuTarget = { kind: "root" };
  return (
    <button
      ref={ref}
      type="button"
      data-drop-target="root"
      onContextMenu={(event) => onContextMenu(event, target)}
      onKeyDown={(event) => onKeyboardMenu(event, target)}
      className={`flex-1 rounded px-2 py-2 text-left text-sm font-medium ${isDropTarget ? "bg-indigo-100 text-indigo-800" : "text-zinc-700 hover:bg-zinc-100"}`}
    >
      All notes
      <span className="ml-2 text-xs font-normal text-zinc-400">Drop here for top level</span>
    </button>
  );
}

function RecentDocumentRow({
  document,
  selected,
  editing,
  onSelect,
  onRename,
  onStartRename,
  onCancel,
  onContextMenu,
  onKeyboardMenu,
}: {
  document: NavigationDocument;
  selected: boolean;
  editing: boolean;
  onSelect: (id: string) => void;
  onRename: (id: string, title: string) => Promise<void>;
  onStartRename: () => void;
  onCancel: () => void;
  onContextMenu: (event: MouseEvent, target: MenuTarget) => void;
  onKeyboardMenu: (event: KeyboardEvent, target: MenuTarget) => void;
}) {
  const { ref, isDragging } = useDraggable({
    id: `document:${document.id}:recent`,
    data: { kind: "document", id: document.id, parentId: document.folder_id },
    disabled: editing,
  });
  const target: MenuTarget = { kind: "document", id: document.id, recent: true };
  return (
    <li data-document-row-id={document.id} className={isDragging ? "opacity-40" : undefined}>
      {editing ? (
        <DocumentRenameForm document={document} depth={0} onSave={onRename} onCancel={onCancel} />
      ) : (
        <button
          ref={ref}
          type="button"
          data-document-id={document.id}
          data-recent-document-id={document.id}
          aria-current={selected ? "page" : undefined}
          onClick={() => onSelect(document.id)}
          onDoubleClick={onStartRename}
          onContextMenu={(event) => onContextMenu(event, target)}
          onKeyDown={(event) => onKeyboardMenu(event, target)}
          className={`w-full truncate px-3 py-2 text-left text-sm hover:bg-zinc-50 ${selected ? "bg-zinc-100" : ""}`}
        >
          {document.title}
        </button>
      )}
    </li>
  );
}

function DocumentRow(props: NavigationRowProps & { node: NavigationDocument }) {
  const { node, depth, selectedId, editingDocumentId } = props;
  const target: MenuTarget = { kind: node.kind, id: node.id };
  const { ref, isDragging } = useDraggable({
    id: `document:${node.id}:tree`,
    data: { kind: "document", id: node.id, parentId: node.folder_id },
    disabled: editingDocumentId === node.id,
  });
  const selected = selectedId === node.id;
  return (
    <li data-document-row-id={node.id} className={isDragging ? "opacity-40" : undefined}>
      {editingDocumentId === node.id ? (
        <DocumentRenameForm
          document={node}
          depth={depth}
          onSave={props.onRenameDocument}
          onCancel={props.onCancelRename}
        />
      ) : (
        <button
          ref={ref}
          type="button"
          aria-label={node.title}
          data-document-id={node.id}
          aria-current={selected ? "page" : undefined}
          onClick={() => props.onSelect(node.id)}
          onDoubleClick={() => props.onStartRenameDocument(node.id)}
          onContextMenu={(event) => props.onContextMenu(event, target)}
          onKeyDown={(event) => props.onKeyboardMenu(event, target)}
          className={`flex w-full flex-col gap-1 border-b border-zinc-100 px-3 py-2.5 text-left hover:bg-zinc-50 ${selected ? "bg-zinc-100" : ""}`}
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
      )}
    </li>
  );
}

function FolderRow(props: NavigationRowProps & { node: NavigationFolder }) {
  const {
    node,
    depth,
    expandedFolders,
    creatingParent,
    folderName,
    editingFolderId,
    dragItem,
    allNodes,
  } = props;
  const target: MenuTarget = { kind: "folder", id: node.id };
  const expanded = expandedFolders[node.id] ?? true;
  const childrenId = `folder-panel-${encodeURIComponent(node.id)}`;
  const parentId = node.parent_id;
  const invalidDrop = isInvalidFolderTarget(allNodes, dragItem, node);
  const draggable = useDraggable({
    id: `folder:${node.id}`,
    data: { kind: "folder", id: node.id, parentId },
    disabled: editingFolderId === node.id,
  });
  const droppable = useDroppable({
    id: `folder-target:${node.id}`,
    data: { kind: "folder", id: node.id },
    disabled: invalidDrop,
  });
  return (
    <li
      data-folder-id={node.id}
      data-drop-target={node.id}
      className={
        droppable.isDropTarget
          ? "rounded bg-indigo-50 ring-2 ring-inset ring-indigo-300"
          : undefined
      }
    >
      <div className="flex items-center">
        {editingFolderId === node.id ? (
          <FolderRenameForm
            folder={node}
            depth={depth}
            onSave={props.onRenameFolder}
            onCancel={props.onCancelRename}
          />
        ) : (
          <button
            type="button"
            aria-label={`Folder: ${node.name}`}
            aria-expanded={expanded}
            aria-controls={childrenId}
            onClick={() => props.onToggleFolder(node.id)}
            onDoubleClick={() => props.onStartRenameFolder(node.id)}
            onContextMenu={(event) => props.onContextMenu(event, target)}
            onKeyDown={(event) => props.onKeyboardMenu(event, target)}
            ref={(element) => {
              draggable.ref(element);
              droppable.ref(element);
            }}
            className={`flex min-w-0 flex-1 items-center gap-2 px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-zinc-500 hover:bg-zinc-50 hover:text-zinc-800 ${draggable.isDragging ? "opacity-40" : ""}`}
            style={{ paddingLeft: `${12 + depth * 16}px` }}
          >
            <span aria-hidden="true" className="w-3 text-zinc-400">
              {expanded ? "▾" : "▸"}
            </span>
            <span className="truncate">{node.name}</span>
          </button>
        )}
      </div>
      {expanded && (
        <ul id={childrenId}>
          {node.children.map((child) => (
            <NavigationRow
              key={`${child.kind}:${child.id}`}
              {...props}
              node={child}
              depth={depth + 1}
            />
          ))}
          {creatingParent === node.id && (
            <li>
              <FolderCreateForm
                name={folderName}
                destination={`in ${node.name}`}
                onNameChange={props.onFolderNameChange}
                onSubmit={props.onSubmitFolder}
                onCancel={props.onCancelCreate}
              />
            </li>
          )}
        </ul>
      )}
    </li>
  );
}

function MenuButton({
  children,
  disabled,
  onClick,
}: {
  children: string;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      role="menuitem"
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="block w-full px-3 py-2 text-left text-sm text-zinc-700 hover:bg-zinc-100 disabled:opacity-40"
    >
      {children}
    </button>
  );
}

function FolderCreateForm({
  name,
  destination,
  onNameChange,
  onSubmit,
  onCancel,
}: {
  name: string;
  destination: string;
  onNameChange: (name: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onCancel: () => void;
}) {
  return (
    <form className="flex gap-1 px-3 py-2" onSubmit={onSubmit}>
      <input
        aria-label="Folder name"
        autoFocus
        value={name}
        onChange={(event) => onNameChange(event.target.value)}
        className="min-w-0 flex-1 rounded border border-zinc-300 px-2 py-1 text-sm"
        placeholder={`New folder ${destination}`}
      />
      <button type="submit" className="rounded px-2 text-xs text-zinc-700 hover:bg-zinc-100">
        Add
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
  return (
    <InlineRenameForm
      label={`Rename ${folder.name}`}
      value={name}
      onChange={setName}
      onSubmit={() => onSave(folder.id, name.trim())}
      onCancel={onCancel}
      paddingLeft={12 + depth * 16}
    />
  );
}

function DocumentRenameForm({
  document,
  depth,
  onSave,
  onCancel,
}: {
  document: NavigationDocument;
  depth: number;
  onSave: (id: string, title: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [title, setTitle] = useState(document.title);
  return (
    <InlineRenameForm
      label={`Rename ${document.title}`}
      value={title}
      onChange={setTitle}
      onSubmit={() => onSave(document.id, title.trim())}
      onCancel={onCancel}
      paddingLeft={12 + depth * 16}
    />
  );
}

function InlineRenameForm({
  label,
  value,
  onChange,
  onSubmit,
  onCancel,
  paddingLeft,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => Promise<void>;
  onCancel: () => void;
  paddingLeft: number;
}) {
  return (
    <form
      className="flex min-w-0 flex-1 gap-1 py-1 pr-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (value.trim()) void onSubmit().catch(() => undefined);
      }}
      style={{ paddingLeft }}
    >
      <input
        aria-label={label}
        autoFocus
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            onCancel();
          }
        }}
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

function readDragData(value: unknown): DragData | null {
  if (!value || typeof value !== "object") return null;
  const data = value as Record<string, unknown>;
  if ((data.kind !== "folder" && data.kind !== "document") || typeof data.id !== "string")
    return null;
  if (data.parentId !== null && typeof data.parentId !== "string") return null;
  return { kind: data.kind, id: data.id, parentId: data.parentId };
}

function readDropData(value: unknown): DropData | null {
  if (!value || typeof value !== "object") return null;
  const data = value as Record<string, unknown>;
  if (data.kind === "root") return { kind: "root" };
  return data.kind === "folder" && typeof data.id === "string"
    ? { kind: "folder", id: data.id }
    : null;
}
function isInvalidFolderTarget(
  nodes: NavigationNode[],
  item: DragData | null,
  target: NavigationFolder,
): boolean {
  if (!item) return false;
  return !canDropIntoFolder(nodes, item, target.id);
}

export function canDropIntoFolder(
  nodes: NavigationNode[],
  item: DragData,
  targetId: string,
): boolean {
  if (item.id === targetId || item.parentId === targetId) return false;
  if (item.kind !== "folder") return true;
  const source = findFolder(nodes, item.id);
  return !!source && !isDescendantFromNodes(source, targetId);
}

function menuTargetId(target: MenuTarget): string {
  return target.kind === "root" ? "" : target.id;
}

function isDescendantFromNodes(folder: NavigationFolder, candidateId: string): boolean {
  return folder.children.some(
    (child) =>
      child.id === candidateId ||
      (child.kind === "folder" && isDescendantFromNodes(child, candidateId)),
  );
}

function collectFolderIds(nodes: NavigationNode[]): string[] {
  return nodes.flatMap((node) =>
    node.kind === "folder" ? [node.id, ...collectFolderIds(node.children)] : [],
  );
}

function findFolder(nodes: NavigationNode[], id: string): NavigationFolder | null {
  for (const node of nodes) {
    if (node.kind === "folder") {
      if (node.id === id) return node;
      const found = findFolder(node.children, id);
      if (found) return found;
    }
  }
  return null;
}

function findDocument(nodes: NavigationNode[], id: string): NavigationDocument | null {
  for (const node of nodes) {
    if (node.kind === "document" && node.id === id) return node;
    if (node.kind === "folder") {
      const found = findDocument(node.children, id);
      if (found) return found;
    }
  }
  return null;
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
