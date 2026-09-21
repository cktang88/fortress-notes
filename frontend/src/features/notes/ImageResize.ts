import Image from "@tiptap/extension-image";

/**
 * Image node with a drag-to-resize handle. Width is stored as a `width` attribute and
 * rendered onto the <img>, so (with turndown's keep(['img'])) it survives the Markdown
 * round-trip as raw HTML.
 */
export const ImageResize = Image.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      width: {
        default: null,
        parseHTML: (el) => el.getAttribute("width"),
        renderHTML: (attrs) => (attrs.width ? { width: attrs.width } : {}),
      },
    };
  },

  addNodeView() {
    return ({ node, editor, getPos }) => {
      const dom = document.createElement("span");
      dom.className = "img-resize-wrapper";

      const img = document.createElement("img");
      img.src = node.attrs.src;
      if (node.attrs.alt) img.alt = node.attrs.alt;
      if (node.attrs.width) img.setAttribute("width", String(node.attrs.width));
      dom.appendChild(img);

      const handle = document.createElement("span");
      handle.className = "img-resize-handle";
      dom.appendChild(handle);

      handle.addEventListener("mousedown", (event) => {
        event.preventDefault();
        const startX = event.clientX;
        const startWidth = img.offsetWidth;

        const onMove = (e: MouseEvent) => {
          const width = Math.max(40, startWidth + e.clientX - startX);
          img.setAttribute("width", String(width));
        };
        const onUp = () => {
          document.removeEventListener("mousemove", onMove);
          document.removeEventListener("mouseup", onUp);
          const width = Number(img.getAttribute("width")) || null;
          if (typeof getPos === "function") {
            const pos = getPos();
            if (typeof pos !== "number") return;
            const { view } = editor;
            view.dispatch(view.state.tr.setNodeMarkup(pos, undefined, { ...node.attrs, width }));
          }
        };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
      });

      return { dom };
    };
  },
});
