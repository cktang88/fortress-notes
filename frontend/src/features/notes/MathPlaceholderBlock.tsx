import { createReactBlockSpec } from "@blocknote/react";

export const mathPlaceholderBlock = createReactBlockSpec(
  {
    type: "mathPlaceholder",
    propSchema: {},
    content: "none",
  },
  {
    render: () => (
      <div
        role="note"
        aria-label="Math placeholder"
        className="rounded border border-dashed border-zinc-300 bg-zinc-50 px-3 py-2 text-sm text-zinc-500"
      >
        Math block placeholder
      </div>
    ),
  },
);
