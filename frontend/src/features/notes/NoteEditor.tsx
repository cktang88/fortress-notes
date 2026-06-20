import { useRef } from "react";
import { useEditor, EditorContent } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Placeholder from "@tiptap/extension-placeholder";
import { htmlToMarkdown, markdownToHtml } from "./markdown";

interface Props {
  /** Stable per note — parent passes key={note.id} so this remounts on note change. */
  initialMarkdown: string;
  onSave: (markdown: string) => void;
}

const AUTOSAVE_MS = 800;

export function NoteEditor({ initialMarkdown, onSave }: Props) {
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const editor = useEditor({
    extensions: [StarterKit, Placeholder.configure({ placeholder: "Start writing…" })],
    content: markdownToHtml(initialMarkdown),
    editorProps: { attributes: { class: "tiptap prose max-w-none" } },
    onUpdate: ({ editor }) => {
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        onSave(htmlToMarkdown(editor.getHTML()));
      }, AUTOSAVE_MS);
    },
    onBlur: ({ editor }) => {
      if (timer.current) clearTimeout(timer.current);
      onSave(htmlToMarkdown(editor.getHTML()));
    },
  });

  return (
    <div className="px-8 py-6">
      <EditorContent editor={editor} />
    </div>
  );
}
