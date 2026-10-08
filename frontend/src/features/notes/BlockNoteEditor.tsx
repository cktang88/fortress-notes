import { forwardRef, useEffect, useEffectEvent, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Flex } from "@mantine/core";
import { mergeRefs, useFocusTrap, useFocusWithin } from "@mantine/hooks";
import { BlockNoteSchema, defaultBlockSpecs } from "@blocknote/core";
import { SideMenuExtension } from "@blocknote/core/extensions";
import type {
  Block,
  BlocksChanged,
  PartialBlock,
  BlockNoteEditor as BlockNoteEditorInstance,
} from "@blocknote/core";
import { createExtension } from "@blocknote/core";
import { SuggestionMenu, insertOrUpdateBlockForSlashMenu } from "@blocknote/core/extensions";
import {
  ComponentsContext,
  FormattingToolbarController,
  getDefaultReactSlashMenuItems,
  SuggestionMenuController,
  SideMenu,
  SideMenuController,
  useBlockNoteEditor,
  useComponentsContext,
  useCreateBlockNote,
  useEditorState,
  useExtension,
  useExtensionState,
} from "@blocknote/react";
import type { ComponentProps, DefaultReactSuggestionItem, SideMenuProps } from "@blocknote/react";
import { BlockNoteView } from "@blocknote/mantine";
import { NodeSelection, TextSelection } from "@tiptap/pm/state";
import { calloutBlock } from "./CalloutBlock";
import { mathPlaceholderBlock } from "./MathPlaceholderBlock";
import "@blocknote/core/fonts/inter.css";
import "@blocknote/mantine/style.css";
import { BlockPropertiesEditor } from "./BlockPropertiesEditor";
import { BlockReviewPanel } from "./BlockReviewPanel";
import { embedLoadedSameOriginImages } from "./clipboardImages";
import { ApiError, blockApi, notesApi } from "./api";
import { invalidateDocumentLists, queryKeys } from "./queryKeys";
import {
  asBlockTransaction,
  makePendingTransaction,
  readPendingTransactions,
  writePendingTransactions,
} from "./pendingBlockTransactions";
import {
  blockLink,
  documentLink,
  matchingLinkTargets,
  parseBlockReference,
} from "./linkSuggestions";
import { useBlockReference, useDebouncedValue, useWritingSuggestions } from "./hooks";
import { WritingSuggestions } from "./WritingSuggestions";
import type {
  BlockDocument,
  BlockLinkTarget,
  BlockNode,
  BlockOperation,
  LinkCheck,
  NoteSummary,
} from "./types";

interface Props {
  document: BlockDocument;
  focusBlockId?: string | null;
  /** Bumped for every focus request, so re-requesting the same block refocuses it. */
  focusNonce?: number;
  linkTargets: readonly NoteSummary[];
  linkChecks: readonly LinkCheck[];
  onFocusedBlockChange: (blockId: string | null) => void;
  /** Open another note (used by writing suggestions). */
  onOpenNote?: (documentId: string, blockId: string | null) => void;
}

type Location = {
  parentId: string | null;
  position: number;
  depth: number;
};

const slashMenuShortcut = createExtension(() => ({
  key: "slashMenuShortcut",
  keyboardShortcuts: {
    "Mod-/": ({ editor }) => {
      const menu = editor.getExtension(SuggestionMenu);
      if (!menu) return false;
      menu.openSuggestionMenu("/");
      return true;
    },
  },
}))();

const accessibleFormattingToolbarRoot = forwardRef<
  HTMLDivElement,
  ComponentProps["Generic"]["Toolbar"]["Root"]
>((props, ref) => {
  const { className, children, onMouseEnter, onMouseLeave, variant } = props;
  const { ref: focusRef, focused } = useFocusWithin();
  const trapRef = useFocusTrap(focused);

  return (
    <Flex
      className={className}
      ref={mergeRefs(ref, focusRef, trapRef)}
      role="group"
      aria-label="Text formatting"
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      gap={variant === "action-toolbar" ? 2 : undefined}
    >
      {children}
    </Flex>
  );
});

function AccessibleFormattingToolbarController() {
  const components = useComponentsContext();
  const editor = useBlockNoteEditor();
  const selectionIncludesImage = useEditorState({
    editor,
    selector: ({ editor: currentEditor }) => {
      const selection = currentEditor.prosemirrorState.selection;
      return selection instanceof NodeSelection && selection.node.type.name === "image";
    },
  });
  const accessibleComponents = useMemo(
    () =>
      components && {
        ...components,
        FormattingToolbar: {
          ...components.FormattingToolbar,
          Root: accessibleFormattingToolbarRoot,
        },
      },
    [components],
  );

  if (!accessibleComponents) return null;

  return (
    <ComponentsContext.Provider value={accessibleComponents}>
      <FormattingToolbarController
        floatingUIOptions={
          selectionIncludesImage ? { useFloatingOptions: { placement: "bottom-start" } } : undefined
        }
      />
    </ComponentsContext.Provider>
  );
}

function DelayedSideMenu(props: SideMenuProps) {
  const extension = useExtension(SideMenuExtension);
  const state = useExtensionState(SideMenuExtension, {
    selector: (current) =>
      current
        ? {
            blockId: current.block?.id ?? null,
            show: current.show,
            frozen: extension.menuFrozen,
          }
        : null,
  });
  const [readyBlockId, setReadyBlockId] = useState<string | null>(null);

  useEffect(() => {
    if (!state?.show || !state.blockId) {
      setReadyBlockId(null);
      return;
    }
    if (state.frozen) {
      setReadyBlockId(state.blockId);
      return;
    }
    setReadyBlockId(null);
    const timer = window.setTimeout(() => setReadyBlockId(state.blockId), 1000);
    return () => window.clearTimeout(timer);
  }, [state?.blockId, state?.frozen, state?.show]);

  const visible = Boolean(state?.show && state.blockId && state.blockId === readyBlockId);
  return (
    <div
      aria-hidden={!visible}
      inert={!visible}
      style={{
        visibility: visible ? "visible" : "hidden",
        pointerEvents: visible ? "auto" : "none",
      }}
    >
      <SideMenu {...props} />
    </div>
  );
}

