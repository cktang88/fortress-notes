import { expect, test, type Page, type Route } from "@playwright/test";
import { proxyToBackend } from "./backend";
import { createNote, focusedBlockId, noteTitle, uniqueWord } from "./notes";

test.beforeEach(async ({ page }, testInfo) => proxyToBackend(page, testInfo));

const answerCard = (page: Page) => page.getByRole("region", { name: "Answer from your notes" });
const searchBox = (page: Page) => page.getByRole("searchbox", { name: "Search notes" });

function reply(status: string, extra: Record<string, unknown> = {}) {
  return (route: Route) =>
    route.fulfill({
      json: { status, answer: [], sources: [], steps: [], elapsed_ms: 1200, ...extra },
    });
}

test("each answer status explains itself, and the card follows the current question", async ({
  page,
}) => {
  await page.goto("/");
  const card = answerCard(page);
  const search = searchBox(page);

  const statuses: [string, string][] = [
    ["not_found", "Your notes don't seem to answer that."],
    ["not_configured", "Answers need an AI key"],
    ["timeout", "That took too long to answer."],
    ["unavailable", "Couldn't reach the AI service."],
  ];
  for (const [status, message] of statuses) {
    await page.unroute("**/api/ask");
    await page.route("**/api/ask", reply(status));
    await search.fill(`what about ${status}?`);
    await search.press("Control+Enter");
    await expect(card).toContainText(message);
    // Only one card, and only one status message at a time.
    await expect(card).toHaveCount(1);
    for (const [, other] of statuses.filter(([name]) => name !== status))
      await expect(card).not.toContainText(other);
  }
  // Timing is shown for real answers attempts, not the "no key" hint.
  await expect(card).toContainText("1.2 s");

  // Changing the question hides the old answer; going back to it shows it again.
  await search.fill("a different question");
  await expect(card).toHaveCount(0);
  await search.fill("what about unavailable?");
  await expect(card).toContainText("Couldn't reach the AI service.");

  // Dismissing keeps the search but drops the card.
  await card.getByRole("button", { name: "Dismiss answer" }).click();
  await expect(card).toHaveCount(0);
  await expect(search).toHaveValue("what about unavailable?");

  // A server error is reported in the card instead of failing silently.
  await page.unroute("**/api/ask");
  await page.route("**/api/ask", (route) =>
    route.fulfill({ status: 500, json: { detail: "model exploded" } }),
  );
  await page.getByRole("button", { name: "✦ Ask your notes" }).click();
  await expect(card).toContainText("model exploded");

  // Clearing the search hides the card.
  await search.press("Escape");
  await expect(card).toHaveCount(0);
});

test("a slow answer to an earlier question never replaces the current one", async ({ page }) => {
  await page.goto("/");
  const search = searchBox(page);
  const card = answerCard(page);
  let releaseFirst: () => void = () => undefined;
  const firstHeld = new Promise<void>((resolve) => (releaseFirst = resolve));
  await page.route("**/api/ask", async (route) => {
    const { question } = route.request().postDataJSON() as { question: string };
    if (question === "first question") {
      await firstHeld;
      await route.fulfill({
        json: {
          status: "answered",
          answer: [{ text: "Stale answer to the first question.", citations: [] }],
          sources: [],
        },
      });
    } else {
      await reply("not_found")(route);
    }
  });

  await search.fill("first question");
  await search.press("Control+Enter");
  await expect(card).toContainText("Reading your notes about “first question”…");

  await search.fill("second question");
  await search.press("Control+Enter");
  await expect(card).toContainText("Your notes don't seem to answer that.");
  // Let the stale reply land, then check it was ignored.
  const staleReply = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/ask") &&
      response.request().postData()?.includes("first question") === true,
  );
  releaseFirst();
  await staleReply;
  await expect(card).toContainText("Your notes don't seem to answer that.");
  await expect(card).not.toContainText("Stale answer");
  await search.fill("first question");
  await expect(card).toHaveCount(0);
});

test("each citation opens its own note at the cited paragraph", async ({ page }, testInfo) => {
  const word = uniqueWord("ask");
  const garden = await createNote(page, testInfo, {
    title: `Garden ${word}`,
    body: "Intro paragraph about the garden.\n\nTomatoes need staking in June.\n\nBasil goes next to them.",
  });
  const trip = await createNote(page, testInfo, {
    title: `Trip ${word}`,
    body: "We leave on Friday.\n\nThe neighbour waters the tomatoes while we're away.",
  });
  const staking = garden.blocks[1];
  const watering = trip.blocks[1];
  await page.route("**/api/ask", (route) =>
    route.fulfill({
      json: {
        status: "answered",
        answer: [
          {
            text: "Stake the tomatoes in June.",
            citations: [
              {
                block_id: staking.id,
                document_id: garden.id,
                document_title: garden.title,
                quote: "Tomatoes need staking",
              },
            ],
          },
          {
            text: "The neighbour waters them while you travel.",
            citations: [
              {
                block_id: watering.id,
                document_id: trip.id,
                document_title: trip.title,
                quote: "waters the tomatoes",
              },
              // The same paragraph cited twice keeps its first number.
              {
                block_id: staking.id,
                document_id: garden.id,
                document_title: garden.title,
                quote: "staking in June",
              },
            ],
          },
        ],
        sources: [],
        steps: [{ action: "grep", detail: "tomato" }],
        elapsed_ms: 900,
      },
    }),
  );

  await page.goto("/");
  await searchBox(page).fill(`tomatoes ${word}`);
  await page.getByRole("button", { name: "✦ Ask your notes" }).click();
  const card = answerCard(page);
  await expect(card).toContainText("Stake the tomatoes in June.");
  await expect(card).toContainText("Searched for “tomato”");
  await expect(card.getByRole("listitem")).toHaveText([`1. ${garden.title}`, `2. ${trip.title}`]);

  // The superscript "2" goes to the trip note's second paragraph.
  await card.getByRole("button", { name: "2", exact: true }).click();
  await expect(noteTitle(page)).toHaveValue(trip.title);
  await expect(page).toHaveURL(new RegExp(`note=${trip.id}#block=${watering.id}`));
  await expect.poll(() => focusedBlockId(page)).toBe(watering.id);

  // The card stays for the question, so the other source is one click away.
  await card.getByRole("button", { name: `1. ${garden.title}` }).click();
  await expect(noteTitle(page)).toHaveValue(garden.title);
  await expect.poll(() => focusedBlockId(page)).toBe(staking.id);
});
