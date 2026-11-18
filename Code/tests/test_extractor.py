"""Comprehensive test suite for Phase 2 LLM Job Extractor Pipeline.

Verifies:
1. JobContentPreprocessor HTML stripping, sanitization, and whitespace normalization.
2. Verbatim evidence verification (exact, case-insensitive, normalized whitespace).
3. Anti-hallucination guard triggering and reverting ungrounded fields to None.
4. Deterministic SHA-256 posting_id generation.
5. End-to-end extraction producing valid JobPostingRecord instances.
6. Salary annualization and strict null handling.
7. Cumulative token accounting, cost calculation, and hallucination mitigation tracking.
8. Optional RawCache integration.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from pipeline.extractor.extractor import (
    JobContentPreprocessor,
    JobExtractor,
)
from pipeline.extractor.llm_client import LLMClient
from pipeline.models.schema import (
    EmploymentTypeEnum,
    JobPostingRecord,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
)
from pipeline.storage.raw_cache import RawCache


# ==============================================================================
# 1. JobContentPreprocessor Tests
# ==============================================================================


class TestJobContentPreprocessor:
    """Tests for HTML boilerplate stripping and text normalization."""

    @pytest.fixture
    def preprocessor(self) -> JobContentPreprocessor:
        return JobContentPreprocessor()

    def test_clean_plain_text(self, preprocessor: JobContentPreprocessor):
        raw = "   Senior Data Engineer   at Stripe.\n\nRemote US only.   "
        cleaned = preprocessor.clean(raw)
        assert cleaned == "Senior Data Engineer at Stripe.\n\nRemote US only."

    def test_clean_html_strips_boilerplate(self, preprocessor: JobContentPreprocessor):
        html = """
        <html>
            <head><title>Job Page</title></head>
            <body>
                <header><nav><a href="/jobs">Back to Jobs</a></nav></header>
                <script>console.log("tracking pixel");</script>
                <style>body { font-size: 14px; }</style>
                <main>
                    <h1>Senior Data Engineer</h1>
                    <p>We are seeking a <b>Senior Data Engineer</b> to join our platform team.</p>
                    <ul>
                        <li>Experience with Python and Apache Spark</li>
                        <li>Familiarity with Snowflake and dbt</li>
                    </ul>
                </main>
                <aside>Related jobs...</aside>
                <footer>&copy; 2026 Acme Corp. All rights reserved.</footer>
            </body>
        </html>
        """
        cleaned = preprocessor.clean(html)

        # Boilerplate should be stripped
        assert "Back to Jobs" not in cleaned
        assert "tracking pixel" not in cleaned
        assert "font-size" not in cleaned
        assert "Related jobs" not in cleaned
        assert "All rights reserved" not in cleaned

        # Core job description should be preserved
        assert "Senior Data Engineer" in cleaned
        assert "platform team" in cleaned
        assert "Experience with Python and Apache Spark" in cleaned
        assert "Familiarity with Snowflake and dbt" in cleaned

    def test_clean_html_entities_and_whitespace(self, preprocessor: JobContentPreprocessor):
        raw = "Data&amp;ML Engineer&nbsp;&nbsp;&nbsp;&ndash;&nbsp;&nbsp;&nbsp;Stripe\r\n\r\n\r\n\r\nRemote"
        cleaned = preprocessor.clean(raw)
        assert "Data&ML Engineer" in cleaned or "Data&amp;ML Engineer" in cleaned
        # Excessive newlines collapsed to max 2
        assert "\n\n\n" not in cleaned
        assert "Stripe" in cleaned
        assert "Remote" in cleaned

    def test_clean_empty_and_none(self, preprocessor: JobContentPreprocessor):
        assert preprocessor.clean(None) == ""
        assert preprocessor.clean("") == ""
        assert preprocessor.clean("   \n\t  ") == ""

    def test_aliases(self, preprocessor: JobContentPreprocessor):
        sample = "<p>Data Engineer</p>"
        res1 = preprocessor.clean(sample)
        res2 = preprocessor.preprocess(sample)
        res3 = preprocessor.clean_html(sample)
        res4 = preprocessor(sample)
        assert res1 == res2 == res3 == res4 == "Data Engineer"


# ==============================================================================
# 2. Evidence Verification Tests
# ==============================================================================


class TestEvidenceVerification:
    """Tests for verify_evidence verbatim substring matching."""

    SOURCE_TEXT = """
    About Stripe:
    Stripe is looking for a Staff Data Engineer, Streaming Infrastructure.
    Location: San Francisco, CA (Hybrid: 2 days in office).
    Base Salary: $210,000 - $265,000 USD per year plus equity and benefits.
    Requirements:
    - 6+ years experience with Apache Kafka, Apache Flink, and Python.
    - Strong distributed systems background.
    Notice: We do not offer visa sponsorship for this role.
    """

    def test_verbatim_exact_match(self):
        evidence = {
            "title": "Staff Data Engineer, Streaming Infrastructure",
            "salary": "$210,000 - $265,000 USD per year",
            "remote_policy": "Hybrid: 2 days in office",
        }
        results = JobExtractor.verify_evidence(self.SOURCE_TEXT, evidence)
        assert results["title"] is True
        assert results["salary"] is True
        assert results["remote_policy"] is True

    def test_case_insensitive_match(self):
        evidence = {
            "title": "STAFF DATA ENGINEER, STREAMING INFRASTRUCTURE",
            "remote_policy": "hybrid: 2 days in office",
        }
        results = JobExtractor.verify_evidence(self.SOURCE_TEXT, evidence)
        assert results["title"] is True
        assert results["remote_policy"] is True

    def test_whitespace_normalized_match(self):
        text_with_newlines = "Base Salary:\n    $210,000 - $265,000 USD\n    per year"
        evidence = {"salary": "$210,000 - $265,000 USD per year"}
        results = JobExtractor.verify_evidence(text_with_newlines, evidence)
        assert results["salary"] is True

    def test_fabricated_snippet_fails(self):
        evidence = {
            "salary": "$300,000 - $400,000 per year",
            "visa_sponsorship": "We provide comprehensive visa sponsorship",
        }
        results = JobExtractor.verify_evidence(self.SOURCE_TEXT, evidence)
        assert results["salary"] is False
        assert results["visa_sponsorship"] is False

    def test_empty_or_none_snippets(self):
        evidence = {
            "salary": None,
            "city": "",
            "skills": "   ",
        }
        results = JobExtractor.verify_evidence(self.SOURCE_TEXT, evidence)
        assert results["salary"] is False
        assert results["city"] is False
        assert results["skills"] is False

    def test_empty_source_text(self):
        evidence = {"title": "Data Engineer"}
        results = JobExtractor.verify_evidence("", evidence)
        assert results["title"] is False


# ==============================================================================
# 3. Anti-Hallucination Guard Tests
# ==============================================================================


class TestAntiHallucinationGuard:
    """Tests for sanitize_hallucinations behavior and mitigation accounting."""

    SOURCE_TEXT = (
        "Databricks is hiring a Senior Data Engineer in San Francisco, CA. "
        "Workplace policy: Hybrid (3 days in office). "
        "Requirements: 5+ years with Python, Spark, and SQL. "
        "We are unable to sponsor work visas at this time."
    )

    @pytest.fixture
    def extractor(self) -> JobExtractor:
        return JobExtractor(llm_client=MagicMock())

    def test_grounded_fields_preserved(self, extractor: JobExtractor):
        extracted = {
            "company": "Databricks",
            "title": "Senior Data Engineer",
            "seniority": "senior",
            "city": "San Francisco",
            "country": None,
            "remote_policy": "hybrid",
            "salary_min": None,
            "salary_max": None,
            "currency": None,
            "skills": ["Python", "Spark", "SQL"],
            "visa_sponsorship": False,
            "evidence": {
                "seniority": "Senior Data Engineer",
                "city": "San Francisco, CA",
                "remote_policy": "Hybrid (3 days in office)",
                "skills": "Python, Spark, and SQL",
                "visa_sponsorship": "unable to sponsor work visas",
            },
        }

        sanitized = extractor.sanitize_hallucinations(extracted, self.SOURCE_TEXT)

        assert sanitized["seniority"] == "senior"
        assert sanitized["city"] == "San Francisco"
        assert sanitized["remote_policy"] == "hybrid"
        assert sanitized["skills"] == ["Python", "Spark", "SQL"]
        assert sanitized["visa_sponsorship"] is False
        assert extractor.hallucination_mitigations == 0

    def test_fabricated_snippet_reverted_to_none(self, extractor: JobExtractor, caplog):
        extracted = {
            "company": "Databricks",
            "title": "Senior Data Engineer",
            "salary_min": 180000.0,
            "salary_max": 230000.0,
            "currency": "USD",
            "remote_policy": "hybrid",
            "evidence": {
                "salary": "$180,000 - $230,000 base pay",  # Fabricated snippet not in source!
                "remote_policy": "Hybrid (3 days in office)",  # Grounded
            },
        }

        with caplog.at_level(logging.WARNING):
            sanitized = extractor.sanitize_hallucinations(extracted, self.SOURCE_TEXT)

        # Salary fields must be nulled
        assert sanitized["salary_min"] is None
        assert sanitized["salary_max"] is None
        assert sanitized["currency"] is None

        # Grounded remote policy preserved
        assert sanitized["remote_policy"] == "hybrid"

        # Fabricated snippet removed from evidence
        assert "salary" not in sanitized["evidence"]
        assert "remote_policy" in sanitized["evidence"]

        # Counter incremented
        assert extractor.hallucination_mitigations >= 2

        # Structured warning logged
        assert "Hallucination detected on field 'salary_min'" in caplog.text
        assert "Set to None." in caplog.text

    def test_missing_evidence_snippet_reverted_to_none(self, extractor: JobExtractor, caplog):
        """If a field was extracted as non-null, but its evidence snippet is completely missing."""
        extracted = {
            "company": "Databricks",
            "title": "Senior Data Engineer",
            "visa_sponsorship": True,  # Non-null, but no evidence snippet!
            "seniority": "senior",
            "evidence": {
                "seniority": "Senior Data Engineer",  # Grounded
                # visa_sponsorship missing
            },
        }

        with caplog.at_level(logging.WARNING):
            sanitized = extractor.sanitize_hallucinations(extracted, self.SOURCE_TEXT)

        assert sanitized["visa_sponsorship"] is None
        assert sanitized["seniority"] == "senior"
        assert extractor.hallucination_mitigations >= 1
        assert "Hallucination detected on field 'visa_sponsorship'" in caplog.text

    def test_currency_nulled_when_salary_numbers_absent(self, extractor: JobExtractor):
        """Strict Null Rule for currency when no compensation numbers exist."""
        extracted = {
            "company": "Databricks",
            "title": "Data Engineer",
            "salary_min": None,
            "salary_max": None,
            "currency": "USD",  # Hallucinated currency without numbers
            "evidence": {},
        }

        sanitized = extractor.sanitize_hallucinations(extracted, self.SOURCE_TEXT)
        assert sanitized["currency"] is None
        assert extractor.hallucination_mitigations >= 1

    def test_skills_empty_list_when_ungrounded(self, extractor: JobExtractor):
        extracted = {
            "company": "Databricks",
            "title": "Data Engineer",
            "skills": ["Rust", "Kubernetes"],
            "evidence": {
                "skills": "Must have 3 years Rust experience",  # Not in source text!
            },
        }

        sanitized = extractor.sanitize_hallucinations(extracted, self.SOURCE_TEXT)
        assert sanitized["skills"] == []
        assert "skills" not in sanitized["evidence"]
        assert extractor.hallucination_mitigations >= 1


# ==============================================================================
# 4. Deterministic ID Generation Tests
# ==============================================================================


class TestDeterministicIdGeneration:
    """Tests for SHA-256 stable posting_id generation."""

    def test_id_stability(self):
        id1 = JobExtractor.generate_posting_id("Stripe", "Staff Data Engineer", "San Francisco, CA")
        id2 = JobExtractor.generate_posting_id("Stripe", "Staff Data Engineer", "San Francisco, CA")
        assert id1 == id2
        assert len(id1) == 64
        # Verify it's a valid hex string
        int(id1, 16)

    def test_id_case_and_whitespace_insensitivity(self):
        id1 = JobExtractor.generate_posting_id("  Stripe  ", "Staff Data Engineer\n", " San Francisco, CA ")
        id2 = JobExtractor.generate_posting_id("stripe", "staff data engineer", "san francisco, ca")
        assert id1 == id2

    def test_id_uniqueness(self):
        id1 = JobExtractor.generate_posting_id("Stripe", "Senior Data Engineer", "Remote")
        id2 = JobExtractor.generate_posting_id("Stripe", "Staff Data Engineer", "Remote")
        id3 = JobExtractor.generate_posting_id("Airbnb", "Senior Data Engineer", "Remote")
        assert id1 != id2
        assert id1 != id3
        assert id2 != id3


# ==============================================================================
# 5. End-to-End Extraction Pipeline Tests
# ==============================================================================


class TestEndToEndExtraction:
    """Tests for JobExtractor.extract_posting."""

    GREENHOUSE_HTML = """
    <div id="content">
        <h1>Staff Data Engineer, Core Platform</h1>
        <div class="location">San Francisco, CA (Hybrid)</div>
        <div class="description">
            <p>Stripe is looking for a Staff Data Engineer to lead our Core Platform team.</p>
            <p>Employment Type: Full-time</p>
            <p>Compensation: $210,000 - $260,000 USD base salary per year.</p>
            <p>Requirements: Apache Spark, Delta Lake, Python, SQL, and Kafka.</p>
            <p>Notice: Applicants must be authorized to work in the US. No visa sponsorship.</p>
        </div>
    </div>
    """

    MOCK_LLM_OUTPUT = {
        "company": "Stripe",
        "title": "Staff Data Engineer, Core Platform",
        "seniority": "staff_plus",
        "city": "San Francisco",
        "country": "US",
        "remote_policy": "hybrid",
        "salary_min": 210000.0,
        "salary_max": 260000.0,
        "currency": "USD",
        "employment_type": "full_time",
        "skills": ["Apache Spark", "Delta Lake", "Python", "SQL", "Kafka"],
        "visa_sponsorship": False,
        "posted_date": None,
        "evidence": {
            "title": "Staff Data Engineer, Core Platform",
            "seniority": "Staff Data Engineer",
            "remote_policy": "San Francisco, CA (Hybrid)",
            "employment_type": "Employment Type: Full-time",
            "salary": "$210,000 - $260,000 USD base salary per year",
            "skills": "Apache Spark, Delta Lake, Python, SQL, and Kafka",
            "visa_sponsorship": "No visa sponsorship",
        },
    }

    def test_extract_posting_success(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.extract.return_value = self.MOCK_LLM_OUTPUT

        extractor = JobExtractor(llm_client=mock_client)

        raw_payload = {
            "company": "Stripe",
            "title": "Staff Data Engineer, Core Platform",
            "url": "https://boards.greenhouse.io/stripe/jobs/456789",
            "raw_html": self.GREENHOUSE_HTML,
        }

        record = extractor.extract_posting(raw_payload)

        # Strongly typed JobPostingRecord assertions
        assert isinstance(record, JobPostingRecord)
        assert record.company == "Stripe"
        assert record.title == "Staff Data Engineer, Core Platform"
        assert record.seniority == SeniorityEnum.staff_plus
        assert record.remote_policy == RemotePolicyEnum.hybrid
        assert record.salary_min == 210000.0
        assert record.salary_max == 260000.0
        assert record.currency == "USD"
        assert record.employment_type == EmploymentTypeEnum.full_time
        assert record.visa_sponsorship is False
        assert record.skills == ["Apache Spark", "Delta Lake", "Python", "SQL", "Kafka"]
        assert record.source_urls == ["https://boards.greenhouse.io/stripe/jobs/456789"]
        assert record.status == StatusEnum.open
        assert len(record.posting_id) == 64

        # Evidence assertions
        assert "salary" in record.evidence
        assert "remote_policy" in record.evidence
        assert record.evidence["salary"] == "$210,000 - $260,000 USD base salary per year"

        # Model serializability
        dumped = record.model_dump()
        assert isinstance(dumped, dict)
        json_str = record.to_json()
        assert "Staff Data Engineer" in json_str

    def test_extract_posting_with_hallucination_mitigation(self):
        """End-to-end extraction where LLM hallucinates an ungrounded salary."""
        html_without_salary = """
        <h1>Lead Data Engineer</h1>
        <p>Location: New York, NY (Remote)</p>
        <p>Skills: Python, dbt, Snowflake</p>
        """

        hallucinated_llm_output = {
            "company": "Plaid",
            "title": "Lead Data Engineer",
            "seniority": "senior",
            "remote_policy": "remote",
            "salary_min": 190000.0,
            "salary_max": 240000.0,
            "currency": "USD",
            "visa_sponsorship": True,  # Hallucinated!
            "skills": ["Python", "dbt", "Snowflake"],
            "evidence": {
                "seniority": "Lead Data Engineer",
                "remote_policy": "Remote",
                "skills": "Python, dbt, Snowflake",
                "salary": "$190,000 - $240,000",  # Fake snippet!
                # visa snippet missing
            },
        }

        mock_client = MagicMock(spec=LLMClient)
        mock_client.extract.return_value = hallucinated_llm_output

        extractor = JobExtractor(llm_client=mock_client)

        raw_payload = {
            "company": "Plaid",
            "title": "Lead Data Engineer",
            "url": "https://jobs.lever.co/plaid/abc123",
            "content": html_without_salary,
        }

        record = extractor.extract_posting(raw_payload)

        # Grounded fields preserved
        assert record.company == "Plaid"
        assert record.title == "Lead Data Engineer"
        assert record.remote_policy == RemotePolicyEnum.remote
        assert record.skills == ["Python", "dbt", "Snowflake"]

        # Hallucinated fields nulled
        assert record.salary_min is None
        assert record.salary_max is None
        assert record.currency is None
        assert record.visa_sponsorship is None

        # Evidence only contains valid grounded snippet
        assert "salary" not in record.evidence
        assert "remote_policy" in record.evidence

        # Mitigation tracked
        assert extractor.hallucination_mitigations >= 2

    def test_extract_posting_hourly_salary_annualization(self):
        """Verify that hourly rates are correctly annualized into annual figures."""
        text = "Hourly Contractor: Data Engineer. Rate is $85/hr. Full-time 40 hrs/week. Remote in US."

        llm_output = {
            "company": "ContractCo",
            "title": "Data Engineer",
            "remote_policy": "remote",
            "salary_min": 85.0,
            "salary_max": 85.0,
            "salary_period": "hour",
            "currency": "USD",
            "evidence": {
                "remote_policy": "Remote in US",
                "salary": "Rate is $85/hr",
            },
        }

        mock_client = MagicMock(spec=LLMClient)
        mock_client.extract.return_value = llm_output

        extractor = JobExtractor(llm_client=mock_client)
        raw_payload = {
            "company": "ContractCo",
            "title": "Data Engineer",
            "text": text,
        }

        record = extractor.extract_posting(raw_payload)

        # $85 * 2080 = $176,800
        assert record.salary_min == 176800.0
        assert record.salary_max == 176800.0
        assert record.currency == "USD"

    def test_extract_posting_fallback_to_extract_job_data(self):
        """Verifies compatibility if LLMClient provides extract_job_data() instead of extract()."""
        mock_client = MagicMock()
        del mock_client.extract  # Remove extract to test fallback
        mock_metrics = {
            "prompt_tokens": 1200,
            "completion_tokens": 300,
            "cost_usd": 0.00018,
        }
        mock_client.extract_job_data.return_value = (self.MOCK_LLM_OUTPUT, mock_metrics)

        extractor = JobExtractor(llm_client=mock_client)
        record = extractor.extract_posting({
            "company": "Stripe",
            "title": "Staff Data Engineer",
            "content": self.GREENHOUSE_HTML,
        })

        assert record.company == "Stripe"
        assert extractor.total_prompt_tokens == 1200
        assert extractor.total_completion_tokens == 300
        assert extractor.total_cost_usd == 0.00018


# ==============================================================================
# 6. Cost Tracking & Accounting Tests
# ==============================================================================


class TestCostTrackingAccounting:
    """Tests for get_cost_summary and accounting logic."""

    def test_cost_summary_initial_state(self):
        extractor = JobExtractor(llm_client=MagicMock())
        summary = extractor.get_cost_summary()

        assert summary["total_extractions"] == 0
        assert summary["total_prompt_tokens"] == 0
        assert summary["total_completion_tokens"] == 0
        assert summary["total_cost_usd"] == 0.0
        assert summary["average_cost_per_posting"] == 0.0
        assert summary["hallucination_mitigations"] == 0

    def test_cost_summary_accumulation(self):
        mock_client = MagicMock()
        extractor = JobExtractor(llm_client=mock_client)

        output = {
            "company": "Figma",
            "title": "Data Engineer",
            "remote_policy": "remote",
            "evidence": {"remote_policy": "Remote"},
        }
        mock_metrics = {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "cost_usd": 0.000135,
        }
        mock_client.extract.return_value = (output, mock_metrics)

        payload = {"company": "Figma", "title": "Data Engineer", "content": "Remote Data Engineer role"}

        # Run 3 extractions
        extractor.extract_posting(payload)
        extractor.extract_posting(payload)
        extractor.extract_posting(payload)

        summary = extractor.get_cost_summary()
        assert summary["total_extractions"] == 3
        assert summary["total_prompt_tokens"] == 3000
        assert summary["total_completion_tokens"] == 600
        assert summary["total_tokens"] == 3600
        assert summary["total_cost_usd"] == pytest.approx(0.000405, abs=1e-6)
        assert summary["average_cost_per_posting"] == pytest.approx(0.000135, abs=1e-6)

    def test_cost_summary_reset(self):
        extractor = JobExtractor(llm_client=MagicMock())
        extractor.total_extractions = 5
        extractor.total_prompt_tokens = 5000
        extractor.total_completion_tokens = 1000
        extractor.total_cost_usd = 0.001
        extractor.hallucination_mitigations = 3

        extractor.reset_cost_summary()
        summary = extractor.get_cost_summary()

        assert summary["total_extractions"] == 0
        assert summary["total_prompt_tokens"] == 0
        assert summary["total_cost_usd"] == 0.0
        assert summary["hallucination_mitigations"] == 0


# ==============================================================================
# 7. RawCache Integration Tests
# ==============================================================================


class TestRawCacheIntegration:
    """Tests for optional RawCache integration within JobExtractor."""

    def test_extractor_with_in_memory_cache(self):
        cache = RawCache(":memory:")
        mock_client = MagicMock()
        mock_client.extract.return_value = {
            "company": "Airbnb",
            "title": "Staff Data Engineer",
            "remote_policy": "remote",
            "evidence": {"remote_policy": "Remote"},
        }

        extractor = JobExtractor(llm_client=mock_client, cache=cache)

        raw_payload = {
            "company": "Airbnb",
            "title": "Staff Data Engineer",
            "url": "https://careers.airbnb.com/positions/998877",
            "content": "Staff Data Engineer. Fully Remote position.",
        }

        record = extractor.extract_posting(raw_payload)
        assert record.company == "Airbnb"

        # Verify cache has stored the raw content
        assert cache.count() == 1
        cached_payload = cache.get_by_url("https://careers.airbnb.com/positions/998877")
        assert cached_payload is not None
        assert "Fully Remote position" in cached_payload

        cache.close()
