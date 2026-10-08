import { useState } from "react";

export const CAPTURE_INPUT_ID = "quick-capture";

interface Props {
  onCapture: (text: string) => Promise<unknown>;
}

/** Jot a thought into its own note in Uncategorized without leaving what you're doing. */
export function CaptureBox({ onCapture }: Props) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    const value = text.trim();
    if (!value || busy) return;
    setBusy(true);
    try {
      await onCapture(value);
      setText("");
    } catch {
      // The caller shows the error; keep the text so nothing is lost.
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="border-b border-zinc-200 px-3 pb-3"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label htmlFor={CAPTURE_INPUT_ID} className="sr-only">
        Quick capture
      </label>
      <input
        id={CAPTURE_INPUT_ID}
        value={text}
        disabled={busy}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            setText("");
            event.currentTarget.blur();
          }
        }}
        placeholder="Jot something down… (Enter saves it)"
        title="Quick capture — ⌘/Ctrl+Shift+Space from anywhere"
        className="w-full rounded-md border border-dashed border-zinc-300 bg-zinc-50 px-3 py-1.5 text-sm placeholder:text-zinc-400 focus:border-solid focus:border-zinc-500 focus:bg-white focus:outline-none"
      />
    </form>
  );
}
