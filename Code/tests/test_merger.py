"""Unit and integration tests for RecordMerger and CanonicalJobPosting model.

Verifies:
- Source authority hierarchy prioritization (Greenhouse > Lever > Careers > HN)
- Non-null geographic and remote policy fallback
- Salary resolution: non-null priority and highest-authority precedence
- Case-insensitive skills union and Title-casing
- Evidence dictionary merging preserving highest-authority snippets
- Provenance auditing: sources, source_urls, source_posting_ids, and duplicate_count
- Timestamps calculation: min(first_seen) and max(last_seen)
- Idempotent 64-char SHA-256 canonical_id generation
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import List

import pytest
from pydantic import ValidationError

from pipeline.dedup.merger import RecordMerger, extract_source_name
from pipeline.models.canonical import (
    CanonicalJobPosting,
    format_skill,
    generate_canonical_id,
)
from pipeline.models.schema import (
    EmploymentTypeEnum,
    JobPostingRecord,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
)


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def merger() -> RecordMerger:
    return RecordMerger()


def make_record(
    posting_id: str = "rec_001",
    company: str = "Stripe",
    title: str = "Senior Data Engineer",
    seniority: SeniorityEnum = SeniorityEnum.senior,
    city: str = "San Francisco",
    country: str = "US",
    remote_policy: RemotePolicyEnum = RemotePolicyEnum.hybrid,
    salary_min: float = 160000.0,
    salary_max: float = 220000.0,
    currency: str = "USD",
    employment_type: EmploymentTypeEnum = EmploymentTypeEnum.full_time,
    skills: List[str] = None,
    visa_sponsorship: bool = True,
    source_urls: List[str] = None,
    evidence: dict = None,
    first_seen: datetime = None,
    last_seen: datetime = None,
    status: StatusEnum = StatusEnum.open,
    posted_date: date = None,
) -> JobPostingRecord:
    now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    return JobPostingRecord(
        posting_id=posting_id,
        company=company,
        title=title,
        seniority=seniority,
        city=city,
        country=country,
        remote_policy=remote_policy,
        salary_min=salary_min,
        salary_max=salary_max,
        currency=currency,
        employment_type=employment_type,
        skills=skills if skills is not None else ["Python", "SQL"],
        visa_sponsorship=visa_sponsorship,
        source_urls=source_urls if source_urls is not None else ["https://boards.greenhouse.io/stripe/jobs/101"],
        evidence=evidence if evidence is not None else {"title": "Senior Data Engineer opening"},
        first_seen=first_seen or now,
        last_seen=last_seen or now,
        status=status,
        posted_date=posted_date,
    )


# ==============================================================================
# Tests
# ==============================================================================


class TestRecordMergerAuthority:
    """Tests source authority weighting and field prioritization."""

    def test_greenhouse_over_hackernews(self, merger: RecordMerger):
        """Greenhouse record (authority 10) overrides Hacker News record (authority 5)."""
        gh_rec = make_record(
            posting_id="gh_1",
            company="Stripe",
            title="Staff Data Engineer",
            source_urls=["https://boards.greenhouse.io/stripe/jobs/999"],
            evidence={"title": "Official Staff Data Engineer listing on Greenhouse"},
        )
        hn_rec = make_record(
            posting_id="hn_1",
            company="stripe inc",
            title="Data Eng / Staff (whoishiring)",
            source_urls=["https://news.ycombinator.com/item?id=8888"],
            evidence={"title": "HN mention: Data Eng / Staff"},
        )

        canonical = merger.merge([hn_rec, gh_rec])

        assert canonical.company == "Stripe"
        assert canonical.title == "Staff Data Engineer"
        assert canonical.duplicate_count == 2
        assert "greenhouse" in canonical.sources
        assert "hackernews" in canonical.sources
        assert set(canonical.source_posting_ids) == {"gh_1", "hn_1"}

    def test_lever_over_hackernews(self, merger: RecordMerger):
        """Lever record (authority 10) takes precedence over Hacker News record."""
        lever_rec = make_record(
            posting_id="lev_1",
            company="Palantir",
            title="Senior Deployment Strategist",
            source_urls=["https://jobs.lever.co/palantir/555"],
        )
        hn_rec = make_record(
            posting_id="hn_2",
            company="Palantir Tech",
            title="Deployment Strategist - Sr",
            source_urls=["https://news.ycombinator.com/item?id=9999"],
        )

        canonical = merger.merge([hn_rec, lever_rec])

        assert canonical.company == "Palantir"
        assert canonical.title == "Senior Deployment Strategist"
        assert canonical.duplicate_count == 2
        assert "lever" in canonical.sources
        assert "hackernews" in canonical.sources


class TestRecordMergerFieldResolution:
    """Tests resolution of specific fields across duplicate records."""

    def test_location_and_remote_policy_non_null_fallback(self, merger: RecordMerger):
        """Non-null location and remote policy values are preserved from lower-authority source if top source is null."""
        gh_rec = make_record(
            posting_id="gh_1",
            city=None,
            country=None,
            remote_policy=None,
            source_urls=["https://boards.greenhouse.io/stripe/jobs/1"],
        )
        hn_rec = make_record(
            posting_id="hn_1",
            city="Seattle",
            country="US",
            remote_policy=RemotePolicyEnum.remote,
            source_urls=["https://news.ycombinator.com/item?id=2"],
        )

        canonical = merger.merge([gh_rec, hn_rec])

        assert canonical.city == "Seattle"
        assert canonical.country == "US"
        assert canonical.remote_policy == RemotePolicyEnum.remote

    def test_salary_non_null_takes_priority(self, merger: RecordMerger):
        """Non-null salary is selected even when the top-authority record has null salary."""
        gh_rec = make_record(
            posting_id="gh_1",
            salary_min=None,
            salary_max=None,
            currency=None,
            source_urls=["https://boards.greenhouse.io/stripe/jobs/1"],
        )
        hn_rec = make_record(
            posting_id="hn_1",
            salary_min=175000.0,
            salary_max=225000.0,
            currency="USD",
            source_urls=["https://news.ycombinator.com/item?id=2"],
        )

        canonical = merger.merge([gh_rec, hn_rec])

        assert canonical.salary_min == 175000.0
        assert canonical.salary_max == 225000.0
        assert canonical.currency == "USD"

    def test_salary_highest_authority_precedence_when_both_present(self, merger: RecordMerger):
        """When multiple records have salary, the highest authority source takes precedence."""
        gh_rec = make_record(
            posting_id="gh_1",
            salary_min=190000.0,
            salary_max=250000.0,
            currency="USD",
            source_urls=["https://boards.greenhouse.io/stripe/jobs/1"],
        )
        hn_rec = make_record(
            posting_id="hn_1",
            salary_min=150000.0,
            salary_max=200000.0,
            currency="USD",
            source_urls=["https://news.ycombinator.com/item?id=2"],
        )

        canonical = merger.merge([hn_rec, gh_rec])

        assert canonical.salary_min == 190000.0
        assert canonical.salary_max == 250000.0

    def test_skills_case_insensitive_union_and_title_case(self, merger: RecordMerger):
        """Skills are unified across all records case-insensitively and Title-cased/standardized."""
        rec1 = make_record(
            posting_id="r1",
            skills=["python", "SQL", "dbt"],
            source_urls=["https://boards.greenhouse.io/stripe/1"],
        )
        rec2 = make_record(
            posting_id="r2",
            skills=["Python", "spark", "AWS", "SQL"],
            source_urls=["https://jobs.lever.co/stripe/2"],
        )
        rec3 = make_record(
            posting_id="r3",
            skills=["KAFKA", "sql", "airflow", "gcp"],
            source_urls=["https://news.ycombinator.com/item?id=3"],
        )

        canonical = merger.merge([rec1, rec2, rec3])

        # All skills unified, case-insensitive, sorted
        expected_skills = ["Airflow", "AWS", "dbt", "GCP", "Kafka", "Python", "Spark", "SQL"]
        assert canonical.skills == expected_skills

    def test_evidence_merging_retains_highest_authority_per_field(self, merger: RecordMerger):
        """Evidence snippets are combined, keeping the snippet from the highest authority source for each field."""
        gh_rec = make_record(
            posting_id="gh_1",
            source_urls=["https://boards.greenhouse.io/stripe/1"],
            evidence={
                "title": "Senior Data Engineer at Stripe",
                "salary": "Base salary $180,000 - $240,000 USD",
            },
        )
        hn_rec = make_record(
            posting_id="hn_1",
            source_urls=["https://news.ycombinator.com/item?id=2"],
            evidence={
                "salary": "HN compensation snippet: 150-200k",
                "remote": "Remote anywhere in US",
            },
        )

        canonical = merger.merge([gh_rec, hn_rec])

        assert canonical.evidence["title"] == "Senior Data Engineer at Stripe"
        # Salary from Greenhouse wins
        assert canonical.evidence["salary"] == "Base salary $180,000 - $240,000 USD"
        # Remote from HN is kept since Greenhouse had no remote snippet
        assert canonical.evidence["remote"] == "Remote anywhere in US"

    def test_timestamps_min_first_seen_max_last_seen(self, merger: RecordMerger):
        """first_seen is min across records; last_seen is max across records."""
        t1 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 10, 10, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 1, 5, 10, 0, 0, tzinfo=timezone.utc)
        t4 = datetime(2026, 1, 25, 10, 0, 0, tzinfo=timezone.utc)

        rec1 = make_record(posting_id="r1", first_seen=t1, last_seen=t2)
        rec2 = make_record(posting_id="r2", first_seen=t3, last_seen=t4)

        canonical = merger.merge([rec1, rec2])

        assert canonical.first_seen == t1
        assert canonical.last_seen == t4

    def test_single_record_merger(self, merger: RecordMerger):
        """Merging a single record produces a valid CanonicalJobPosting with duplicate_count=1."""
        rec = make_record(
            posting_id="solo_1",
            company="Databricks",
            title="Lead Data Architect",
            city="San Francisco",
            country="US",
            skills=["Spark", "Delta Lake"],
        )

        canonical = merger.merge([rec])

        assert canonical.duplicate_count == 1
        assert canonical.company == "Databricks"
        assert canonical.title == "Lead Data Architect"
        assert canonical.source_posting_ids == ["solo_1"]
        assert len(canonical.canonical_id) == 64

    def test_empty_cluster_raises_value_error(self, merger: RecordMerger):
        """Merging an empty cluster raises ValueError."""
        with pytest.raises(ValueError, match="Cannot merge an empty cluster"):
            merger.merge([])


class TestCanonicalJobPostingModel:
    """Tests model validation, hash reproducibility, and serialization."""

    def test_deterministic_canonical_id_sha256(self):
        """canonical_id is exactly 64 hexadecimal characters and deterministic across runs."""
        c_id1 = generate_canonical_id("Stripe", "Senior Data Engineer", "San Francisco, US")
        c_id2 = generate_canonical_id("stripe", "senior data engineer", "san francisco, us")
        c_id3 = generate_canonical_id("Stripe ", " Senior Data Engineer ", "San Francisco, US ")

        assert len(c_id1) == 64
        assert c_id1 == c_id2 == c_id3
        # Direct hash verification
        expected = hashlib.sha256(b"stripe::senior data engineer::san francisco, us").hexdigest()
        assert c_id1 == expected

    def test_format_skill_acronyms_and_title_casing(self):
        """Known acronyms are preserved; general skills are Title-cased."""
        assert format_skill("sql") == "SQL"
        assert format_skill("dbt") == "dbt"
        assert format_skill("aws") == "AWS"
        assert format_skill("gcp") == "GCP"
        assert format_skill("python") == "Python"
        assert format_skill("pYtHoN") == "Python"
        assert format_skill("apache spark") == "Apache Spark"

    def test_model_serialization(self, merger: RecordMerger):
        """CanonicalJobPosting serializes cleanly to dict and JSON."""
        rec = make_record(posting_id="ser_1")
        canonical = merger.merge([rec])

        d = canonical.to_dict()
        assert isinstance(d, dict)
        assert d["canonical_id"] == canonical.canonical_id
        assert d["duplicate_count"] == 1

        json_str = canonical.to_json()
        assert isinstance(json_str, str)
        assert canonical.canonical_id in json_str

    def test_extra_fields_forbidden(self):
        """Model forbids extra unrecognized fields."""
        c_id = generate_canonical_id("Stripe", "Data Engineer")
        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            CanonicalJobPosting(
                canonical_id=c_id,
                company="Stripe",
                title="Data Engineer",
                first_seen=now,
                last_seen=now,
                duplicate_count=1,
                hallucinated_field="forbidden",
            )
