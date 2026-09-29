import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { embedLoadedSameOriginImages } from "./clipboardImages";

describe("embedLoadedSameOriginImages", () => {
  afterEach(() => vi.restoreAllMocks());

  it("embeds loaded same-origin images in copied HTML", () => {
    const image = loadedImage("http://localhost:5174/media/picture.png");
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      drawImage: vi.fn(),
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue(
      "data:image/png;base64,aW1hZ2U=",
    );

    const html = embedLoadedSameOriginImages(
      '<p>Caption</p><img src="/media/picture.png" data-url="/media/picture.png">',
      [image],
      "http://localhost:5174/",
    );

    expect(html).toContain('<p>Caption</p>');
    expect(html).toContain('src="data:image/png;base64,aW1hZ2U="');
    expect(html).not.toContain("data-url");
  });

  it("leaves unloaded and cross-origin image URLs unchanged", () => {
    const unloaded = document.createElement("img");
    Object.defineProperties(unloaded, {
      complete: { value: false },
      naturalWidth: { value: 0 },
      naturalHeight: { value: 0 },
    });

    expect(embedLoadedSameOriginImages(
      '<img src="/media/picture.png"><img src="https://other.test/picture.png">',
      [unloaded],
      "http://localhost:5174/",
    )).toBeNull();
  });
});

function loadedImage(src: string): HTMLImageElement {
  const image = document.createElement("img");
  image.src = src;
  Object.defineProperties(image, {
    complete: { value: true },
    naturalWidth: { value: 1 },
    naturalHeight: { value: 1 },
  });
  return image;
}
