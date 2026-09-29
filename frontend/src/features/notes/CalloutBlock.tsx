import { createReactBlockSpec } from "@blocknote/react";

export const calloutBlock = createReactBlockSpec(
  {
    type: "callout",
    propSchema: {},
    content: "inline",
  },
  {
    render: ({ contentRef }) => (
      <aside
        role="note"
        aria-label="Callout"
        className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-zinc-800"
      >
        <div ref={contentRef} />
      </aside>
    ),
  },
);
