"""Unit tests for GreenhouseScraper module."""

from unittest.mock import MagicMock, patch
import pytest

from pipeline.config import GREENHOUSE_FALLBACK_TOKENS, GREENHOUSE_TOKENS
from pipeline.scrapers.base import PoliteHTTPClient
from pipeline.scrapers.greenhouse import GreenhouseScraper
from pipeline.storage.raw_cache import RawCache


def test_greenhouse_scraper_init_defaults():
    """Verify GreenhouseScraper initializes with default tokens and fallback tokens."""
    scraper = GreenhouseScraper()
    assert scraper.tokens == GREENHOUSE_TOKENS
    assert scraper.fallback_tokens == GREENHOUSE_FALLBACK_TOKENS
    assert isinstance(scraper.client, PoliteHTTPClient)
    assert scraper.raw_cache is None


def test_greenhouse_scraper_init_custom():
    """Verify GreenhouseScraper accepts custom tokens, fallback tokens, and client."""
    mock_client = MagicMock(spec=PoliteHTTPClient)
    custom_tokens = ["custom_corp"]
    custom_fallbacks = ["backup_corp"]
    cache = RawCache(":memory:")

    scraper = GreenhouseScraper(
        client=mock_client,
        tokens=custom_tokens,
        fallback_tokens=custom_fallbacks,
        raw_cache=cache,
    )
    assert scraper.tokens == ["custom_corp"]
    assert scraper.fallback_tokens == ["backup_corp"]
    assert scraper.client is mock_client
    assert scraper.raw_cache is cache


def test_fetch_board_success():
    """Verify fetch_board retrieves jobs on 200 OK and tags them with board token."""
    mock_client = MagicMock(spec=PoliteHTTPClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "jobs": [
            {"id": 101, "title": "Data Engineer"},
            {"id": 102, "title": "ML Engineer"},
        ]
    }
    mock_resp.text = '{"jobs": [{"id": 101}, {"id": 102}]}'
    mock_client.get.return_value = mock_resp

    scraper = GreenhouseScraper(client=mock_client)
    jobs = scraper.fetch_board("stripe")

    assert len(jobs) == 2
    assert jobs[0]["id"] == 101
    assert jobs[0]["_board_token"] == "stripe"
    assert jobs[1]["id"] == 102
    assert jobs[1]["_board_token"] == "stripe"
    mock_client.get.assert_called_once_with(
        "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true"
    )


def test_fetch_board_fallback_on_404():
    """Verify fetch_board falls back to GREENHOUSE_FALLBACK_TOKENS when primary returns 404."""
    mock_client = MagicMock(spec=PoliteHTTPClient)

    def side_effect(url, **kwargs):
        resp = MagicMock()
        if "failing_token" in url:
            resp.status_code = 404
            resp.text = '{"error": "not found"}'
        elif "cloudflare" in url:
            resp.status_code = 200
            resp.json.return_value = {
                "jobs": [{"id": 999, "title": "Cloudflare Systems Engineer"}]
            }
            resp.text = '{"jobs": [{"id": 999}]}'
        else:
            resp.status_code = 404
        return resp

    mock_client.get.side_effect = side_effect
    scraper = GreenhouseScraper(client=mock_client, fallback_tokens=["cloudflare", "gitlab"])

    with patch("pipeline.scrapers.greenhouse.logger.warning") as mock_warn:
        jobs = scraper.fetch_board("failing_token")

    assert len(jobs) == 1
    assert jobs[0]["id"] == 999
    assert jobs[0]["_board_token"] == "cloudflare"
    # Ensure a warning note was logged about the failure and fallback
    assert any("Initiating fallback search" in str(c) for c in mock_warn.call_args_list)
    assert any("Fallback succeeded" in str(c) for c in mock_warn.call_args_list)


def test_fetch_board_fallback_on_zero_jobs():
    """Verify fetch_board falls back when primary returns 200 with an empty job list."""
    mock_client = MagicMock(spec=PoliteHTTPClient)

    def side_effect(url, **kwargs):
        resp = MagicMock()
        if "empty_corp" in url:
            resp.status_code = 200
            resp.json.return_value = {"jobs": []}
            resp.text = '{"jobs": []}'
        elif "gitlab" in url:
            resp.status_code = 200
            resp.json.return_value = {
                "jobs": [{"id": 888, "title": "GitLab Data Architect"}]
            }
            resp.text = '{"jobs": [{"id": 888}]}'
        else:
            resp.status_code = 404
        return resp

    mock_client.get.side_effect = side_effect
    scraper = GreenhouseScraper(client=mock_client, fallback_tokens=["gitlab"])

    jobs = scraper.fetch_board("empty_corp")
    assert len(jobs) == 1
    assert jobs[0]["id"] == 888
    assert jobs[0]["_board_token"] == "gitlab"


