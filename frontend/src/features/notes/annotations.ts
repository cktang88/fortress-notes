import { Extension } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import type { Node as PMNode } from "@tiptap/pm/model";
import { Decoration, DecorationSet } from "@tiptap/pm/view";

export type AnnotationType = "consistency" | "stale" | "review";

export interface Annotation {
  id: string;
  /** The text in the note this annotation refers to (used to locate the range). */
  claim: string;
  type: AnnotationType;
  message: string;
  /** Replacement text offered on Accept (stale facts only). */
  suggestion?: string;
}

export interface AnnotationState {
  annotations: Annotation[];
  brokenLinks: Set<string>;
}

export const annotationKey = new PluginKey<AnnotationState>("fortress-annotations");

/** Build the doc's concatenated text plus a map from char offset -> ProseMirror pos. */
function buildPositionMap(doc: PMNode): { text: string; posOf: number[] } {
  let text = "";
  const posOf: number[] = [];
  doc.descendants((node, pos) => {
    if (node.isText && node.text) {
      for (let i = 0; i < node.text.length; i++) posOf.push(pos + i);
      text += node.text;
    }
    return true;
  });
  return { text, posOf };
}

/** Find [from, to] of the first case-insensitive match of `needle` in the doc. */
function locate(text: string, posOf: number[], needle: string): [number, number] | null {
  const trimmed = needle.trim().toLowerCase();
  if (!trimmed) return null;
  const idx = text.toLowerCase().indexOf(trimmed);
  if (idx === -1) return null;
  const from = posOf[idx];
  const to = posOf[idx + trimmed.length - 1] + 1;
  return [from, to];
}

/** Public: find the document range [from, to] for a claim string, or null. */
export function findAnnotationRange(doc: PMNode, claim: string): [number, number] | null {
  const { text, posOf } = buildPositionMap(doc);
  return locate(text, posOf, claim);
}

function computeDecorations(doc: PMNode, state: AnnotationState): DecorationSet {
  const decos: Decoration[] = [];
  const { text, posOf } = buildPositionMap(doc);

  for (const a of state.annotations) {
    const range = locate(text, posOf, a.claim);
    if (!range) continue;
    decos.push(
      Decoration.inline(range[0], range[1], {
        class: `squiggle squiggle-${a.type}`,
        "data-annotation-id": a.id,
      }),
    );
  }

  if (state.brokenLinks.size > 0) {
    doc.descendants((node, pos) => {
      if (!node.isText) return true;
      const link = node.marks.find((m) => m.type.name === "link");
      if (link && state.brokenLinks.has(link.attrs.href)) {
        decos.push(
          Decoration.inline(pos, pos + (node.text?.length ?? 0), { class: "link-broken" }),
        );
      }
      return true;
    });
  }

  return DecorationSet.create(doc, decos);
}

export const AnnotationExtension = Extension.create({
  name: "fortressAnnotations",

  addProseMirrorPlugins() {
    return [
      new Plugin<AnnotationState>({
        key: annotationKey,
        state: {
          init: () => ({ annotations: [], brokenLinks: new Set<string>() }),
          apply(tr, value) {
            const meta = tr.getMeta(annotationKey) as Partial<AnnotationState> | undefined;
            return meta ? { ...value, ...meta } : value;
          },
        },
        props: {
          decorations(state) {
            const pluginState = annotationKey.getState(state);
            if (!pluginState) return null;
            return computeDecorations(state.doc, pluginState);
          },
        },
      }),
    ];
  },
});
