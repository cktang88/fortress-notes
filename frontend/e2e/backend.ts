import type { Page, TestInfo } from "@playwright/test";

/**
 * Send the app's /api and /media requests to the test backend. Requests are
 * fetched from Node and fulfilled, which works in every Chromium build
 * (rewriting the URL in the browser is blocked by some builds).
 */
export async function proxyToBackend(page: Page, testInfo: TestInfo) {
  const backendUrl = String(testInfo.config.metadata.backendUrl);
  for (const pattern of ["**/api/**", "**/media/**"]) {
    await page.route(pattern, async (route) => {
      const url = new URL(route.request().url());
      try {
        const response = await route.fetch({ url: `${backendUrl}${url.pathname}${url.search}` });
        await route.fulfill({ response });
      } catch (error) {
        // The page navigated, closed, or a test re-routed this request mid-flight
        // (e.g. simulating going offline); there is nothing left to answer.
        if (!/disposed|closed|already handled/i.test(String(error))) throw error;
      }
    });
  }
}

interface NavNode {
  kind: "folder" | "document";
  id: string;
  children?: NavNode[];
}

/**
 * Empty the shared test workspace through the normal API (trash and purge every
 * note, then delete folders deepest first). For specs whose pointer gestures
 * depend on the sidebar's layout.
 */
export async function resetWorkspace(page: Page, testInfo: TestInfo) {
  const api = `${String(testInfo.config.metadata.backendUrl)}/api`;
  const navigation = (await (await page.request.get(`${api}/navigation`)).json()) as {
    items: NavNode[];
  };
  const folders: { id: string; depth: number }[] = [];
  const visit = async (nodes: NavNode[], depth: number) => {
    for (const node of nodes) {
      if (node.kind === "document") await page.request.delete(`${api}/notes/${node.id}`);
      else {
        folders.push({ id: node.id, depth });
        await visit(node.children ?? [], depth + 1);
      }
    }
  };
  await visit(navigation.items, 0);
  await page.request.delete(`${api}/trash`);
  for (const folder of folders.sort((a, b) => b.depth - a.depth)) {
    await page.request.delete(`${api}/folders/${folder.id}`);
  }
}