const editorSchema = BlockNoteSchema.create({
  blockSpecs: {
    ...defaultBlockSpecs,
    callout: calloutBlock(),
    mathPlaceholder: mathPlaceholderBlock(),
  },
});
type AppEditor = BlockNoteEditorInstance<
  typeof editorSchema.blockSchema,
  typeof editorSchema.inlineContentSchema,
  typeof editorSchema.styleSchema
>;
type AppBlock = Block<
  typeof editorSchema.blockSchema,
  typeof editorSchema.inlineContentSchema,
  typeof editorSchema.styleSchema
>;
type AppChanges = BlocksChanged<
  typeof editorSchema.blockSchema,
  typeof editorSchema.inlineContentSchema,
  typeof editorSchema.styleSchema
>;
type AppPartialBlock = PartialBlock<
  typeof editorSchema.blockSchema,
  typeof editorSchema.inlineContentSchema,
  typeof editorSchema.styleSchema
>;

export function BlockNoteEditor({
  document: initialDocument,
  focusBlockId,
  focusNonce = 0,
  linkTargets,
  linkChecks,
  onFocusedBlockChange,
  onOpenNote,
}: Props) {
  const queryClient = useQueryClient();
  const initialContent = useMemo(
    () => initialDocument.children.map(toPartialBlock),
    [initialDocument.children],
  );
  const [editorActionError, setEditorActionError] = useState<string | null>(null);
  const editorRef = useRef<AppEditor | null>(null);
  const editor = useCreateBlockNote(
    {
      schema: editorSchema,
      domAttributes: { editor: { "aria-label": "Document content" } },
      initialContent: initialContent.length > 0 ? initialContent : undefined,
      extensions: [slashMenuShortcut],
      uploadFile: async (file, blockId) => {
        setEditorActionError(null);
        try {
          return await notesApi.uploadFile(file);
        } catch (error) {
          setEditorActionError(
            error instanceof Error
              ? `Could not upload ${file.name}: ${error.message}`
              : `Could not upload ${file.name}`,
          );
          if (blockId) {
            window.setTimeout(() => {
              const currentEditor = editorRef.current;
              const block = currentEditor?.getBlock(blockId);
              if (block && "url" in block.props && !block.props.url) {
                currentEditor?.removeBlocks([blockId]);
              }
            }, 0);
          }
          return { props: { url: "", name: file.name } };
        }
      },
    },
    [initialDocument.id],
  );
  editorRef.current = editor;
  const saveQueue = useRef(Promise.resolve());
  const blockUpdatedAt = useRef(blockUpdatedAtById(initialDocument.children));
  const documentRevision = useRef(initialDocument.revision);
  const recoveryStarted = useRef(false);
  const hydrating = useRef(true);
  /** The pending transaction still collecting keystrokes (not yet sent). */
  const openBatchId = useRef<string | null>(null);
  /** The pending transaction currently on the wire. */
  const inFlightId = useRef<string | null>(null);
  const flushTimer = useRef<number | undefined>(undefined);
  const retryDelay = useRef(0);
  const flushWaiters = useRef<(() => void)[]>([]);
  const [saving, setSaving] = useState(false);
  const [saveProblem, setSaveProblem] = useState<SaveProblem | null>(null);
  const conflicted = saveProblem?.kind === "conflict";
  const [hasBlockSelection, setHasBlockSelection] = useState(false);
  // "You wrote about this before": the paragraph being written, once typing pauses.
  const [writing, setWriting] = useState<{ id: string; text: string } | null>(null);
  const settledWriting = useDebouncedValue(writing, SUGGESTION_PAUSE_MS);
  const [suggestionsOn, setSuggestionsOn] = useState(readSuggestionsPreference);
  const [dismissedFor, setDismissedFor] = useState<string | null>(null);
  const suggestionTarget =
    settledWriting &&
    settledWriting === writing &&
    settledWriting.text.trim().length >= SUGGESTION_MIN_CHARS &&
    dismissedFor !== settledWriting.id
      ? settledWriting
      : null;
  const writingSuggestions = useWritingSuggestions(
    initialDocument.id,
    suggestionTarget?.id ?? null,
    suggestionTarget?.text ?? "",
    suggestionsOn && !!suggestionTarget,
  );
  const visibleSuggestions =
    suggestionTarget && suggestionsOn
      ? (writingSuggestions.data ?? []).filter(
          (item) =>
            item.score > 0 &&
            item.matched_block_id &&
            !suggestionTarget.text.includes(item.matched_block_id),
        )
      : [];
  const [reviewBlockIds, setReviewBlockIds] = useState<string[]>([]);
  const [preview, setPreview] = useState<{ blockId: string; left: number; top: number } | null>(
    null,
  );
  const originalLinkTitles = useRef(new WeakMap<HTMLAnchorElement, string | null>());
  const previewQuery = useBlockReference(preview?.blockId ?? null);
  useEffect(
    () => applyLinkCheckMarkers(editor.domElement ?? null, linkChecks, originalLinkTitles.current),
    [editor, linkChecks],
  );

  /**
   * Queue edits durably (localStorage) at once, then send them in one batch after
   * typing pauses. Consecutive edits to the same block collapse into one update.
   */
  const enqueueOperations = (operations: BlockOperation[], immediate = false) => {
    try {
      const pending = readPendingTransactions(initialDocument.id);
      const open = pending.at(-1);
      if (
        open &&
        open.transaction_id === openBatchId.current &&
        open.transaction_id !== inFlightId.current &&
        open.base_revision === undefined
      ) {
        open.operations = mergeOperations(open.operations, operations);
      } else {
        const created = makePendingTransaction(operations);
        pending.push(created);
        openBatchId.current = created.transaction_id;
      }
      writePendingTransactions(initialDocument.id, pending);
    } catch (error) {
      setSaveProblem({
        kind: "conflict",
        message: error instanceof Error ? error.message : "Could not keep edits on this device",
      });
      return Promise.resolve();
    }
    return scheduleFlush(immediate ? 0 : SAVE_DELAY_MS);
  };

  const scheduleFlush = (delay: number) =>
    new Promise<void>((resolve) => {
      flushWaiters.current.push(resolve);
      window.clearTimeout(flushTimer.current);
      flushTimer.current = window.setTimeout(() => void flush(), delay);
    });

  const flush = () => {
    window.clearTimeout(flushTimer.current);
    openBatchId.current = null;
    const waiters = flushWaiters.current.splice(0);
    saveQueue.current = saveQueue.current.then(async () => {
      if (readPendingTransactions(initialDocument.id).length === 0) return;
      setSaving(true);
      try {
        await drainPendingTransactions();
        retryDelay.current = 0;
        setSaveProblem((problem) => (problem?.kind === "offline" ? null : problem));
        invalidateDocumentLists(queryClient);
      } catch (error) {
        if (isRejectedEdit(error)) {
          setSaveProblem({
            kind: "conflict",
            message: "This note changed somewhere else, so your latest edits couldn't be saved.",
          });
        } else {
          // Offline or a server hiccup: keep editing, keep the edits, try again soon.
          retryDelay.current = Math.min(30_000, Math.max(2_000, retryDelay.current * 2));
          setSaveProblem({
            kind: "offline",
            message: "Can't reach the server. Edits are kept on this device and will sync.",
          });
          window.clearTimeout(flushTimer.current);
          flushTimer.current = window.setTimeout(() => void flush(), retryDelay.current);
        }
      } finally {
        setSaving(false);
        for (const resolve of waiters) resolve();
      }
    });
    return saveQueue.current;
  };

  const drainPendingTransactions = async () => {
    let pending = readPendingTransactions(initialDocument.id);
    while (pending.length > 0) {
      const head = pending[0];
      if (head.base_revision === undefined) {
        head.base_revision = documentRevision.current;
        head.operations = withExpectedUpdatedAt(head.operations, blockUpdatedAt.current);
        writePendingTransactions(initialDocument.id, pending);
      }
      inFlightId.current = head.transaction_id;
      let document: BlockDocument;
      try {
        document = await blockApi.transaction(
          initialDocument.id,
          asBlockTransaction(head, documentRevision.current),
        );
      } finally {
        inFlightId.current = null;
      }
      pending = readPendingTransactions(initialDocument.id);
      const completedIndex = pending.findIndex(
        (transaction) => transaction.transaction_id === head.transaction_id,
      );
      if (completedIndex >= 0) pending.splice(completedIndex, 1);
      writePendingTransactions(initialDocument.id, pending);
      blockUpdatedAt.current = blockUpdatedAtById(document.children);
      documentRevision.current = document.revision;
      queryClient.setQueryData(queryKeys.document(initialDocument.id), document);
    }
  };

  /** Show the server's version of the note in the editor without saving anything. */
  const showServerVersion = (document: BlockDocument) => {
    hydrating.current = true;
    editor.replaceBlocks(editor.document, document.children.map(toPartialBlock));
    hydrateLegacyBlocks(editor, document.children);
    hydrating.current = false;
    blockUpdatedAt.current = blockUpdatedAtById(document.children);
    documentRevision.current = document.revision;
    queryClient.setQueryData(queryKeys.document(initialDocument.id), document);
  };

  const loadLatestVersion = async () => {
    window.clearTimeout(flushTimer.current);
    openBatchId.current = null;
    writePendingTransactions(initialDocument.id, []);
    try {
      showServerVersion(await blockApi.get(initialDocument.id));
      setSaveProblem(null);
    } catch (error) {
      setSaveProblem({
        kind: "conflict",
        message: error instanceof Error ? error.message : "Could not load the latest version",
      });
    }
  };

  const changeHistory = (action: "undo" | "redo") => {
    // Send any edits still waiting for a pause first, so undo applies to them.
    void flush();
    saveQueue.current = saveQueue.current.then(async () => {
      setSaving(true);
      try {
        showServerVersion(await blockApi[action](initialDocument.id, documentRevision.current));
        setSaveProblem(null);
        invalidateDocumentLists(queryClient);
      } catch (error) {
        hydrating.current = false;
        setEditorActionError(error instanceof Error ? error.message : `Could not ${action}`);
      } finally {
        setSaving(false);
      }
    });
    return saveQueue.current;
  };

  const showPreview = (event: React.MouseEvent<HTMLElement>) => {
    const anchor = (event.target as HTMLElement).closest<HTMLAnchorElement>("a");
    const reference = anchor && parseBlockReference(anchor.getAttribute("href") ?? "");
    if (!anchor || !reference) return;
    const surface = event.currentTarget.getBoundingClientRect();
    const bounds = anchor.getBoundingClientRect();
    setPreview({
      blockId: reference.blockId,
      left: bounds.left - surface.left,
      top: bounds.bottom - surface.top + 6,
    });
  };

  const onChange = (changedEditor: AppEditor, context: { getChanges: () => AppChanges }) => {
    if (hydrating.current) return;
    const operations = changesToOperations(changedEditor, context.getChanges());
    if (operations.length === 0) return;
    void enqueueOperations(operations);
    const block = changedEditor.getTextCursorPosition().block;
    setWriting({ id: block.id, text: inlineText(block.content) });
  };

  // Notes imported from Markdown carry no editor content yet; parse it once per mount.
  const hydrateOnMount = useEffectEvent(() => {
    hydrateLegacyBlocks(editor, initialDocument.children);
    hydrating.current = false;
  });
  useEffect(() => hydrateOnMount(), [editor]);

  // A newer revision that didn't come from this editor (another tab, a backup restore):
  // show it, unless local edits are waiting — then the next save reports the conflict.
  const adoptOutsideChange = useEffectEvent(() => {
    if (initialDocument.revision === documentRevision.current) return;
    const waiting =
      inFlightId.current !== null || readPendingTransactions(initialDocument.id).length > 0;
    if (!waiting) showServerVersion(initialDocument);
  });
  useEffect(() => adoptOutsideChange(), [initialDocument.revision]);

  // Replay edits left over from a previous session, and send any waiting edits
  // when the note is closed so they aren't stranded on this device.
  const recoverAndFlushOnClose = useEffectEvent(() => {
    if (!recoveryStarted.current) {
      recoveryStarted.current = true;
      void flush();
    }
    return () => {
      if (readPendingTransactions(initialDocument.id).length > 0) void flush();
    };
  });
  useEffect(() => recoverAndFlushOnClose(), []);

  useEffect(() => {
    if (!focusBlockId || !editor.getBlock(focusBlockId)) return;
    const frame = requestAnimationFrame(() => {
      editor.setTextCursorPosition(focusBlockId, "start");
      editor.focus();
      const target = Array.from(
        editor.domElement?.querySelectorAll<HTMLElement>("[data-id]") ?? [],
      ).find((element) => element.dataset.id === focusBlockId);
      target?.scrollIntoView({ block: "center" });
    });
    return () => cancelAnimationFrame(frame);
  }, [editor, focusBlockId, focusNonce]);

  const getDocumentLinkItems = async (query: string): Promise<DefaultReactSuggestionItem[]> =>
    matchingLinkTargets(linkTargets, query).map((target) => ({
      key: "paragraph",
      title: target.title,
      subtext: `Document · ${target.id}`,
      group: "Link to document",
      onItemClick: () => {
        editor.insertInlineContent(documentLink(target));
        editor.getExtension(SuggestionMenu)?.closeMenu();
      },
    }));

  const getBlockLinkItems = async (query: string): Promise<DefaultReactSuggestionItem[]> => {
    const targets: BlockLinkTarget[] = await blockApi.linkTargets(query);
    return targets.map((target) => ({
      key: "paragraph",
      title: target.text || `${target.block_type} block`,
      subtext: `${target.document_title} · ${target.block_id}`,
      group: "Link to block",
      onItemClick: () => {
        editor.insertInlineContent(blockLink(target));
        editor.getExtension(SuggestionMenu)?.closeMenu();
      },
    }));
  };

  const getSlashItems = async (query: string): Promise<DefaultReactSuggestionItem[]> => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const defaults = getDefaultReactSlashMenuItems(editor).filter((item) =>
      `${item.title} ${item.subtext ?? ""}`.toLocaleLowerCase().includes(normalizedQuery),
    );
    if (["math placeholder", "math block"].some((term) => term.includes(normalizedQuery))) {
      defaults.push({
        title: "Math placeholder",
        subtext: "Insert a placeholder for a future math block",
        group: "Blocks",
        onItemClick: () => {
          insertOrUpdateBlockForSlashMenu(editor, { type: "mathPlaceholder" });
          editor.getExtension(SuggestionMenu)?.closeMenu();
        },
      });
    }
    if ("callout".includes(normalizedQuery)) {
      defaults.push({
        title: "Callout",
        subtext: "Insert a simple rich-text callout block",
        group: "Blocks",
        onItemClick: () => {
          insertOrUpdateBlockForSlashMenu(editor, { type: "callout" });
          editor.getExtension(SuggestionMenu)?.closeMenu();
        },
      });
    }
    const targets: BlockLinkTarget[] = await blockApi.linkTargets(query);
    const references = targets.map((target) => ({
      key: "paragraph",
      title: `Embed block: ${target.text || `${target.block_type} block`}`,
      subtext: `${target.document_title} · ${target.block_id}`,
      group: "Embeds",
      onItemClick: () => {
        editor.insertInlineContent(blockLink(target));
        editor.getExtension(SuggestionMenu)?.closeMenu();
      },
    }));
    return [...defaults, ...references];
  };

  const selectionActions = hasBlockSelection
    ? blockSelectionActions(editor, initialDocument.id)
    : null;
  const reviewBlockId = reviewBlockIds[0];
  const selectedBlock =
    selectionActions?.count === 1
      ? flattenNodes(initialDocument.children).find((node) => node.id === selectionActions.blockId)
      : undefined;

  return (
    <section
      className="blocknote-shell relative flex min-h-0 flex-1 flex-col"
      onCopy={(event) => {
        const clipboardHtml = event.clipboardData.getData("text/html");
        let html = clipboardHtml;
        const selection = window.getSelection();
        const selectionRange = selection?.rangeCount ? selection.getRangeAt(0) : null;
        const editorDom = editor.domElement;
        if (
          !html &&
          selection &&
          selectionRange &&
          editorDom?.contains(selectionRange.commonAncestorContainer)
        ) {
          const selectedContent = document.createElement("div");
          for (let index = 0; index < selection.rangeCount; index += 1) {
            selectedContent.append(selection.getRangeAt(index).cloneContents());
          }
          html = selectedContent.innerHTML;
        }
        if (!html) {
          const selectedBlocks = editor.getSelection()?.blocks;
          if (selectedBlocks?.length) html = editor.blocksToHTMLLossy(selectedBlocks);
        }
        if (!/<img\b/i.test(html)) return;

        const embeddedHtml = embedLoadedSameOriginImages(
          html,
          editorDom?.querySelectorAll("img") ?? [],
          window.location.href,
        );
        if (embeddedHtml) {
          event.clipboardData.setData("text/html", embeddedHtml);
          if (!event.clipboardData.getData("text/plain")) {
            event.clipboardData.setData("text/plain", selection?.toString() ?? "");
          }
          event.preventDefault();
        }
      }}
      onKeyDownCapture={(event) => {
        const target = event.target;
        if (!(target instanceof HTMLElement) || !target.closest('[contenteditable="true"]')) {
          return;
        }

        if ((!event.metaKey && !event.ctrlKey) || event.altKey) return;
        const style = event.key.toLowerCase();
        if (style !== "b" && style !== "i") return;

        syncNativeTextSelection(editor);
        event.preventDefault();
        event.stopPropagation();
        editor.toggleStyles(style === "b" ? { bold: true } : { italic: true });
      }}
      onMouseOver={showPreview}
      onClick={(event) => {
        const anchor = (event.target as HTMLElement).closest("a");
        if (anchor && parseBlockReference(anchor.getAttribute("href") ?? "")) {
          event.preventDefault();
          showPreview(event);
        }
      }}
      onMouseLeave={() => setPreview(null)}
      onFocusCapture={() => onFocusedBlockChange(editor.getTextCursorPosition().block.id)}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null))
          onFocusedBlockChange(null);
      }}
    >
      <div className="flex items-center gap-2 px-8 pt-4 text-xs text-zinc-400">
        <span className="font-medium uppercase tracking-wide text-zinc-500">
          {editor.document.length} blocks
        </span>
        <span role="status" aria-live="polite" className="flex items-center gap-2">
          {saveProblem?.kind === "offline" ? (
            <>
              <span className="text-amber-700">{saveProblem.message}</span>
              <button
                type="button"
                onClick={() => void flush()}
                className="rounded border border-amber-300 px-1.5 text-amber-800 hover:bg-amber-50"
              >
                Retry now
              </button>
            </>
          ) : saving ? (
            "Saving…"
          ) : null}
        </span>
        <span className="ml-auto flex gap-1">
          <button
            type="button"
            aria-pressed={suggestionsOn}
            title="Show related paragraphs from other notes while you write"
            onClick={() => {
              setSuggestionsOn(!suggestionsOn);
              writeSuggestionsPreference(!suggestionsOn);
            }}
            className="rounded border px-2 py-1"
          >
            Suggestions {suggestionsOn ? "on" : "off"}
          </button>
          <button
            type="button"
            className="rounded border px-2 py-1 disabled:opacity-40"
            aria-label="Undo saved change"
            disabled={saving || conflicted || !initialDocument.can_undo}
            onClick={() => void changeHistory("undo")}
          >
            Undo
          </button>
          <button
            type="button"
            className="rounded border px-2 py-1 disabled:opacity-40"
            aria-label="Redo saved change"
            disabled={saving || conflicted || !initialDocument.can_redo}
            onClick={() => void changeHistory("redo")}
          >
            Redo
          </button>
        </span>
      </div>
      {conflicted && (
        <div
          role="alert"
          className="mx-8 mt-2 flex items-center gap-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800"
        >
          <span className="flex-1">{saveProblem?.message}</span>
          <button
            type="button"
            onClick={() => void loadLatestVersion()}
            className="rounded-md bg-red-700 px-2.5 py-1 text-xs font-medium text-white hover:bg-red-600"
          >
            Load latest version
          </button>
        </div>
      )}
      {editorActionError && (
        <div role="alert" className="px-8 pt-2 text-xs text-red-600">
          {editorActionError}
        </div>
      )}
      <BlockNoteView
        editor={editor}
        slashMenu={false}
        formattingToolbar={false}
        sideMenu={false}
        editable={!conflicted}
        onChange={onChange}
        onSelectionChange={() => {
          const selection = editor.prosemirrorState.selection;
          const blockSelection = isBlockLevelSelection(selection);
          setHasBlockSelection(blockSelection);
          setReviewBlockIds(blockSelection || !selection.empty ? selectedBlockIds(editor) : []);
          if (editor.isFocused()) {
            onFocusedBlockChange(editor.getTextCursorPosition().block.id);
          }
        }}
        className="min-h-0 flex-1 overflow-y-auto px-8 pb-12 pt-2"
      >
        <AccessibleFormattingToolbarController />
        <SideMenuController sideMenu={DelayedSideMenu} />
        <SuggestionMenuController
          triggerCharacter="/"
          getItems={getSlashItems}
          minQueryLength={0}
        />
        <SuggestionMenuController
          triggerCharacter="[["
          getItems={getDocumentLinkItems}
          minQueryLength={0}
        />
        <SuggestionMenuController
          triggerCharacter="(("
          getItems={getBlockLinkItems}
          minQueryLength={1}
        />
      </BlockNoteView>
      <WritingSuggestions
        suggestions={visibleSuggestions}
        onLink={(item) => {
          if (!suggestionTarget || !item.matched_block_id) return;
          editor.setTextCursorPosition(suggestionTarget.id, "end");
          // A real link: shows the other note's title, previews the paragraph on hover,
          // and records a backlink (the href is a plain block reference).
          editor.insertInlineContent([
            " ",
            {
              type: "link",
              href: blockLink({ block_id: item.matched_block_id, text: "" }),
              content: item.note.title || "related note",
            },
          ]);
          editor.focus();
          setDismissedFor(suggestionTarget.id);
        }}
        onOpen={(documentId, blockId) => onOpenNote?.(documentId, blockId)}
        onDismiss={() => suggestionTarget && setDismissedFor(suggestionTarget.id)}
        onTurnOff={() => {
          setSuggestionsOn(false);
          writeSuggestionsPreference(false);
        }}
      />
      {selectionActions && (
        <div
          role="group"
          aria-label="Selected block actions"
          className="flex items-center gap-2 px-8 pt-2 text-xs"
        >
          {selectionActions.count === 1 && (
            <>
              <button
                type="button"
                className="rounded border px-2 py-1"
                onClick={() => {
                  setEditorActionError(null);
                  void selectionActions.copyLink().catch(() => {
                    setEditorActionError("Could not copy block link");
                  });
                }}
              >
                Copy block link
              </button>
              <button
                type="button"
                className="rounded border px-2 py-1"
                onClick={() => {
                  setEditorActionError(null);
                  void selectionActions.copyEmbed().catch(() => {
                    setEditorActionError("Could not copy block embed");
                  });
                }}
              >
                Copy block embed
              </button>
              <button
                type="button"
                className="rounded border px-2 py-1"
                onClick={() => selectionActions.insertAbove()}
              >
                Insert above
              </button>
              <label className="flex items-center gap-1">
                Convert
                <select
                  aria-label="Convert selected block"
                  className="rounded border px-2 py-1"
                  value=""
                  onChange={(event) => {
                    const type = event.target.value as ConvertibleBlockType;
                    if (type) selectionActions.convert(type);
                  }}
                >
                  <option value="" disabled>
                    To…
                  </option>
                  <option value="paragraph">Paragraph</option>
                  <option value="heading">Heading</option>
                  <option value="quote">Quote</option>
                  <option value="bulletListItem">Bulleted list</option>
                  <option value="numberedListItem">Numbered list</option>
                  <option value="checkListItem">Task list</option>
                  <option value="codeBlock">Code</option>
                </select>
              </label>
            </>
          )}
          <button
            type="button"
            className="rounded border px-2 py-1"
            aria-label="Move selected blocks up"
            onClick={() => selectionActions.move("up")}
          >
            Move up
          </button>
          <button
            type="button"
            className="rounded border px-2 py-1"
            aria-label="Move selected blocks down"
            onClick={() => selectionActions.move("down")}
          >
            Move down
          </button>
          <button
            type="button"
            className="rounded border px-2 py-1"
            onClick={selectionActions.duplicate}
          >
            Duplicate
          </button>
          <button
            type="button"
            className="rounded border px-2 py-1"
            onClick={selectionActions.remove}
          >
            Delete
          </button>
          <span className="text-zinc-400">
            Copy, cut, and paste use BlockNote’s native clipboard handling.
          </span>
        </div>
      )}
      {selectedBlock && (
        <BlockPropertiesEditor
          key={selectedBlock.id}
          blockId={selectedBlock.id}
          values={selectedBlock.user_attrs}
          saving={saving}
          onSave={(user_attrs) =>
            enqueueOperations(
              [{ operation: "set_user_attrs", block_id: selectedBlock.id, user_attrs }],
              true,
            )
          }
        />
      )}
      {reviewBlockId && (
        <BlockReviewPanel
          key={`review-${reviewBlockIds.join("-")}`}
          documentId={initialDocument.id}
          selectedBlockIds={reviewBlockIds}
        />
      )}
      {preview && (
        <div
          role="status"
          className="pointer-events-none absolute z-20 w-72 rounded-md border border-zinc-200 bg-white p-3 text-sm shadow-lg"
          style={{ left: preview.left, top: preview.top }}
        >
          {previewQuery.isLoading && <span className="text-zinc-400">Loading block…</span>}
          {!previewQuery.isLoading && previewQuery.data && (
            <>
              <div className="mb-1 text-xs text-zinc-500">{previewQuery.data.document_title}</div>
              <div className="whitespace-pre-wrap text-zinc-800">
                {previewQuery.data.text || "Empty block"}
              </div>
            </>
          )}
          {!previewQuery.isLoading && !previewQuery.data && (
            <span className="text-zinc-500">Block not found</span>
          )}
        </div>
      )}
    </section>
  );
}

