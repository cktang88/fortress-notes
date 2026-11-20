"""Throttling (fake clock), retries/backoff, token handling, pagination guards."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fortress_onenote_graph.graph import MAX_429_RETRIES, PAGE_SIZE, GraphClient, GraphError
from fortress_onenote_graph.throttle import BACKOFF_FIRST_S, Throttle

from .fakes import FakeClock, FakeGraph


def client(graph, clock=None, **kwargs):
    clock = clock or FakeClock()
    refreshes = []

    def token(force):
        refreshes.append(force)
        return "token-2" if force else "token-1"
    made = GraphClient(token, Throttle(clock=clock, **kwargs), transport=graph)
    made.refreshes = refreshes
    return made, clock


class ThrottleTests(unittest.TestCase):
    def test_minute_and_hour_budgets(self):
        clock = FakeClock()
        throttle = Throttle(clock=clock)
        start = clock.t
        for _ in range(110):
            throttle.acquire()
        self.assertEqual(clock.sleeps, [])  # a burst of 110 is fine
        throttle.acquire()
        self.assertAlmostEqual(clock.t - start, 60.0, places=3)  # the 111th waits for the window
        for _ in range(380 - 111):
            throttle.acquire()
        before = clock.t
        throttle.acquire()  # the 381st request in an hour waits for the hour window
        self.assertGreaterEqual(clock.t - start, 3600.0)
        self.assertGreater(clock.t, before)
        # Never more than 380 in any hour, 110 in any minute.
        sent = list(throttle._sent)
        self.assertLessEqual(max(sum(1 for t in sent if s <= t < s + 3600) for s in sent), 380)
        self.assertLessEqual(max(sum(1 for t in sent if s <= t < s + 60) for s in sent), 110)

    def test_backoff_doubles_and_decays(self):
        clock = FakeClock()
        throttle = Throttle(clock=clock)
        self.assertEqual(throttle.throttled(), BACKOFF_FIRST_S)
        self.assertEqual(throttle.throttled(), BACKOFF_FIRST_S * 2)
        self.assertEqual(throttle.throttled(), BACKOFF_FIRST_S * 4)
        self.assertGreater(throttle.spacing, 1.0)
        start = clock.t
        throttle.acquire()
        self.assertAlmostEqual(clock.t - start, BACKOFF_FIRST_S * 4, places=3)
        spacing = throttle.spacing
        throttle.success()
        self.assertLess(throttle.spacing, spacing)
        self.assertEqual(throttle.throttled(), BACKOFF_FIRST_S)  # reset by the success
        self.assertEqual(throttle.throttled(retry_after=5), 5)  # Retry-After honoured

    def test_limit_percentage_header_slows_down(self):
        throttle = Throttle(clock=FakeClock())
        throttle.success(limit_percentage=95)
        self.assertGreater(throttle.spacing, 0)

    def test_state_is_shared_by_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            path = Path(directory) / "throttle.json"
            first = Throttle(clock=clock, state_path=path)
            for _ in range(380):
                first.acquire()
            clock.t += 120
            second = Throttle(clock=clock, state_path=path)  # a new run within the hour
            wait, reason = second.delay()
            self.assertGreater(wait, 3000)
            self.assertIn("hourly", reason)


class GraphClientTests(unittest.TestCase):
    def test_429_pauses_then_succeeds(self):
        graph = FakeGraph()
        graph.fail.append(["/me", 429, 2])
        made, clock = client(graph)
        self.assertEqual(made.me()["id"], "user-1")
        self.assertEqual(graph.count("/me"), 3)
        self.assertGreaterEqual(clock.t - 1_700_000_000.0, BACKOFF_FIRST_S * 3)

    def test_endless_429_stops_resumably(self):
        graph = FakeGraph()
        graph.fail.append(["/me", 429, 1000])
        made, _ = client(graph)
        with self.assertRaises(GraphError) as caught:
            made.me()
        self.assertEqual(caught.exception.code, "throttled")
        self.assertEqual(graph.count("/me"), MAX_429_RETRIES + 1)

    def test_server_errors_and_network_errors_retry(self):
        graph = FakeGraph()
        graph.fail += [["/me", 503, 2], ["/me", "raise", 1]]
        made, _ = client(graph)
        self.assertEqual(made.me()["id"], "user-1")

    def test_401_refreshes_the_token_once(self):
        graph = FakeGraph()
        graph.fail.append(["/me", 401, 1])
        made, _ = client(graph)
        made.me()
        self.assertEqual(graph.tokens, ["Bearer token-1", "Bearer token-2"])
        graph.fail.append(["/me", 401, 5])
        with self.assertRaises(GraphError):
            made.me()

    def test_client_errors_are_not_retried(self):
        graph = FakeGraph()
        made, _ = client(graph)
        with self.assertRaises(GraphError) as caught:
            made.section_pages("missing-section")
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(len(graph.log), 1)

    def test_token_is_never_sent_outside_graph(self):
        graph = FakeGraph()
        made, _ = client(graph)
        for url in ("https://evil.example.com/resources/x/$value", "http://graph.microsoft.com/v1.0/me",
                    "https://graph.microsoft.com.evil.example/x", "https://user@graph.microsoft.com/x"):
            with self.assertRaises(GraphError):
                made.resource(url)
        self.assertEqual(graph.log, [])


def pages(n, prefix="p"):
    return [{"id": f"{prefix}{i}", "title": f"Page {i}", "order": i, "level": 0,
             "lastModifiedDateTime": "2025-01-01T00:00:00Z"} for i in range(n)]


class PaginationTests(unittest.TestCase):
    def test_skip_pagination_in_order(self):
        graph = FakeGraph()
        graph.pages["s"] = pages(250)
        made, _ = client(graph)
        listed = made.section_pages("s")
        self.assertEqual([page["id"] for page in listed], [f"p{i}" for i in range(250)])
        self.assertEqual(graph.count("/pages?"), 3)
        self.assertIn("pagelevel=true", graph.log[0])
        self.assertIn(f"$top={PAGE_SIZE}", graph.log[0])
        self.assertIn("$skip=200", graph.log[2])

    def test_exact_multiple_of_page_size_stops_on_empty_batch(self):
        graph = FakeGraph()
        graph.pages["s"] = pages(200)
        made, _ = client(graph)
        self.assertEqual(len(made.section_pages("s")), 200)
        self.assertEqual(graph.count("/pages?"), 3)

    def test_graph_ignoring_skip_does_not_loop(self):
        graph = FakeGraph()
        graph.pages["s"] = pages(150)
        graph.ignore_skip = True
        made, _ = client(graph)
        listed = made.section_pages("s")
        self.assertEqual(len(listed), 100)
        self.assertEqual(graph.count("/pages?"), 2)

    def test_pages_sorted_by_order(self):
        graph = FakeGraph()
        graph.pages["s"] = list(reversed(pages(3)))
        made, _ = client(graph)
        self.assertEqual([page["id"] for page in made.section_pages("s")], ["p0", "p1", "p2"])


if __name__ == "__main__":
    unittest.main()
