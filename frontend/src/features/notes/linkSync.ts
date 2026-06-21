import { Extension } from "@tiptap/core";
import { Plugin } from "@tiptap/pm/state";
import type { Mark } from "@tiptap/pm/model";

interface LinkRun {
  from: number;
  to: number;
  href: string;
  text: string;
}

/**
 * Keeps every link's href identical to its visible text. Editing the displayed text
 * therefore also updates the underlying URL — there are no separate link "aliases".
 */
export const LinkSync = Extension.create({
  name: "linkSync",

  addProseMirrorPlugins() {
    return [
      new Plugin({
        appendTransaction(transactions, _oldState, newState) {
          if (!transactions.some((tr) => tr.docChanged)) return null;
          const linkType = newState.schema.marks.link;
          if (!linkType) return null;

          const runs: LinkRun[] = [];
          let cur: LinkRun | null = null;

          newState.doc.descendants((node, pos) => {
            const mark = node.isText && (node.marks.find((m: Mark) => m.type === linkType) ?? null);
            if (mark) {
              const href = mark.attrs.href as string;
              if (cur && cur.href === href && cur.to === pos) {
                cur.to = pos + node.nodeSize;
                cur.text += node.text ?? "";
              } else {
                if (cur) runs.push(cur);
                cur = { from: pos, to: pos + node.nodeSize, href, text: node.text ?? "" };
              }
            } else if (cur) {
              runs.push(cur);
              cur = null;
            }
            return true;
          });
          if (cur) runs.push(cur);

          let tr = newState.tr;
          let modified = false;
          for (const run of runs) {
            if (run.text && run.href !== run.text) {
              tr = tr
                .removeMark(run.from, run.to, linkType)
                .addMark(run.from, run.to, linkType.create({ href: run.text }));
              modified = true;
            }
          }
          return modified ? tr : null;
        },
      }),
    ];
  },
});