type BlockSelectionEditor = Pick<
  AppEditor,
  | "getSelection"
  | "insertBlocks"
  | "removeBlocks"
  | "updateBlock"
  | "moveBlocksUp"
  | "moveBlocksDown"
>;

export type ConvertibleBlockType =
  | "paragraph"
  | "heading"
  | "quote"
  | "bulletListItem"
  | "numberedListItem"
  | "checkListItem"
  | "codeBlock";

export function convertSelectedBlock(
  editor: BlockSelectionEditor,
  type: ConvertibleBlockType,
): void {
  const block = editor.getSelection()?.blocks[0];
  if (block) editor.updateBlock(block.id, { type } as AppPartialBlock);
}

export function insertParagraphAbove(editor: BlockSelectionEditor): string | null {
  const block = editor.getSelection()?.blocks[0];
  if (!block) return null;
  return editor.insertBlocks([{ type: "paragraph" }], block.id, "before")[0]?.id ?? null;
}

export function moveSelectedBlocks(editor: BlockSelectionEditor, direction: "up" | "down"): void {
  if ((editor.getSelection()?.blocks.length ?? 0) === 0) return;
  if (direction === "up") editor.moveBlocksUp();
  else editor.moveBlocksDown();
}

export function selectedBlockIds(editor: BlockSelectionEditor): string[] {
  return (editor.getSelection()?.blocks ?? []).map((block) => block.id);
}

