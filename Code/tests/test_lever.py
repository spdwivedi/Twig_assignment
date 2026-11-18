"""Unit tests for LeverScraper module."""

from unittest.mock import MagicMock, patch
import pytest

from pipeline.config import LEVER_SITES
from pipeline.scrapers.base import PoliteHTTPClient
from pipeline.scrapers.lever import LeverScraper
from pipeline.storage.raw_cache import RawCache


def test_lever_scraper_init_defaults():
    """Verify LeverScraper initializes with default sites and client."""
    scraper = LeverScraper()
    assert scraper.sites == LEVER_SITES
    assert isinstance(scraper.client, PoliteHTTPClient)
    assert scraper.raw_cache is None


def test_lever_scraper_init_custom():
    """Verify LeverScraper accepts custom sites, client, and raw cache."""
    mock_client = MagicMock(spec=PoliteHTTPClient)
    custom_sites = ["custom_org"]
    cache = RawCache(":memory:")

    scraper = LeverScraper(
        client=mock_client,
        sites=custom_sites,
        raw_cache=cache,
    )
    assert scraper.sites == ["custom_org"]
    assert scraper.client is mock_client
    assert scraper.raw_cache is cache


def test_content_type_trap_html():
    """CRITICAL: Verify response.json() is NOT called when Content-Type is non-JSON HTML."""
    mock_client = MagicMock(spec=PoliteHTTPClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
    mock_resp.text = "<html><body><h1>Cloudflare / Captcha Challenge</h1></body></html>"
    # If response.json() is accidentally called, trigger an immediate failure
    mock_resp.json.side_effect = AssertionError("response.json() MUST NOT be called when Content-Type is non-JSON!")
    mock_client.get.return_value = mock_resp

    scraper = LeverScraper(client=mock_client)
    with patch("pipeline.scrapers.lever.logger.warning") as mock_warn:
        results = scraper.fetch_site("palantir")

    assert results == []
    mock_resp.json.assert_not_called()
    assert any("non-JSON Content-Type" in str(c) for c in mock_warn.call_args_list)


def test_content_type_trap_missing_content_type():
    """Verify response.json() is NOT called when Content-Type header is absent."""
    mock_client = MagicMock(spec=PoliteHTTPClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {}
    mock_resp.text = "error payload"
    mock_resp.json.side_effect = AssertionError("response.json() MUST NOT be called without JSON Content-Type!")
    mock_client.get.return_value = mock_resp

    scraper = LeverScraper(client=mock_client)
    results = scraper.fetch_site("palantir")
    assert results == []
    mock_resp.json.assert_not_called()


def test_fetch_site_none_response():
    """Verify fetch_site handles None response gracefully (e.g. network failure or robots.txt block)."""
    mock_client = MagicMock(spec=PoliteHTTPClient)
    mock_client.get.return_value = None

    scraper = LeverScraper(client=mock_client)
    with patch("pipeline.scrapers.lever.logger.warning") as mock_warn:
        results = scraper.fetch_site("palantir")

    assert results == []
    assert any("No HTTP response received" in str(c) for c in mock_warn.call_args_list)


def test_fetch_site_http_error_status():
    """Verify fetch_site handles non-200 HTTP statuses gracefully (e.g. 404 for Plaid)."""
    mock_client = MagicMock(spec=PoliteHTTPClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.headers = {"Content-Type": "application/json; charset=utf-8"}
    mock_resp.text = '{"ok": false, "error": "Document not found"}'
    mock_client.get.return_value = mock_resp

    scraper = LeverScraper(client=mock_client)
    with patch("pipeline.scrapers.lever.logger.warning") as mock_warn:
        results = scraper.fetch_site("plaid")

    assert results == []
    assert any("returned HTTP status 404" in str(c) for c in mock_warn.call_args_list)


def test_fetch_site_success_json_list():
    """Verify fetch_site parses and returns list of job postings on valid JSON response."""
    mock_client = MagicMock(spec=PoliteHTTPClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "application/json; charset=utf-8"}
    expected_jobs = [
        {"id": "job-1", "text": "Data Engineer"},
        {"id": "job-2", "text": "ML Platform Engineer"},
    ]
    mock_resp.json.return_value = expected_jobs
    mock_resp.text = '[{"id": "job-1"}, {"id": "job-2"}]'
    mock_client.get.return_value = mock_resp

    scraper = LeverScraper(client=mock_client)
    results = scraper.fetch_site("palantir")

    assert results == expected_jobs
    assert len(results) == 2
    mock_client.get.assert_called_once_with("https://api.lever.co/v0/postings/palantir?mode=json")


def test_fetch_site_raw_cache_persistence():
    """Verify fetch_site saves response text into RawCache when cache is configured."""
    mock_client = MagicMock(spec=PoliteHTTPClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.json.return_value = [{"id": "job-abc"}]
    payload_text = '[{"id": "job-abc"}]'
    mock_resp.text = payload_text
    mock_client.get.return_value = mock_resp

    cache = RawCache(":memory:")
    scraper = LeverScraper(client=mock_client, raw_cache=cache)
    results = scraper.fetch_site("palantir")

    assert len(results) == 1
    cached_payload = cache.get_by_url("https://api.lever.co/v0/postings/palantir?mode=json")
    assert cached_payload == payload_text


def test_fetch_site_json_parse_exception():
    """Verify fetch_site handles malformed JSON payload without crashing."""
    mock_client = MagicMock(spec=PoliteHTTPClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.json.side_effect = ValueError("Malformed JSON syntax")
    mock_client.get.return_value = mock_resp

    scraper = LeverScraper(client=mock_client)
    with patch("pipeline.scrapers.lever.logger.error") as mock_err:
        results = scraper.fetch_site("palantir")

    assert results == []
    assert any("Failed to parse JSON response" in str(c) for c in mock_err.call_args_list)


def test_fetch_site_unexpected_json_payload_type():
    """Verify fetch_site returns [] when API returns a JSON dictionary or primitive instead of a list."""
    mock_client = MagicMock(spec=PoliteHTTPClient)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.json.return_value = {"error": "Site disabled"}
    mock_client.get.return_value = mock_resp

    scraper = LeverScraper(client=mock_client)
    results = scraper.fetch_site("some_site")
    assert results == []


def test_normalize_job_full():
    """Verify normalize_job extracts all standard fields correctly."""
    scraper = LeverScraper()
    raw = {
        "id": "abc-789",
        "text": " Forward Deployed Software Engineer - Data ",
        "categories": {
            "location": " Washington, DC ",
            "commitment": "Full-time",
            "team": "Defense & Intelligence",
        },
        "hostedUrl": "https://jobs.lever.co/palantir/abc-789",
        "createdAt": 1715000000000,
        "descriptionPlain": "Full plain text description of the role.",
        "description": "<p>HTML description</p>",
        "extra_field": "metadata",
    }

    norm = scraper.normalize_job(raw, "palantir")

    assert norm["source"] == "lever"
    assert norm["source_id"] == "abc-789"
    assert norm["company"] == "Palantir"
    assert norm["title"] == "Forward Deployed Software Engineer - Data"
    assert norm["location_raw"] == "Washington, DC"
    assert norm["commitment"] == "Full-time"
    assert norm["team"] == "Defense & Intelligence"
    assert norm["url"] == "https://jobs.lever.co/palantir/abc-789"
    assert norm["created_at"] == 1715000000000
    assert norm["description_raw"] == "Full plain text description of the role."
    assert norm["raw_payload"] == raw


def test_normalize_job_sparse_and_fallbacks():
    """Verify normalize_job handles missing categories, null descriptionPlain, and null id gracefully."""
    scraper = LeverScraper()
    raw = {
        "id": None,
        "text": None,
        "categories": None,
        "hostedUrl": None,
        "createdAt": None,
        "descriptionPlain": None,
        "description": "Fallback HTML text",
    }

    norm = scraper.normalize_job(raw, "plaid")

    assert norm["source"] == "lever"
    assert norm["source_id"] == ""
    assert norm["company"] == "Plaid"
    assert norm["title"] == ""
    assert norm["location_raw"] == ""
    assert norm["commitment"] == ""
    assert norm["team"] == ""
    assert norm["url"] == ""
    assert norm["created_at"] is None
    assert norm["description_raw"] == "Fallback HTML text"
    assert norm["raw_payload"] == raw


def test_scrape_multi_site():
    """Verify scrape aggregates and normalizes postings across all configured sites."""
    mock_client = MagicMock(spec=PoliteHTTPClient)

    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/json"}
        if "palantir" in url:
            resp.json.return_value = [
                {"id": "p1", "text": "Palantir Data Eng", "categories": {"location": "NYC"}}
            ]
        elif "plaid" in url:
            resp.json.return_value = [
                {"id": "pl1", "text": "Plaid ML Eng", "categories": {"location": "SF"}}
            ]
        else:
            resp.json.return_value = []
        return resp

    mock_client.get.side_effect = mock_get

    scraper = LeverScraper(client=mock_client, sites=["palantir", "plaid"])
    all_jobs = scraper.scrape()

    assert len(all_jobs) == 2
    assert all_jobs[0]["company"] == "Palantir"
    assert all_jobs[0]["title"] == "Palantir Data Eng"
    assert all_jobs[1]["company"] == "Plaid"
    assert all_jobs[1]["title"] == "Plaid ML Eng"
