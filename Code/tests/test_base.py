"""Unit tests for pipeline configuration and base scraper infrastructure."""

import time
from unittest.mock import MagicMock, patch
import pytest
import requests

from pipeline.config import (
    DB_PATH,
    DEFAULT_USER_AGENT,
    GREENHOUSE_FALLBACK_TOKENS,
    GREENHOUSE_TOKENS,
    HN_SUBMITTED_URL,
    LEVER_SITES,
    RATE_LIMIT_DELAY,
    RAW_CACHE_DIR,
)
from pipeline.scrapers.base import (
    BaseScraper,
    DomainRateLimiter,
    PoliteHTTPClient,
    RobotsParser,
)


def test_config_constants():
    """Verify that all required constants are properly configured."""
    assert GREENHOUSE_TOKENS == ["stripe", "airbnb", "databricks", "figma"]
    assert GREENHOUSE_FALLBACK_TOKENS == ["cloudflare", "gitlab"]
    assert LEVER_SITES == ["palantir", "plaid"]
    assert HN_SUBMITTED_URL == "https://news.ycombinator.com/submitted?id=whoishiring"
    assert RATE_LIMIT_DELAY == 1.0
    assert "TwigDataPipelineBot/1.0" in DEFAULT_USER_AGENT
    assert DB_PATH == "D:/Project_Files/AGY_Assignments/Twig/Code/data/pipeline.db"
    assert RAW_CACHE_DIR == "D:/Project_Files/AGY_Assignments/Twig/Code/data/raw"


def test_domain_rate_limiter_enforces_delay():
    """Verify rate limiter delays consecutive calls to the same domain."""
    limiter = DomainRateLimiter(delay=0.1)

    start = time.monotonic()
    limiter.wait("https://api.example.com/v1/jobs")
    limiter.wait("https://api.example.com/v1/departments")
    elapsed = time.monotonic() - start

    assert elapsed >= 0.09


def test_domain_rate_limiter_isolated_domains():
    """Verify rate limiter does not delay requests across different domains."""
    limiter = DomainRateLimiter(delay=0.5)

    start = time.monotonic()
    limiter.wait("https://domain1.com/api")
    limiter.wait("https://domain2.com/api")
    elapsed = time.monotonic() - start

    # Elapsed should be significantly less than the 0.5s limit because domains differ
    assert elapsed < 0.2


def test_robots_parser_caching_and_allowance():
    """Verify RobotsParser caches robots.txt per domain and respects directives."""
    parser = RobotsParser()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "User-agent: *\nDisallow: /private/\nAllow: /public/\n"

    with patch("requests.get", return_value=mock_resp) as mock_get:
        allowed = parser.can_fetch("TwigDataPipelineBot", "https://example.org/public/jobs")
        assert allowed is True

        disallowed = parser.can_fetch("TwigDataPipelineBot", "https://example.org/private/jobs")
        assert disallowed is False

        # Second domain query should use cache, not invoke requests.get again
        parser.can_fetch("TwigDataPipelineBot", "https://example.org/public/other")
        assert mock_get.call_count == 1


def test_robots_parser_failsafe_on_error():
    """Verify RobotsParser defaults to True on network errors or 404s."""
    parser = RobotsParser()

    with patch("requests.get", side_effect=requests.exceptions.Timeout("Connection timed out")):
        allowed = parser.can_fetch("TwigDataPipelineBot", "https://unavailable.example.com/jobs")
        assert allowed is True


def test_polite_http_client_robots_disallowed():
    """Verify PoliteHTTPClient skips fetch and returns None if disallowed by robots.txt."""
    mock_robots = MagicMock(spec=RobotsParser)
    mock_robots.can_fetch.return_value = False

    client = PoliteHTTPClient(robots_parser=mock_robots)
    response = client.get("https://forbidden.example.com/jobs")

    assert response is None
    mock_robots.can_fetch.assert_called_once()


def test_polite_http_client_sets_default_headers():
    """Verify PoliteHTTPClient sets default User-Agent and Accept headers."""
    mock_robots = MagicMock(spec=RobotsParser)
    mock_robots.can_fetch.return_value = True
    client = PoliteHTTPClient(robots_parser=mock_robots)

    with patch.object(client.session, "get") as mock_session_get:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_session_get.return_value = mock_resp

        client.get("https://api.example.com/jobs", headers={"X-Custom": "val"})

        call_kwargs = mock_session_get.call_args.kwargs
        sent_headers = call_kwargs["headers"]
        assert sent_headers["User-Agent"] == DEFAULT_USER_AGENT
        assert "application/json" in sent_headers["Accept"]
        assert sent_headers["X-Custom"] == "val"


def test_polite_http_client_failsoft_on_exception():
    """Verify PoliteHTTPClient logs errors without crashing on fatal network errors."""
    mock_robots = MagicMock(spec=RobotsParser)
    mock_robots.can_fetch.return_value = True
    client = PoliteHTTPClient(robots_parser=mock_robots, total_retries=2, backoff_factor=0.01)

    with patch.object(client.session, "get", side_effect=requests.exceptions.ConnectionError("Failed")):
        response = client.get("https://unreachable.example.com/api")
        assert response is None


def test_base_scraper_contract():
    """Verify BaseScraper abstract class initialization and contract."""
    client = PoliteHTTPClient()

    class TestScraper(BaseScraper):
        def scrape(self):
            return [{"id": 1, "title": "Software Engineer"}]

    scraper = TestScraper(client=client)
    records = scraper.scrape()
    assert len(records) == 1
    assert records[0]["title"] == "Software Engineer"