function cloneForInsert(block: AppBlock): AppPartialBlock {
  return {
    type: block.type,
    props: block.props,
    content: block.content,
    children: block.children.map(cloneForInsert),
  } as PartialBlock;
}

export function duplicateSelectedBlocks(editor: BlockSelectionEditor): string[] {
  const blocks = editor.getSelection()?.blocks ?? [];
  if (blocks.length === 0) return [];
  const inserted = editor.insertBlocks(
    blocks.map(cloneForInsert),
    blocks[blocks.length - 1].id,
    "after",
  );
  return inserted.map((block) => block.id);
}

export function deleteSelectedBlocks(editor: BlockSelectionEditor): string[] {
  const ids = selectedBlockIds(editor);
  if (ids.length > 0) editor.removeBlocks(ids);
  return ids;
}

function blockSelectionActions(editor: BlockSelectionEditor, documentId?: string) {
  const blocks = editor.getSelection()?.blocks ?? [];
  return {
    count: blocks.length,
    blockIds: blocks.map((block) => block.id),
    blockId: blocks.length === 1 ? blocks[0]?.id : undefined,
    copyEmbed: async () => {
      const block = blocks[0];
      if (blocks.length !== 1 || !block) return;
      await navigator.clipboard.writeText(blockLink({ block_id: block.id, text: "" }));
    },
    copyLink: async () => {
      const block = blocks[0];
      if (blocks.length !== 1 || !block || !documentId) return;
      await navigator.clipboard.writeText(
        buildBlockPermalink(window.location.href, documentId, block.id),
      );
    },
    insertAbove: () => insertParagraphAbove(editor),
    move: (direction: "up" | "down") => moveSelectedBlocks(editor, direction),
    convert: (type: ConvertibleBlockType) => convertSelectedBlock(editor, type),
    duplicate: () => duplicateSelectedBlocks(editor),
    remove: () => deleteSelectedBlocks(editor),
  };
}

