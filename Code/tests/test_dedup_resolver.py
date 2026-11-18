"""Unit and integration tests for DeduplicationEngine, DisjointSetUnion, and CLI.

Verifies:
- Transitive matching via Union-Find (A matches B, B matches C -> unified cluster {A, B, C})
- Preservation of distinct concurrent roles (disjoint roles remain separate canonical entities)
- Cross-company isolation (identical titles at different companies remain distinct)
- Strict idempotency: re-running deduplication yields identical canonical IDs and counts
- SQLite persistence: saving canonical postings to `canonical_job_postings` table and loading back
- End-to-end resolution with custom injected components or built-in defaults
- CLI execution and dry-run mode
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pytest

from pipeline.dedup.cli import run_dedup
from pipeline.dedup.resolver import (
    DeduplicationEngine,
    DefaultCandidateBlocker,
    DefaultCompanyNormalizer,
    DefaultJobPostingMatcher,
    DefaultTitleParser,
    DisjointSetUnion,
)
from pipeline.models.canonical import CanonicalJobPosting
from pipeline.models.schema import (
    EmploymentTypeEnum,
    JobPostingRecord,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
)


# ==============================================================================
# Helper Factories
# ==============================================================================


def create_record(
    posting_id: str,
    company: str,
    title: str,
    seniority: SeniorityEnum = None,
    city: str = None,
    country: str = "US",
    remote_policy: RemotePolicyEnum = None,
    salary_min: float = None,
    salary_max: float = None,
    currency: str = None,
    skills: List[str] = None,
    source_urls: List[str] = None,
    evidence: dict = None,
    first_seen: datetime = None,
    last_seen: datetime = None,
) -> JobPostingRecord:
    now = datetime(2026, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
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
        employment_type=EmploymentTypeEnum.full_time,
        skills=skills or ["Python"],
        visa_sponsorship=None,
        source_urls=source_urls or [f"https://example.com/jobs/{posting_id}"],
        evidence=evidence or {"title": f"{title} at {company}"},
        first_seen=first_seen or now,
        last_seen=last_seen or now,
        status=StatusEnum.open,
    )


# ==============================================================================
# DSU Tests
# ==============================================================================


class TestDisjointSetUnion:
    """Tests the Disjoint Set Union (Union-Find) algorithmic primitives."""

    def test_dsu_basic_operations(self):
        dsu = DisjointSetUnion()
        assert dsu.find("a") == "a"
        assert dsu.find("b") == "b"

        merged = dsu.union("a", "b")
        assert merged is True
        assert dsu.find("a") == dsu.find("b")

        # Union already unified items
        assert dsu.union("a", "b") is False

    def test_dsu_transitivity(self):
        """A=B and B=C yields root(A) == root(B) == root(C)."""
        dsu = DisjointSetUnion()
        dsu.union("item_1", "item_2")
        dsu.union("item_2", "item_3")

        root1 = dsu.find("item_1")
        root2 = dsu.find("item_2")
        root3 = dsu.find("item_3")

        assert root1 == root2 == root3
        clusters = dsu.get_clusters(["item_1", "item_2", "item_3", "item_4"])
        # Should be 2 clusters: {1, 2, 3} and {4}
        assert len(clusters) == 2
        cluster_sets = [set(c) for c in clusters]
        assert {"item_1", "item_2", "item_3"} in cluster_sets
        assert {"item_4"} in cluster_sets


# ==============================================================================
# Deduplication Engine Tests
# ==============================================================================


class TestDeduplicationEngineResolution:
    """Tests full clustering and entity resolution in DeduplicationEngine."""

    @pytest.fixture
    def engine(self, tmp_path: Path) -> DeduplicationEngine:
        db_file = tmp_path / "test_pipeline.db"
        return DeduplicationEngine(db_path=str(db_file))

    def test_transitive_matching_unification(self, engine: DeduplicationEngine):
        """A matches B, and B matches C -> all three unify into 1 CanonicalJobPosting (duplicate_count=3)."""
        # Posting A: Greenhouse listing for Stripe Senior Data Engineer
        rec_a = create_record(
            posting_id="gh_stripe_1",
            company="Stripe",
            title="Senior Data Engineer",
            seniority=SeniorityEnum.senior,
            city="San Francisco",
            remote_policy=RemotePolicyEnum.hybrid,
            salary_min=180000.0,
            salary_max=240000.0,
            currency="USD",
            skills=["Python", "SQL", "dbt"],
            source_urls=["https://boards.greenhouse.io/stripe/jobs/111"],
        )
        # Posting B: Direct Careers page referencing same requisition or title
        rec_b = create_record(
            posting_id="car_stripe_1",
            company="Stripe, Inc.",
            title="Senior Data Engineer - Data Platform",
            seniority=SeniorityEnum.senior,
            city="San Francisco",
            remote_policy=RemotePolicyEnum.hybrid,
            salary_min=None,
            salary_max=None,
            skills=["Python", "Spark", "Kafka"],
            source_urls=["https://stripe.com/jobs/111"],
        )
        # Posting C: Hacker News submission referencing the role
        rec_c = create_record(
            posting_id="hn_stripe_1",
            company="stripe",
            title="Senior Data Engineer",
            seniority=SeniorityEnum.senior,
            city="San Francisco",
            remote_policy=RemotePolicyEnum.hybrid,
            salary_min=170000.0,
            salary_max=230000.0,
            currency="USD",
            skills=["Python", "AWS"],
            source_urls=["https://news.ycombinator.com/item?id=777"],
        )

        canonical_list = engine.resolve([rec_a, rec_b, rec_c])

        assert len(canonical_list) == 1
        canonical = canonical_list[0]
        assert canonical.company == "Stripe"
        assert canonical.title == "Senior Data Engineer"
        assert canonical.duplicate_count == 3
        assert set(canonical.source_posting_ids) == {"gh_stripe_1", "car_stripe_1", "hn_stripe_1"}
        # Greenhouse salary took precedence
        assert canonical.salary_min == 180000.0
        assert canonical.salary_max == 240000.0
        # Skills union across all three records
        assert "AWS" in canonical.skills
        assert "dbt" in canonical.skills
        assert "Kafka" in canonical.skills
        assert "Python" in canonical.skills

    def test_disjoint_roles_remain_separate(self, engine: DeduplicationEngine):
        """Distinct concurrent roles at the same company must NOT merge."""
        # 1. Senior Data Engineer vs Staff Data Engineer
        senior_rec = create_record(
            posting_id="stripe_sr",
            company="Stripe",
            title="Senior Data Engineer",
            seniority=SeniorityEnum.senior,
            source_urls=["https://boards.greenhouse.io/stripe/jobs/1"],
        )
        staff_rec = create_record(
            posting_id="stripe_staff",
            company="Stripe",
            title="Staff Data Engineer",
            seniority=SeniorityEnum.staff_plus,
            source_urls=["https://boards.greenhouse.io/stripe/jobs/2"],
        )
        # 2. Data Engineer vs Machine Learning Engineer
        mle_rec = create_record(
            posting_id="stripe_mle",
            company="Stripe",
            title="Machine Learning Engineer",
            source_urls=["https://boards.greenhouse.io/stripe/jobs/3"],
        )

        canonical_list = engine.resolve([senior_rec, staff_rec, mle_rec])

        assert len(canonical_list) == 3
        titles = {c.title for c in canonical_list}
        assert "Senior Data Engineer" in titles
        assert "Staff Data Engineer" in titles
        assert "Machine Learning Engineer" in titles
        for c in canonical_list:
            assert c.duplicate_count == 1

    def test_cross_company_isolation(self, engine: DeduplicationEngine):
        """Identical titles across different companies must remain separate canonical entities."""
        stripe_rec = create_record(
            posting_id="p1",
            company="Stripe",
            title="Lead Data Architect",
            source_urls=["https://boards.greenhouse.io/stripe/1"],
        )
        databricks_rec = create_record(
            posting_id="p2",
            company="Databricks",
            title="Lead Data Architect",
            source_urls=["https://boards.greenhouse.io/databricks/2"],
        )

        canonical_list = engine.resolve([stripe_rec, databricks_rec])

        assert len(canonical_list) == 2
        companies = {c.company for c in canonical_list}
        assert companies == {"Stripe", "Databricks"}

    def test_strict_idempotency(self, engine: DeduplicationEngine):
        """Re-running deduplication on identical input records produces identical canonical IDs and counts."""
        rec1 = create_record(
            posting_id="idemp_1",
            company="Airbnb",
            title="Data Platform Engineer",
            city="San Francisco",
            source_urls=["https://boards.greenhouse.io/airbnb/100"],
        )
        rec2 = create_record(
            posting_id="idemp_2",
            company="Airbnb Inc",
            title="Data Platform Engineer",
            city="San Francisco",
            source_urls=["https://news.ycombinator.com/item?id=200"],
        )

        pass1 = engine.resolve([rec1, rec2])
        pass2 = engine.resolve([rec1, rec2])
        pass3 = engine.resolve([rec2, rec1])  # Permuted input order

        assert len(pass1) == len(pass2) == len(pass3) == 1
        c1 = pass1[0]
        c2 = pass2[0]
        c3 = pass3[0]

        assert c1.canonical_id == c2.canonical_id == c3.canonical_id
        assert c1.duplicate_count == c2.duplicate_count == c3.duplicate_count == 2
        assert c1.to_dict() == c2.to_dict() == c3.to_dict()

    def test_empty_postings_list_returns_empty(self, engine: DeduplicationEngine):
        """Passing an empty list to resolve returns an empty list without error."""
        assert engine.resolve([]) == []


# ==============================================================================
# SQLite Persistence Tests
# ==============================================================================


class TestSQLitePersistence:
    """Tests saving canonical records to SQLite and querying/loading them."""

    def test_save_and_load_roundtrip(self, tmp_path: Path):
        db_path = str(tmp_path / "persistence_test.db")
        engine = DeduplicationEngine(db_path=db_path)

        rec1 = create_record(
            posting_id="p_001",
            company="Databricks",
            title="Senior Spark Engineer",
            seniority=SeniorityEnum.senior,
            city="Mountain View",
            country="US",
            remote_policy=RemotePolicyEnum.onsite,
            salary_min=185000.0,
            salary_max=245000.0,
            currency="USD",
            skills=["Scala", "Spark", "Java"],
            source_urls=["https://boards.greenhouse.io/databricks/55"],
        )
        rec2 = create_record(
            posting_id="p_002",
            company="Databricks Inc",
            title="Senior Spark Engineer",
            seniority=SeniorityEnum.senior,
            city="Mountain View",
            country="US",
            skills=["Spark", "Delta Lake"],
            source_urls=["https://news.ycombinator.com/item?id=99"],
        )

        # 1. Resolve and Save
        canonical_postings = engine.resolve_and_save([rec1, rec2], db_path=db_path)
        assert len(canonical_postings) == 1
        original_canonical = canonical_postings[0]

        # 2. Inspect SQLite table directly
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM canonical_job_postings WHERE canonical_id=?", (original_canonical.canonical_id,))
        row = cursor.fetchone()
        assert row is not None
        conn.close()

        # 3. Load from SQLite via engine
        loaded_postings = engine.load_from_db(db_path=db_path)
        assert len(loaded_postings) == 1
        loaded = loaded_postings[0]

        assert loaded.canonical_id == original_canonical.canonical_id
        assert loaded.company == original_canonical.company
        assert loaded.title == original_canonical.title
        assert loaded.salary_min == original_canonical.salary_min
        assert loaded.salary_max == original_canonical.salary_max
        assert loaded.duplicate_count == 2
        assert set(loaded.sources) == {"greenhouse", "hackernews"}
        assert "Spark" in loaded.skills


# ==============================================================================
# CLI and Orchestration Tests
# ==============================================================================


class TestCLIRunner:
    """Tests the run_dedup function and CLI entrypoint."""

    def test_run_dedup_dry_run_with_memory_records(self, tmp_path: Path):
        db_path = str(tmp_path / "cli_test.db")
        rec = create_record(
            posting_id="cli_1",
            company="Figma",
            title="Analytics Engineer",
            city="San Francisco",
        )

        results = run_dedup(
            postings=[rec],
            db_path=db_path,
            dry_run=True,
        )

        assert len(results) == 1
        assert results[0].company == "Figma"
        # Since dry-run was True, DB file should not have canonical_job_postings table
        if Path(db_path).exists():
            conn = sqlite3.connect(db_path)
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            assert "canonical_job_postings" not in tables
            conn.close()
