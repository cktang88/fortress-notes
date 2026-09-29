/**
 * Rewrites copied image URLs to data URLs when the matching app image is already
 * loaded. That lets other apps receive the image bytes in the HTML clipboard.
 */
export function embedLoadedSameOriginImages(
  html: string,
  renderedImages: Iterable<HTMLImageElement>,
  baseUrl: string,
): string | null {
  const renderedByUrl = new Map<string, HTMLImageElement>();
  for (const image of renderedImages) {
    if (!image.complete || image.naturalWidth === 0 || image.naturalHeight === 0) continue;
    const url = resolveSameOriginUrl(image.currentSrc || image.src, baseUrl);
    if (url) renderedByUrl.set(url, image);
  }
  const parsed = new DOMParser().parseFromString(html, "text/html");
  let changed = false;

  for (const copiedImage of parsed.querySelectorAll<HTMLImageElement>("img[src]")) {
    const url = resolveSameOriginUrl(copiedImage.getAttribute("src") ?? "", baseUrl);
    const renderedImage = url ? renderedByUrl.get(url) : undefined;
    if (!renderedImage) continue;

    const dataUrl = renderImageDataUrl(renderedImage);
    if (!dataUrl) continue;

    copiedImage.src = dataUrl;
    copiedImage.removeAttribute("data-url");
    changed = true;
  }

  return changed ? parsed.body.innerHTML : null;
}

function resolveSameOriginUrl(value: string, baseUrl: string): string | null {
  try {
    const resolved = new URL(value, baseUrl);
    return resolved.origin === new URL(baseUrl).origin ? resolved.href : null;
  } catch {
    return null;
  }
}

function renderImageDataUrl(image: HTMLImageElement): string | null {
  try {
    const canvas = document.createElement("canvas");
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
    const context = canvas.getContext("2d");
    if (!context) return null;
    context.drawImage(image, 0, 0);
    return canvas.toDataURL("image/png");
  } catch {
    return null;
  }
}