export function buildBlockPermalink(baseUrl: string, documentId: string, blockId: string): string {
  const url = new URL(baseUrl);
  url.searchParams.set("note", documentId);
  url.hash = `block=${encodeURIComponent(blockId)}`;
  return url.toString();
}

export function isBlockLevelSelection(selection: { toJSON(): unknown }): boolean {
  // BlockNote returns blocks for text ranges too; its drag-handle block range
  // uses the serialized "multiple-node" selection type.
  const serialized = selection.toJSON();
  if (typeof serialized !== "object" || serialized === null || !("type" in serialized)) {
    return false;
  }
  return serialized.type === "node" || serialized.type === "multiple-node";
}

function syncNativeTextSelection(editor: AppEditor): void {
  const selection = window.getSelection();
  const view = editor.prosemirrorView;
  const anchorNode = selection?.anchorNode;
  const focusNode = selection?.focusNode;
  if (
    !selection ||
    selection.isCollapsed ||
    !anchorNode ||
    !focusNode ||
    !view.dom.contains(anchorNode) ||
    !view.dom.contains(focusNode)
  ) {
    return;
  }

  const anchor = view.posAtDOM(anchorNode, selection.anchorOffset);
  const head = view.posAtDOM(focusNode, selection.focusOffset);
  const state = editor.prosemirrorState;
  if (
    anchor < 0 ||
    head < 0 ||
    anchor > state.doc.content.size ||
    head > state.doc.content.size ||
    !state.doc.resolve(anchor).parent.inlineContent ||
    !state.doc.resolve(head).parent.inlineContent ||
    (state.selection.anchor === anchor && state.selection.head === head)
  ) {
    return;
  }

  const textSelection = TextSelection.create(state.doc, anchor, head);
  editor.transact((tr) => tr.setSelection(textSelection));
}

