"""Comprehensive test suite for Phase 1 Ingestion: RawCache, RoleFilter, and Ingestion Runner."""

import json
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.filters.role_filter import RoleFilter
from pipeline.ingest import run_ingest, _extract_payload_string, _extract_url, _format_summary_table
from pipeline.storage.raw_cache import RawCache


class TestRawCache:
    """Unit tests for SQLite-backed RawCache."""

    def test_init_and_table_creation(self):
        cache = RawCache(":memory:")
        assert cache.count() == 0
        cache.close()

    def test_save_and_get_payload(self):
        cache = RawCache(":memory:")
        url = "https://boards-api.greenhouse.io/v1/boards/stripe/jobs/123"
        payload = json.dumps({"title": "Staff Data Engineer", "location": "Remote"})

        cache_id = cache.save(source="greenhouse", url=url, payload=payload)
        assert isinstance(cache_id, str)
        assert len(cache_id) == 64

        assert cache.has(cache_id) is True
        assert cache.get(cache_id) == payload
        assert cache.count() == 1
        assert cache.count(source="greenhouse") == 1
        assert cache.count(source="lever") == 0
        cache.close()

    def test_save_batch(self):
        cache = RawCache(":memory:")
        items = [
            ("greenhouse", f"https://gh.com/{i}", json.dumps({"job": i}))
            for i in range(10)
        ]
        cids = cache.save_batch(items)
        assert len(cids) == 10
        assert cache.count() == 10
        assert cache.count("greenhouse") == 10
        for cid in cids:
            assert cache.has(cid) is True
        cache.close()

    def test_generate_cache_id_determinism(self):
        url = "https://example.com/job"
        content = "Sample job posting text for hashing"
        cid1 = RawCache.generate_cache_id(url, content)
        cid2 = RawCache.generate_cache_id(url, content)
        assert cid1 == cid2

        # Verify first 500 chars limit
        long_content1 = "A" * 500 + "B" * 100
        long_content2 = "A" * 500 + "C" * 100
        assert RawCache.generate_cache_id(url, long_content1) == RawCache.generate_cache_id(url, long_content2)

    def test_idempotent_replace(self):
        cache = RawCache(":memory:")
        url = "https://api.lever.co/v0/postings/palantir/abc"
        payload1 = json.dumps({"id": "abc", "version": 1})
        payload2 = json.dumps({"id": "abc", "version": 1})

        cid1 = cache.save("lever", url, payload1)
        cid2 = cache.save("lever", url, payload2)
        assert cid1 == cid2
        assert cache.count() == 1
        cache.close()

    def test_clear_by_source(self):
        cache = RawCache(":memory:")
        cache.save("greenhouse", "https://gh.com/1", "p1")
        cache.save("greenhouse", "https://gh.com/2", "p2")
        cache.save("lever", "https://lever.com/1", "p3")
        assert cache.count() == 3

        deleted = cache.clear(source="greenhouse")
        assert deleted == 2
        assert cache.count() == 1
        assert cache.count("lever") == 1
        cache.close()


class TestRoleFilter:
    """Unit tests for RoleFilter target role and US-remote eligibility."""

    @pytest.fixture
    def rf(self):
        return RoleFilter()

    def test_target_titles_matched(self, rf):
        valid_titles = [
            "Data Engineer",
            "Senior Data Engineer",
            "Staff Data Engineer",
            "Principal Data Engineer",
            "Lead Data Engineer",
            "Data Engineering Manager",
            "Director of Data Engineering",
            "Machine Learning Engineer",
            "Staff ML Engineer",
            "Applied Machine Learning Engineer",
            "AI/ML Engineer",
            "Analytics Engineer",
            "Senior Analytics Engineer",
            "Platform Engineer",
            "Data Platform Engineer",
            "ML Platform Engineer",
            "Data Scientist",
            "Senior Machine Learning Scientist",
            "Software Engineer, Data",
            "Software Engineer - Machine Learning",
            "Software Engineer (Data Platform)",
            "SWE - Analytics Infrastructure",
            "MLOps Engineer",
            "Big Data Developer",
            "Database Engineer",
        ]
        for title in valid_titles:
            assert rf.is_target_title(title) is True, f"Failed to match valid target title: '{title}'"

    def test_non_target_titles_rejected(self, rf):
        invalid_titles = [
            "Frontend Engineer",
            "iOS Developer",
            "Android Developer",
            "Technical Recruiter",
            "Recruiter - Data Engineering",
            "Account Executive",
            "Sales Manager",
            "Product Marketing Manager",
            "Product Manager, Data Products",
            "Customer Support Specialist",
            "Graphic Designer",
            "Legal Counsel",
            "Salesforce Administrator",
        ]
        for title in invalid_titles:
            assert rf.is_target_title(title) is False, f"Erroneously matched non-target title: '{title}'"

    def test_us_or_remote_locations_matched(self, rf):
        valid_locations = [
            "San Francisco, CA",
            "New York, NY",
            "Austin, TX",
            "Seattle, WA",
            "Boston, Massachusetts",
            "Denver, CO",
            "Remote",
            "US Remote",
            "Remote - US",
            "Remote, United States",
            "United States",
            "Sunnyvale, CA, USA",
            "Chicago, IL",
            "Atlanta, GA",
            "Work from Home",
        ]
        for loc in valid_locations:
            assert rf.is_us_or_remote(loc) is True, f"Failed to match valid US/Remote location: '{loc}'"

    def test_foreign_locations_rejected(self, rf):
        invalid_locations = [
            "London, UK",
            "Berlin, Germany",
            "Toronto, Canada",
            "Vancouver, BC, Canada",
            "Sydney, Australia",
            "Bangalore, India",
            "Remote - EMEA",
            "Remote (Europe)",
            "Paris, France",
            "Tokyo, Japan",
        ]
        for loc in invalid_locations:
            assert rf.is_us_or_remote(loc) is False, f"Erroneously matched non-US location: '{loc}'"

    def test_filter_postings_greenhouse_and_lever(self, rf):
        postings = [
            {
                "source": "greenhouse",
                "title": "Senior Data Engineer",
                "location_raw": "San Francisco, CA",
                "description_raw": "Build scalable data pipelines.",
            },
            {
                "source": "greenhouse",
                "title": "Frontend Engineer",
                "location_raw": "San Francisco, CA",
                "description_raw": "React UI work.",
            },
            {
                "source": "lever",
                "title": "Machine Learning Engineer",
                "location": {"name": "Remote, US"},
                "description_raw": "Deploy LLM models.",
            },
            {
                "source": "lever",
                "title": "Data Engineer",
                "location_raw": "London, UK",
                "description_raw": "ETL pipelines.",
            },
        ]
        candidates = rf.filter_postings(postings)
        assert len(candidates) == 2
        assert candidates[0]["title"] == "Senior Data Engineer"
        assert candidates[1]["title"] == "Machine Learning Engineer"

    def test_filter_postings_hacker_news(self, rf):
        hn_postings = [
            {
                "source": "hackernews",
                "title": "Stripe | Remote (US) | Full-time",
                "description_raw": "We are looking for Senior Data Engineers to build streaming systems with Flink and Kafka.",
            },
            {
                "source": "hn",
                "title": "Startup | Berlin, Germany",
                "description_raw": "Hiring Frontend Developers and UI Designers.",
            },
            {
                "source": "hackernews",
                "title": "Acme | London, UK",
                "description_raw": "Hiring ML Engineers for our London office.",
            },
            {
                "source": "hackernews",
                "title": "FinTech Co | Remote",
                "description_raw": "Looking for MLOps Engineers and Platform Engineers to manage Kubernetes clusters.",
            },
        ]
        candidates = rf.filter_postings(hn_postings)
        assert len(candidates) == 2
        assert "Data Engineer" in candidates[0]["description_raw"]
        assert "MLOps Engineer" in candidates[1]["description_raw"]


