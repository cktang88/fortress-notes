import { useRef, useState } from "react";

interface Props {
  initialMarkdown: string;
  onSave: (markdown: string) => void;
}

const AUTOSAVE_MS = 800;

export function RawEditor({ initialMarkdown, onSave }: Props) {
  const [value, setValue] = useState(initialMarkdown);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleChange = (next: string) => {
    setValue(next);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => onSave(next), AUTOSAVE_MS);
  };

  return (
    <div className="px-8 py-6">
      <textarea
        value={value}
        onChange={(e) => handleChange(e.target.value)}
        onBlur={() => onSave(value)}
        spellCheck={false}
        className="min-h-[60vh] w-full resize-none font-mono text-sm leading-relaxed text-zinc-800 focus:outline-none"
      />
    </div>
  );
}