function applyLinkCheckMarkers(
  root: HTMLElement | null,
  links: readonly LinkCheck[],
  originalTitles: WeakMap<HTMLAnchorElement, string | null>,
) {
  if (!root) return;
  const statusByUrl = new Map(
    links.filter((link) => link.status !== "ok").map((link) => [link.url, link]),
  );
  const update = () => {
    for (const anchor of root.querySelectorAll<HTMLAnchorElement>("a[href]")) {
      const checked =
        statusByUrl.get(anchor.href) ?? statusByUrl.get(anchor.getAttribute("href") ?? "");
      const blockId = anchor.closest<HTMLElement>("[data-id]")?.dataset.id;
      const inCheckedBlock = !!checked && checked.block_ids.includes(blockId ?? "");
      if (checked && inCheckedBlock) {
        if (!originalTitles.has(anchor)) originalTitles.set(anchor, anchor.getAttribute("title"));
        anchor.dataset.linkStatus = checked.status;
        anchor.title = `Link check: ${checked.status}`;
      } else if (anchor.dataset.linkStatus) {
        delete anchor.dataset.linkStatus;
        const oldTitle = originalTitles.get(anchor);
        if (oldTitle) anchor.setAttribute("title", oldTitle);
        else anchor.removeAttribute("title");
      }
    }
  };
  update();
  const observer = new MutationObserver(update);
  observer.observe(root, {
    childList: true,
    characterData: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["href"],
  });
  return () => observer.disconnect();
}

