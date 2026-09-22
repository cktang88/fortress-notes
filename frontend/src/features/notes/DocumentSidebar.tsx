import { useState } from "react";
import { StatusBadge } from "./StatusBadge";
import type { NoteStatus } from "./types";

export interface NavigationDocument {
  kind: "document";
  id: string;
  title: string;
  status: NoteStatus;
  updated_at: string;
}

export interface NavigationFolder {
  kind: "folder";
  id: string;
  name: string;
  children: NavigationNode[];
}

export type NavigationNode = NavigationFolder | NavigationDocument;

export interface DocumentSidebarProps {
  nodes: NavigationNode[];
  selectedId: string | null;
  loading?: boolean;
  onSelect: (id: string) => void;
}

export function DocumentSidebar({
  nodes,
  selectedId,
  loading = false,
  onSelect,
}: DocumentSidebarProps) {
  const [expandedFolders, setExpandedFolders] = useState<Record<string, boolean>>(() =>
    collectFolderIds(nodes).reduce<Record<string, boolean>>((expanded, id) => {
      expanded[id] = true;
      return expanded;
    }, {}),
  );

  function toggleFolder(id: string) {
    setExpandedFolders((current) => ({ ...current, [id]: !current[id] }));
  }

  if (loading) {
    return <div className="p-4 text-sm text-zinc-400">Loading documents…</div>;
  }

  if (nodes.length === 0) {
    return <div className="p-4 text-sm text-zinc-400">No documents.</div>;
  }

  return (
    <nav aria-label="Documents" className="flex-1 overflow-y-auto">
      <ul className="py-1" role="tree">
        {nodes.map((node) => (
          <NavigationRow
            key={node.id}
            node={node}
            depth={0}
            expandedFolders={expandedFolders}
            selectedId={selectedId}
            onSelect={onSelect}
            onToggleFolder={toggleFolder}
          />
        ))}
      </ul>
    </nav>
  );
}

interface NavigationRowProps {
  node: NavigationNode;
  depth: number;
  expandedFolders: Record<string, boolean>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onToggleFolder: (id: string) => void;
}

function NavigationRow({
  node,
  depth,
  expandedFolders,
  selectedId,
  onSelect,
  onToggleFolder,
}: NavigationRowProps) {
  if (node.kind === "document") {
    return (
      <li role="treeitem" aria-selected={selectedId === node.id}>
        <button
          type="button"
          data-document-id={node.id}
          aria-current={selectedId === node.id ? "page" : undefined}
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
      </li>
    );
  }

  const expanded = expandedFolders[node.id] ?? true;
  const childrenId = `folder-panel-${encodeURIComponent(node.id)}`;

  return (
    <li role="treeitem" aria-expanded={expanded}>
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={childrenId}
        onClick={() => onToggleFolder(node.id)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-zinc-500 hover:bg-zinc-50 hover:text-zinc-800"
        style={{ paddingLeft: `${12 + depth * 16}px` }}
      >
        <span aria-hidden="true" className="w-3 text-zinc-400">
          {expanded ? "▾" : "▸"}
        </span>
        <span className="truncate">{node.name}</span>
      </button>
      {expanded && (
        <ul id={childrenId} role="group">
          {node.children.map((child) => (
            <NavigationRow
              key={child.id}
              node={child}
              depth={depth + 1}
              expandedFolders={expandedFolders}
              selectedId={selectedId}
              onSelect={onSelect}
              onToggleFolder={onToggleFolder}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

function collectFolderIds(nodes: NavigationNode[]): string[] {
  return nodes.flatMap((node) =>
    node.kind === "folder" ? [node.id, ...collectFolderIds(node.children)] : [],
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