def test_fetch_board_all_fallbacks_fail():
    """Verify fetch_board returns empty list when both primary and all fallbacks fail."""
    mock_client = MagicMock(spec=PoliteHTTPClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_client.get.return_value = mock_resp

    scraper = GreenhouseScraper(
        client=mock_client,
        tokens=["dead_token"],
        fallback_tokens=["bad_fallback_1", "bad_fallback_2"],
    )

    jobs = scraper.fetch_board("dead_token")
    assert jobs == []


def test_normalize_job_full_attributes():
    """Verify normalize_job correctly maps and sanitizes all required fields."""
    scraper = GreenhouseScraper()
    raw_job = {
        "id": 8172510,
        "title": "  Senior Staff Data Engineer  ",
        "location": {"name": "  San Francisco, CA / Remote  "},
        "absolute_url": "https://stripe.com/jobs/8172510",
        "updated_at": "2026-11-18T14:30:00Z",
        "content": "&lt;h2&gt;&lt;strong&gt;Role &amp; Responsibilities&lt;/strong&gt;&lt;/h2&gt;",
        "departments": [{"name": "Data Platform"}],
        "_board_token": "stripe",
    }

    normalized = scraper.normalize_job(raw_job, token="stripe")

    assert normalized["source"] == "greenhouse"
    assert normalized["source_id"] == "8172510"
    assert normalized["company"] == "Stripe"
    assert normalized["title"] == "Senior Staff Data Engineer"
    assert normalized["location_raw"] == "San Francisco, CA / Remote"
    assert normalized["url"] == "https://stripe.com/jobs/8172510"
    assert normalized["updated_at"] == "2026-11-18T14:30:00Z"
    assert normalized["description_raw"] == "<h2><strong>Role & Responsibilities</strong></h2>"

    # raw_payload should be untouched and not contain internal helper keys
    assert normalized["raw_payload"]["id"] == 8172510
    assert "_board_token" not in normalized["raw_payload"]
    assert normalized["raw_payload"]["departments"] == [{"name": "Data Platform"}]


def test_normalize_job_graceful_missing_fields():
    """Verify normalize_job handles empty or missing keys gracefully without errors."""
    scraper = GreenhouseScraper()
    empty_raw = {}

    normalized = scraper.normalize_job(empty_raw, token="airbnb")

    assert normalized["source"] == "greenhouse"
    assert normalized["source_id"] == ""
    assert normalized["company"] == "Airbnb"
    assert normalized["title"] == ""
    assert normalized["location_raw"] == ""
    assert normalized["url"] == ""
    assert normalized["updated_at"] == ""
    assert normalized["description_raw"] == ""
    assert normalized["raw_payload"] == {}


def test_scrape_aggregation_across_tokens():
    """Verify scrape iterates through configured tokens and aggregates normalized results."""
    mock_client = MagicMock(spec=PoliteHTTPClient)
    scraper = GreenhouseScraper(client=mock_client, tokens=["stripe", "airbnb"])

    stripe_jobs = [
        {"id": 1, "title": "Stripe Role 1", "location": {"name": "SF"}},
        {"id": 2, "title": "Stripe Role 2", "location": {"name": "NYC"}},
    ]
    airbnb_jobs = [
        {"id": 3, "title": "Airbnb Role 1", "location": {"name": "Remote"}},
    ]

    def mock_fetch_board(token: str):
        if token == "stripe":
            return stripe_jobs
        elif token == "airbnb":
            return airbnb_jobs
        return []

    scraper.fetch_board = MagicMock(side_effect=mock_fetch_board)

    results = scraper.scrape()

    assert len(results) == 3
    assert results[0]["company"] == "Stripe"
    assert results[0]["title"] == "Stripe Role 1"
    assert results[1]["company"] == "Stripe"
    assert results[1]["title"] == "Stripe Role 2"
    assert results[2]["company"] == "Airbnb"
    assert results[2]["title"] == "Airbnb Role 1"


def test_raw_cache_persistence_on_successful_fetch():
    """Verify raw JSON responses are stored in RawCache if configured."""
    cache = RawCache(":memory:")
    mock_client = MagicMock(spec=PoliteHTTPClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"jobs": [{"id": 55, "title": "Cached Job"}]}
    mock_resp.text = '{"jobs": [{"id": 55, "title": "Cached Job"}]}'
    mock_client.get.return_value = mock_resp

    scraper = GreenhouseScraper(client=mock_client, raw_cache=cache)
    jobs = scraper.fetch_board("databricks")

    assert len(jobs) == 1
    assert cache.count(source="greenhouse") == 1
    cached_payload = cache.get_by_url(
        "https://boards-api.greenhouse.io/v1/boards/databricks/jobs?content=true"
    )
    assert cached_payload == '{"jobs": [{"id": 55, "title": "Cached Job"}]}'