export function toPartialBlock(node: BlockNode): AppPartialBlock {
  const storedContent = node.content.blocknote;
  const content = storedContent === undefined ? markdownText(node) : storedContent;
  const children = node.children.map(toPartialBlock);

  if (node.type === "heading") {
    const match = markdownText(node).match(/^\s*(#{1,6})\s+/);
    return {
      id: node.id,
      type: "heading",
      props: { level: Number(node.attrs.level ?? match?.[1].length ?? 1) },
      content: match ? markdownText(node).slice(match[0].length) : content,
      children,
    } as PartialBlock;
  }
  if (node.type === "list") {
    const checked = node.attrs.checked;
    const type =
      typeof checked === "boolean"
        ? "checkListItem"
        : node.attrs.ordered === true
          ? "numberedListItem"
          : "bulletListItem";
    return {
      id: node.id,
      type,
      props: typeof checked === "boolean" ? { checked } : undefined,
      content: markdownText(node)
        .split("\n")
        .map((line) => line.replace(/^\s*(?:[-+*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?/, ""))
        .join("\n"),
      children,
    } as PartialBlock;
  }
  if (node.type === "quote") {
    return {
      id: node.id,
      type: "quote",
      content: markdownText(node)
        .split("\n")
        .map((line) => line.replace(/^\s*>\s?/, ""))
        .join("\n"),
      children,
    } as PartialBlock;
  }
  if (node.type === "code") {
    return {
      id: node.id,
      type: "codeBlock",
      content: markdownText(node)
        .replace(/^\s*(```|~~~)[^\n]*\n?/, "")
        .replace(/\n\s*(```|~~~)\s*$/, ""),
      children,
    } as PartialBlock;
  }
  if (node.type === "thematic_break") {
    return { id: node.id, type: "divider", children } as PartialBlock;
  }
  if (node.type === "mathPlaceholder") {
    return { id: node.id, type: "mathPlaceholder", children } as unknown as PartialBlock;
  }
  if (node.type === "callout") {
    return { id: node.id, type: "callout", content, children } as unknown as PartialBlock;
  }
  if (["image", "file", "audio", "video"].includes(node.type)) {
    const props = Object.fromEntries(Object.entries(node.attrs).filter(([key]) => key !== "id"));
    return { id: node.id, type: node.type, props, children } as PartialBlock;
  }
  return { id: node.id, type: "paragraph", content, children } as PartialBlock;
}

function markdownText(node: BlockNode): string {
  return String(node.content.markdown ?? node.text ?? "");
}

function flattenNodes(nodes: readonly BlockNode[]): BlockNode[] {
  return nodes.flatMap((node) => [node, ...flattenNodes(node.children)]);
}

function changesToOperations(editor: AppEditor, changes: AppChanges): BlockOperation[] {
  const locations = new Map<string, Location>();
  indexLocations(editor.document, locations);
  const operations: BlockOperation[] = [];

  for (const change of changes) {
    if (change.type === "delete") {
      if (!hasDeletedAncestor(change.block.id, changes)) {
        operations.push({ operation: "delete", block_id: change.block.id });
      }
      continue;
    }

    const location = locations.get(change.block.id);
    if (!location) continue;

    if (change.type === "insert") {
      operations.push(toOperation(editor, change.block, "insert", location));
    } else if (change.type === "move") {
      operations.push({
        operation: "move",
        block_id: change.block.id,
        parent_id: location.parentId,
        position: location.position,
      });
      operations.push(toOperation(editor, change.block, "update", location));
    } else {
      operations.push(toOperation(editor, change.block, "update", location));
    }
  }

  return operations;
}

function toOperation(
  editor: AppEditor,
  block: AppBlock,
  operation: "insert" | "update",
  location: Location,
): BlockOperation {
  return {
    operation,
    block_id: block.id,
    parent_id: operation === "insert" ? location.parentId : undefined,
    position: operation === "insert" ? location.position : undefined,
    type: block.type,
    attrs: block.props,
    content: {
      blocknote: block.content,
      markdown:
        block.type === "mathPlaceholder"
          ? "Math placeholder"
          : editor.blocksToMarkdownLossy([block]),
    },
    text: block.type === "mathPlaceholder" ? "Math placeholder" : inlineText(block.content),
  };
}

function indexLocations(blocks: readonly AppBlock[], output: Map<string, Location>): void {
  const visit = (children: readonly AppBlock[], parentId: string | null, depth: number) => {
    children.forEach((block, position) => {
      output.set(block.id, { parentId, position, depth });
      visit(block.children, block.id, depth + 1);
    });
  };
  visit(blocks, null, 0);
}

function inlineText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((item) => {
      if (typeof item === "string") return item;
      if (!item || typeof item !== "object") return "";
      const value = item as { text?: unknown; content?: unknown };
      if (typeof value.text === "string") return value.text;
      return inlineText(value.content);
    })
    .join("");
}

function hasDeletedAncestor(id: string, changes: AppChanges): boolean {
  const block = changes.find((change) => change.block.id === id)?.block;
  if (!block) return false;
  return changes.some(
    (change) =>
      change.type === "delete" && change.block.id !== id && containsDescendant(change.block, id),
  );
}

function containsDescendant(block: AppBlock, id: string): boolean {
  return block.children.some((child) => child.id === id || containsDescendant(child, id));
}

export function withExpectedUpdatedAt(
  operations: readonly BlockOperation[],
  updatedAtByBlock: ReadonlyMap<string, string>,
): BlockOperation[] {
  const checkedBlockIds = new Set<string>();

  return operations.map((operation) => {
    if (
      operation.operation === "insert" ||
      !operation.block_id ||
      checkedBlockIds.has(operation.block_id)
    ) {
      return operation;
    }

    checkedBlockIds.add(operation.block_id);
    const expectedUpdatedAt = updatedAtByBlock.get(operation.block_id);
    return expectedUpdatedAt === undefined
      ? operation
      : { ...operation, expected_updated_at: expectedUpdatedAt };
  });
}

export function transactionPayload(baseRevision: number, operations: BlockOperation[]) {
  return { base_revision: baseRevision, operations };
}

function blockUpdatedAtById(nodes: readonly BlockNode[]): Map<string, string> {
  return new Map(flattenNodes(nodes).map((node) => [node.id, node.updated_at]));
}

const SAVE_DELAY_MS = 400;

type SaveProblem = { kind: "offline" | "conflict"; message: string };

/** A 4xx (other than timeouts/rate limits) means the server refused the edit for good. */
function isRejectedEdit(error: unknown) {
  return (
    error instanceof ApiError &&
    error.status >= 400 &&
    error.status < 500 &&
    error.status !== 408 &&
    error.status !== 429
  );
}

/**
 * Append operations to a batch, collapsing a run of updates to one block into the
 * latest update (fields from later edits win).
 */
export function mergeOperations(batch: BlockOperation[], next: BlockOperation[]): BlockOperation[] {
  const merged = [...batch];
  for (const operation of next) {
    const last = merged.at(-1);
    if (
      operation.operation === "update" &&
      last?.operation === "update" &&
      last.block_id === operation.block_id
    ) {
      merged[merged.length - 1] = { ...last, ...operation };
    } else {
      merged.push(operation);
    }
  }
  return merged;
}

/** Parse Markdown-only blocks (imported notes) into rich editor content. */
function hydrateLegacyBlocks(editor: AppEditor, nodes: readonly BlockNode[]) {
  editor.transact(() => {
    for (const node of flattenNodes(nodes)) {
      if (node.content.blocknote !== undefined) continue;
      const block = editor.getBlock(node.id);
      if (!block || block.type === "divider") continue;
      const parsed = editor.tryParseMarkdownToBlocks(markdownText(node));
      if (parsed[0]?.content !== undefined) {
        editor.updateBlock(block.id, { content: parsed[0].content } as PartialBlock);
      }
    }
  });
}

const SUGGESTION_PAUSE_MS = 1200;
const SUGGESTION_MIN_CHARS = 25;
const SUGGESTIONS_KEY = "fortress-notes:writing-suggestions";

function readSuggestionsPreference(): boolean {
  try {
    return window.localStorage.getItem(SUGGESTIONS_KEY) !== "off";
  } catch {
    return true;
  }
}

function writeSuggestionsPreference(on: boolean) {
  try {
    window.localStorage.setItem(SUGGESTIONS_KEY, on ? "on" : "off");
  } catch {
    // Without storage the choice simply lasts for this visit.
  }
}
