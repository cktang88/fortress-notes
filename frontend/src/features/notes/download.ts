/**
 * Start a browser download for a same-origin URL. Without a filename the
 * server's Content-Disposition name is used.
 */
export function triggerDownload(url: string, filename?: string) {
  const link = document.createElement("a");
  link.href = url;
  if (filename) link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
}
