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
        // The page navigated or closed, the test ended or timed out, or a test re-routed this request
        // mid-flight (e.g. simulating going offline); there is nothing left to answer.
        if (!/disposed|closed|already handled|test ended|test timeout/i.test(String(error))) throw error;
      }
    });
  }
}