class TestIngestRunner:
    """Tests for pipeline orchestrator run_ingest function."""

    @patch("pipeline.ingest.GreenhouseScraper")
    @patch("pipeline.ingest.LeverScraper")
    @patch("pipeline.ingest.HackerNewsScraper")
    def test_run_ingest_orchestration(self, mock_hn_cls, mock_lever_cls, mock_gh_cls, tmp_path):
        db_file = tmp_path / "test_pipeline.db"

        # Mock Greenhouse
        mock_gh = MagicMock()
        mock_gh.scrape.return_value = [
            {"source": "greenhouse", "id": "1", "title": "Data Engineer", "location_raw": "San Francisco, CA", "url": "https://gh.com/1"},
            {"source": "greenhouse", "id": "2", "title": "Product Designer", "location_raw": "Remote", "url": "https://gh.com/2"},
        ]
        mock_gh_cls.return_value = mock_gh

        # Mock Lever
        mock_lever = MagicMock()
        mock_lever.scrape.return_value = [
            {"source": "lever", "id": "3", "title": "Machine Learning Engineer", "location_raw": "New York, NY", "url": "https://lever.com/3"},
            {"source": "lever", "id": "4", "title": "Data Engineer", "location_raw": "Berlin, Germany", "url": "https://lever.com/4"},
        ]
        mock_lever_cls.return_value = mock_lever

        # Mock HN
        mock_hn = MagicMock()
        mock_hn.scrape.return_value = [
            {"source": "hackernews", "id": "5", "title": "Company | Remote US", "description_raw": "Hiring Analytics Engineers and ML Platform Engineers", "url": "https://news.ycombinator.com/item?id=5"},
        ]
        mock_hn_cls.return_value = mock_hn

        # Run with filter_roles=True
        results = run_ingest(
            sources=["greenhouse", "lever", "hn"],
            filter_roles=True,
            db_path=str(db_file),
        )

        # Expected matching:
        # GH: Data Engineer (SF) -> match (1)
        # Lever: ML Engineer (NY) -> match (1)
        # HN: Analytics Engineers (Remote US) -> match (1)
        assert len(results) == 3
        assert any(r["title"] == "Data Engineer" for r in results)
        assert any(r["title"] == "Machine Learning Engineer" for r in results)

        # Verify SQLite cache was populated
        cache = RawCache(str(db_file))
        assert cache.count() == 5  # all 5 raw postings should be cached
        assert cache.count("greenhouse") == 2
        assert cache.count("lever") == 2
        assert cache.count("hn") == 1
        cache.close()

    @patch("pipeline.ingest.GreenhouseScraper")
    def test_run_ingest_with_limit_and_no_filter(self, mock_gh_cls, tmp_path):
        db_file = tmp_path / "test_pipeline_unfiltered.db"

        mock_gh = MagicMock()
        mock_gh.scrape.return_value = [
            {"source": "greenhouse", "id": str(i), "title": f"Title {i}", "location_raw": "Remote", "url": f"https://gh.com/{i}"}
            for i in range(10)
        ]
        mock_gh_cls.return_value = mock_gh

        results = run_ingest(
            sources=["greenhouse"],
            limit=4,
            filter_roles=False,
            db_path=str(db_file),
        )

        assert len(results) == 4
        cache = RawCache(str(db_file))
        assert cache.count() == 4
        cache.close()
