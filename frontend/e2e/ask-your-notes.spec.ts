import { expect, test } from "@playwright/test";
import { proxyToBackend } from "./backend";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

test("asks a question and jumps to the cited paragraph", async ({ page }, testInfo) => {
  const backendUrl = String(testInfo.config.metadata.backendUrl);
  const created = await (
    await page.request.post(`${backendUrl}/api/notes`, {
      data: { title: "Kyoto plans", body: "We booked the ryokan in Gion for two nights." },
    })
  ).json();
  const blockId = (
    await (await page.request.get(`${backendUrl}/api/block-documents/${created.id}`)).json()
  ).children[0].id;

  await page.goto("/");
  const search = page.getByRole("searchbox", { name: "Search notes" });
  await search.fill("where are we staying in Kyoto?");

  // Without an AI key the app says so instead of failing.
  await search.press("Control+Enter");
  const answer = page.getByRole("region", { name: "Answer from your notes" });
  await expect(answer).toContainText("Answers need an AI key");

  // With a model available, each sentence links to the paragraph it came from.
  await page.route("**/api/ask", (route) =>
    route.fulfill({
      json: {
        status: "answered",
        answer: [
          {
            text: "You booked a ryokan in Gion for two nights.",
            citations: [
              {
                block_id: blockId,
                document_id: created.id,
                document_title: "Kyoto plans",
                quote: "booked the ryokan in Gion",
              },
            ],
          },
        ],
        sources: [],
      },
    }),
  );
  await page.getByRole("button", { name: "✦ Ask your notes" }).click();
  await expect(answer).toContainText("You booked a ryokan in Gion for two nights.");
  await answer.getByRole("button", { name: "1. Kyoto plans" }).click();
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue("Kyoto plans");
  await expect(page).toHaveURL(new RegExp(`block=${blockId}`));
});

test("shows a streamed answer and its trace", async ({ page }, testInfo) => {
  const backendUrl = String(testInfo.config.metadata.backendUrl);
  const created = await (
    await page.request.post(`${backendUrl}/api/notes`, {
      data: { title: "Porto flat", body: "The wifi password is sardinha2024." },
    })
  ).json();
  const blockId = (
    await (await page.request.get(`${backendUrl}/api/block-documents/${created.id}`)).json()
  ).children[0].id;
  const sentence = {
    text: "The wifi password is sardinha2024.",
    citations: [
      {
        block_id: blockId,
        document_id: created.id,
        document_title: "Porto flat",
        quote: "wifi password is sardinha2024",
      },
    ],
  };
  const steps = [{ action: "grep", detail: "wifi|password" }];
  await page.route("**/api/ask", (route) => {
    expect(route.request().headers()["accept"]).toContain("application/x-ndjson");
    const events = [
      { type: "step", step: steps[0] },
      { type: "sentence", sentence },
      {
        type: "done",
        result: { status: "answered", answer: [sentence], sources: [], steps, elapsed_ms: 900 },
      },
    ];
    return route.fulfill({
      contentType: "application/x-ndjson",
      body: events.map((event) => JSON.stringify(event)).join("\n"),
    });
  });

  await page.goto("/");
  const search = page.getByRole("searchbox", { name: "Search notes" });
  await search.fill("what is the wifi password in porto?");
  await search.press("Control+Enter");
  const answer = page.getByRole("region", { name: "Answer from your notes" });
  await expect(answer).toContainText("The wifi password is sardinha2024.");
  await expect(answer).toContainText("Searched for “wifi|password” · 0.9 s");
  await answer.getByRole("button", { name: "1. Porto flat" }).click();
  await expect(page.getByRole("textbox", { name: "Note title" })).toHaveValue("Porto flat");
});
