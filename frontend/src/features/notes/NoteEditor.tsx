import { useEffect, useRef, useState } from "react";
import { useEditor, EditorContent } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Placeholder from "@tiptap/extension-placeholder";
import Link from "@tiptap/extension-link";
import { htmlToMarkdown, markdownToHtml } from "./markdown";
import { AnnotationExtension, annotationKey, findAnnotationRange } from "./annotations";
import type { Annotation } from "./annotations";
import { AnnotationPopup } from "./AnnotationPopup";
import { LinkSync } from "./linkSync";

interface Props {
  /** Stable per note — parent passes key={note.id} so this remounts on note change. */
  initialMarkdown: string;
  onSave: (markdown: string) => void;
  annotations: Annotation[];
  brokenLinks: string[];
  /** Called when an annotation is accepted or rejected, so the parent can dismiss it. */
  onResolve: (id: string) => void;
}

const AUTOSAVE_MS = 800;

interface ActivePopup {
  annotation: Annotation;
  x: number;
  y: number;
}

export function NoteEditor({
  initialMarkdown,
  onSave,
  annotations,
  brokenLinks,
  onResolve,
}: Props) {
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const annotationsRef = useRef(annotations);
  annotationsRef.current = annotations;
  const [popup, setPopup] = useState<ActivePopup | null>(null);

  const editor = useEditor({
    extensions: [
      StarterKit,
      Placeholder.configure({ placeholder: "Start writing…" }),
      Link.configure({
        openOnClick: true,
        autolink: true,
        HTMLAttributes: { target: "_blank", rel: "noopener noreferrer" },
      }),
      LinkSync,
      AnnotationExtension,
    ],
    content: markdownToHtml(initialMarkdown),
    editorProps: {
      attributes: { class: "tiptap prose max-w-none" },
      handleClick(view, pos) {
        // Find an annotation whose located range contains the clicked position.
        for (const annotation of annotationsRef.current) {
          const range = findAnnotationRange(view.state.doc, annotation.claim);
          if (range && pos >= range[0] && pos <= range[1]) {
            const coords = view.coordsAtPos(pos);
            setPopup({ annotation, x: coords.left, y: coords.bottom });
            return true;
          }
        }
        setPopup(null);
        return false;
      },
    },
    onUpdate: ({ editor }) => {
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => onSave(htmlToMarkdown(editor.getHTML())), AUTOSAVE_MS);
    },
    onBlur: ({ editor }) => {
      if (timer.current) clearTimeout(timer.current);
      onSave(htmlToMarkdown(editor.getHTML()));
    },
  });

  // Push annotations + broken links into the ProseMirror plugin when they change.
  useEffect(() => {
    if (!editor) return;
    editor.view.dispatch(
      editor.state.tr.setMeta(annotationKey, {
        annotations,
        brokenLinks: new Set(brokenLinks),
      }),
    );
  }, [editor, annotations, brokenLinks]);

  const resolve = (accept: boolean) => {
    if (!popup || !editor) return;
    const { annotation } = popup;
    if (accept && annotation.suggestion) {
      const range = findAnnotationRange(editor.state.doc, annotation.claim);
      if (range) {
        editor
          .chain()
          .focus()
          .insertContentAt({ from: range[0], to: range[1] }, annotation.suggestion)
          .run();
        onSave(htmlToMarkdown(editor.getHTML()));
      }
    }
    onResolve(annotation.id);
    setPopup(null);
  };

  return (
    <div className="px-8 py-6">
      <EditorContent editor={editor} />
      {popup && (
        <AnnotationPopup
          annotation={popup.annotation}
          x={popup.x}
          y={popup.y}
          onAccept={() => resolve(true)}
          onReject={() => resolve(false)}
        />
      )}
    </div>
  );
}
