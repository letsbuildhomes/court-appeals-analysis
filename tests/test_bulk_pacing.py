"""Offline checks for the full-archive request policy."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import extract


class CountingLimiter:
    def __init__(self) -> None:
        self.calls = 0

    def wait(self) -> None:
        self.calls += 1


class BulkPacingTests(unittest.TestCase):
    def test_bulk_limiter_waits_between_request_starts(self) -> None:
        limiter = extract.RateLimiter(1.5, 0.5)
        with (
            patch.object(extract.time, "monotonic", side_effect=[100.0, 100.0, 102.0]),
            patch.object(extract.time, "sleep") as sleep,
            patch.object(extract.random, "uniform", return_value=0.5),
        ):
            limiter.wait()
            limiter.wait()
        sleep.assert_called_once_with(2.0)

    def test_redirect_is_a_separately_paced_request(self) -> None:
        urls = []

        def respond(request: httpx.Request) -> httpx.Response:
            urls.append(str(request.url))
            if request.url.path == "/media/123":
                return httpx.Response(302, headers={"location": "/files/123.pdf"})
            return httpx.Response(200, content=b"%PDF-1.4\nexample")

        limiter = CountingLimiter()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (
                httpx.Client(transport=httpx.MockTransport(respond)) as client,
                patch.object(extract, "ROOT", root),
                patch.object(extract, "PDF_DIR", root / "in" / "pdfs"),
                patch.object(extract, "cached_pdf_is_valid", return_value=False),
                patch.object(extract, "record_download") as record,
            ):
                result = extract.fetch_pdf(
                    None, client, limiter, "https://www.vtcourts.gov/media/123", refetch=False
                )
                self.assertEqual(result, "downloaded")
                self.assertEqual(limiter.calls, 2)
                self.assertEqual(len(urls), 2)
                self.assertEqual(record.call_args.kwargs["status"], "downloaded")
                self.assertTrue((root / record.call_args.kwargs["raw_path"]).is_file())

    def test_repeated_server_busy_status_stops(self) -> None:
        for status in (429, 503):
            with self.subTest(status=status):
                self.assert_repeated_status_stops(status)

    def assert_repeated_status_stops(self, status: int) -> None:
        calls = []

        def respond(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(status)

        with tempfile.TemporaryDirectory() as folder:
            with (
                httpx.Client(transport=httpx.MockTransport(respond)) as client,
                patch.object(extract, "PDF_DIR", Path(folder)),
                patch.object(extract, "cached_pdf_is_valid", return_value=False),
                patch.object(extract, "retry_pause"),
            ):
                with self.assertRaises(extract.CrawlHalt):
                    extract.fetch_pdf(
                        None, client, CountingLimiter(),
                        "https://www.vtcourts.gov/media/123", refetch=False,
                    )
        self.assertEqual(len(calls), extract.MAX_ATTEMPTS)

    def test_long_retry_after_aborts_instead_of_sleeping(self) -> None:
        with (
            patch.object(extract.time, "sleep") as sleep,
            self.assertRaises(extract.CrawlHalt),
        ):
            extract.retry_pause(1, 429, "3600")
        sleep.assert_not_called()

    def test_retry_after_understands_seconds_and_http_dates(self) -> None:
        self.assertEqual(extract.retry_after_seconds("120"), 120)
        self.assertEqual(extract.retry_after_seconds("not a date"), None)
        self.assertEqual(
            extract.retry_after_seconds("Wed, 01 Jan 2100 00:00:00 GMT") > 0, True
        )


if __name__ == "__main__":
    unittest.main()
