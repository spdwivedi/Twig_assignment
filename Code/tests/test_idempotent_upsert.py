"""Comprehensive Test Suite for IdempotentStorageEngine and Upsert Logic.

Tests cover:
1. Clean insertion of canonical job postings on first run.
2. Consecutive re-runs with identical data:
   - 0 duplicates created (table row count unchanged)
   - 0 false change logs generated
   - observation timestamps (last_seen) refreshed
3. Re-run with modified compensation:
   - Updates record in canonical_job_postings
   - Logs exact diff to posting_change_history
4. Re-run with new skill added:
   - Updates record in canonical_job_postings
   - Logs exact diff to posting_change_history
5. Multi-threaded and high-volume batch safety:
   - Handles 100+ records in a single atomic database transaction
6. Verification method:
   - verify_idempotency asserts all invariants across two consecutive runs
7. Transactional integrity and rollback on failures
8. Multi-source merging and duplicate count aggregation
"""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from pipeline.models.canonical import CanonicalJobPosting, generate_canonical_id
from pipeline.models.schema import (
    EmploymentTypeEnum,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
)
from pipeline.storage.change_tracker import ChangeTracker
from pipeline.storage.database import DatabaseManager, init_db
from pipeline.storage.idempotent_upsert import IdempotentStorageEngine
from pipeline.storage.lifecycle_manager import LifecycleManager


# ==============================================================================
# Pytest Fixtures
# ==============================================================================


@pytest.fixture
def memory_db() -> sqlite3.Connection:
    """Yields a configured in-memory SQLite database connection."""
    conn = init_db(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def disk_db(tmp_path: Path) -> Path:
    """Yields a temporary SQLite database file path on disk."""
    db_file = tmp_path / "test_upsert.db"
    return db_file


@pytest.fixture
def storage_engine() -> IdempotentStorageEngine:
    """Returns an IdempotentStorageEngine configured with default dependencies."""
    return IdempotentStorageEngine()


def make_canonical_posting(
    company: str = "Stripe",
    title: str = "Staff Data Engineer",
    city: str = "San Francisco",
    country: str = "US",
    remote_policy: RemotePolicyEnum = RemotePolicyEnum.hybrid,
    salary_min: float = 180000.0,
    salary_max: float = 240000.0,
    currency: str = "USD",
    skills: List[str] = None,
    sources: List[str] = None,
    duplicate_count: int = 2,
    status: StatusEnum = StatusEnum.open,
    first_seen: datetime = None,
    last_seen: datetime = None,
) -> CanonicalJobPosting:
    """Helper factory for creating valid CanonicalJobPosting instances."""
    cid = generate_canonical_id(company=company, title=title, location=city)
    now = datetime.now(timezone.utc)
    fs = first_seen or now
    ls = last_seen or now
    sk = skills if skills is not None else ["Python", "SQL", "Spark", "Kafka"]
    src = sources if sources is not None else ["greenhouse"]

    return CanonicalJobPosting(
        canonical_id=cid,
        company=company,
        title=title,
        seniority=SeniorityEnum.staff_plus,
        city=city,
        country=country,
        remote_policy=remote_policy,
        salary_min=salary_min,
        salary_max=salary_max,
        currency=currency,
        employment_type=EmploymentTypeEnum.full_time,
        skills=sk,
        visa_sponsorship=True,
        sources=src,
        source_urls=[f"https://careers.{company.lower()}.com/jobs/{cid[:8]}"],
        source_posting_ids=[f"src_{cid[:8]}"],
        evidence={"salary": f"${salary_min:,.0f} - ${salary_max:,.0f} base"},
        first_seen=fs,
        last_seen=ls,
        duplicate_count=duplicate_count,
        status=status,
    )


# ==============================================================================
# 1. Clean Initial Run Tests
# ==============================================================================


class TestInitialRunUpsert:
    """Verifies that the first execution inserts records cleanly without errors or false change logs."""

    def test_first_run_inserts_all_records_cleanly(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies initial batch insert of 5 distinct canonical job postings."""
        records = [
            make_canonical_posting(company="Stripe", title="Data Engineer", city="San Francisco"),
            make_canonical_posting(company="Airbnb", title="Data Architect", city="San Francisco"),
            make_canonical_posting(company="Databricks", title="Platform Engineer", city="Seattle"),
            make_canonical_posting(company="Figma", title="Analytics Engineer", city="New York"),
            make_canonical_posting(company="Palantir", title="Deployment Strategist", city="New York"),
        ]

        t0 = datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc)
        result = storage_engine.upsert_canonical_records(
            conn=memory_db,
            records=records,
            current_timestamp=t0,
        )

        assert result["inserted"] == 5
        assert result["updated"] == 0
        assert result["unchanged"] == 0
        assert result["changes_logged"] == 0

        # Verify rows in database
        cursor = memory_db.cursor()
        cursor.execute("SELECT COUNT(*) FROM canonical_job_postings")
        assert cursor.fetchone()[0] == 5

        # Verify zero change history rows generated
        cursor.execute("SELECT COUNT(*) FROM posting_change_history")
        assert cursor.fetchone()[0] == 0

        # Verify record contents and timestamps
        cursor.execute("SELECT * FROM canonical_job_postings WHERE company = 'Stripe'")
        row = cursor.fetchone()
        assert row is not None
        assert row["title"] == "Data Engineer"
        assert row["status"] == "open"
        assert row["first_seen"] == t0.isoformat()
        assert row["last_seen"] == t0.isoformat()
        assert row["duplicate_count"] == 2

    def test_empty_records_list(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies that passing an empty list safely returns zero counts without database mutation."""
        result = storage_engine.upsert_canonical_records(
            conn=memory_db,
            records=[],
        )
        assert result == {"inserted": 0, "updated": 0, "unchanged": 0, "changes_logged": 0}


# ==============================================================================
# 2. Strict Idempotency & Consecutive Re-run Tests
# ==============================================================================


class TestConsecutiveRerunsIdempotency:
    """Verifies that running consecutive passes with identical data creates 0 duplicates and 0 false change logs."""

    def test_consecutive_reruns_identical_data(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies Run 1 -> Run 2 -> Run 3 idempotency, zero false diffs, and last_seen updates."""
        records = [
            make_canonical_posting(company="Stripe", title="Staff Data Engineer"),
            make_canonical_posting(company="Databricks", title="Senior Data Engineer"),
            make_canonical_posting(company="Airbnb", title="Data Infrastructure Lead"),
        ]

        t1 = datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc)
        res1 = storage_engine.upsert_canonical_records(
            conn=memory_db,
            records=records,
            current_timestamp=t1,
        )
        assert res1["inserted"] == 3
        assert res1["updated"] == 0
        assert res1["unchanged"] == 0
        assert res1["changes_logged"] == 0

        # Verify Run 2 with same records at later timestamp t2
        t2 = datetime(2026, 2, 16, 12, 0, 0, tzinfo=timezone.utc)
        res2 = storage_engine.upsert_canonical_records(
            conn=memory_db,
            records=records,
            current_timestamp=t2,
        )

        assert res2["inserted"] == 0
        assert res2["updated"] == 0
        assert res2["unchanged"] == 3
        assert res2["changes_logged"] == 0

        # Verify zero duplicate rows created
        cursor = memory_db.cursor()
        cursor.execute("SELECT COUNT(*) FROM canonical_job_postings")
        assert cursor.fetchone()[0] == 3

        # Verify zero false change logs
        cursor.execute("SELECT COUNT(*) FROM posting_change_history")
        assert cursor.fetchone()[0] == 0

        # Verify last_seen was refreshed to t2 while first_seen remained t1
        cursor.execute("SELECT first_seen, last_seen FROM canonical_job_postings")
        for row in cursor.fetchall():
            assert row["first_seen"] == t1.isoformat()
            assert row["last_seen"] == t2.isoformat()

        # Run 3 at t3
        t3 = datetime(2026, 2, 17, 14, 0, 0, tzinfo=timezone.utc)
        res3 = storage_engine.upsert_canonical_records(
            conn=memory_db,
            records=records,
            current_timestamp=t3,
        )
        assert res3["inserted"] == 0
        assert res3["updated"] == 0
        assert res3["unchanged"] == 3
        assert res3["changes_logged"] == 0

    def test_verify_idempotency_helper_method(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies that engine.verify_idempotency() successfully validates 2 consecutive runs."""
        records = [
            make_canonical_posting(company="Plaid", title="Lead Data Engineer"),
            make_canonical_posting(company="GitLab", title="Backend Engineer"),
        ]

        is_idempotent = storage_engine.verify_idempotency(conn=memory_db, records=records)
        assert is_idempotent is True


# ==============================================================================
# 3. Modified Compensation & Field Diff Tests
# ==============================================================================


class TestModifiedCompensationUpdates:
    """Verifies that compensation changes update the canonical posting and log exact diffs."""

    def test_rerun_modified_compensation_logs_diff(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies salary modification is detected, logged, and updated."""
        initial_record = make_canonical_posting(
            company="Stripe",
            title="Senior Data Engineer",
            salary_min=150000.0,
            salary_max=200000.0,
        )

        t1 = datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc)
        storage_engine.upsert_canonical_records(
            conn=memory_db,
            records=[initial_record],
            current_timestamp=t1,
        )

        # Create updated record with new compensation range
        updated_record = make_canonical_posting(
            company="Stripe",
            title="Senior Data Engineer",
            salary_min=170000.0,
            salary_max=225000.0,
        )

        t2 = datetime(2026, 2, 16, 12, 0, 0, tzinfo=timezone.utc)
        res2 = storage_engine.upsert_canonical_records(
            conn=memory_db,
            records=[updated_record],
            current_timestamp=t2,
        )

        assert res2["inserted"] == 0
        assert res2["updated"] == 1
        assert res2["unchanged"] == 0
        assert res2["changes_logged"] == 2

        # Verify canonical posting in DB reflects new salary
        rec_in_db = storage_engine.get_canonical_record(memory_db, initial_record.canonical_id)
        assert rec_in_db is not None
        assert rec_in_db["salary_min"] == 170000.0
        assert rec_in_db["salary_max"] == 225000.0
        assert rec_in_db["last_seen"] == t2.isoformat()

        # Verify audit history
        history = storage_engine.get_change_history(memory_db, initial_record.canonical_id)
        assert len(history) == 2

        min_chg = next(h for h in history if h["field_name"] == "salary_min")
        assert min_chg["old_value"] == "150000.0"
        assert min_chg["new_value"] == "170000.0"
        assert min_chg["reason"] == "compensation_updated"

        max_chg = next(h for h in history if h["field_name"] == "salary_max")
        assert max_chg["old_value"] == "200000.0"
        assert max_chg["new_value"] == "225000.0"
        assert max_chg["reason"] == "compensation_updated"

    def test_salary_float_tolerance_no_false_diff(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies that trivial numeric differences (within tolerance) do not trigger false diffs."""
        rec1 = make_canonical_posting(company="Figma", title="Data Analyst", salary_min=100000.0, salary_max=130000.0)
        storage_engine.upsert_canonical_records(memory_db, [rec1])

        # Second record with sub-dollar variance
        rec2 = make_canonical_posting(company="Figma", title="Data Analyst", salary_min=100000.5, salary_max=130000.2)
        res = storage_engine.upsert_canonical_records(memory_db, [rec2])

        assert res["updated"] == 0
        assert res["unchanged"] == 1
        assert res["changes_logged"] == 0


# ==============================================================================
# 4. New Skill Added & Order Invariance Tests
# ==============================================================================


class TestSkillsModifications:
    """Verifies that skills updates trigger audit logging, while order rearrangements do not."""

    def test_rerun_new_skill_added_logs_diff(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies skill addition triggers skills_updated log entry."""
        rec1 = make_canonical_posting(
            company="Airbnb",
            title="Data Platform Engineer",
            skills=["Python", "SQL", "Spark"],
        )
        storage_engine.upsert_canonical_records(memory_db, [rec1])

        rec2 = make_canonical_posting(
            company="Airbnb",
            title="Data Platform Engineer",
            skills=["Python", "SQL", "Spark", "Snowflake", "dbt"],
        )
        res = storage_engine.upsert_canonical_records(memory_db, [rec2])

        assert res["inserted"] == 0
        assert res["updated"] == 1
        assert res["unchanged"] == 0
        assert res["changes_logged"] == 1

        history = storage_engine.get_change_history(memory_db, rec1.canonical_id)
        skill_chg = next(h for h in history if h["field_name"] == "skills")
        assert skill_chg["reason"] == "skills_updated"
        assert "Snowflake" in skill_chg["new_value"]
        assert "dbt" in skill_chg["new_value"]

    def test_skills_order_invariance_no_false_diff(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies that changing skills list ordering generates 0 false changes."""
        rec1 = make_canonical_posting(
            company="Databricks",
            title="ML Engineer",
            skills=["Python", "PyTorch", "Kubernetes"],
        )
        storage_engine.upsert_canonical_records(memory_db, [rec1])

        # Same skills in reversed ordering
        rec2 = make_canonical_posting(
            company="Databricks",
            title="ML Engineer",
            skills=["Kubernetes", "PyTorch", "Python"],
        )
        res = storage_engine.upsert_canonical_records(memory_db, [rec2])

        assert res["updated"] == 0
        assert res["unchanged"] == 1
        assert res["changes_logged"] == 0


# ==============================================================================
# 5. Remote Policy, Location & Lifecycle Status Transitions
# ==============================================================================


class TestOtherFieldTransitions:
    """Verifies transitions across remote policy, location, and status."""

    def test_remote_policy_transition(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies remote policy transition logs remote_status_changed."""
        rec1 = make_canonical_posting(company="Stripe", title="Security Data Lead", remote_policy=RemotePolicyEnum.onsite)
        storage_engine.upsert_canonical_records(memory_db, [rec1])

        rec2 = make_canonical_posting(company="Stripe", title="Security Data Lead", remote_policy=RemotePolicyEnum.remote)
        res = storage_engine.upsert_canonical_records(memory_db, [rec2])

        assert res["updated"] == 1
        assert res["changes_logged"] == 1

        history = storage_engine.get_change_history(memory_db, rec1.canonical_id)
        assert history[0]["field_name"] == "remote_policy"
        assert history[0]["old_value"] == "onsite"
        assert history[0]["new_value"] == "remote"
        assert history[0]["reason"] == "remote_status_changed"

    def test_status_lifecycle_transition(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies status change (open -> closed) logs status_changed."""
        rec1 = make_canonical_posting(company="Plaid", title="Senior ETL Engineer", status=StatusEnum.open)
        storage_engine.upsert_canonical_records(memory_db, [rec1])

        rec2 = make_canonical_posting(company="Plaid", title="Senior ETL Engineer", status=StatusEnum.closed)
        res = storage_engine.upsert_canonical_records(memory_db, [rec2])

        assert res["updated"] == 1
        assert res["changes_logged"] == 1

        history = storage_engine.get_change_history(memory_db, rec1.canonical_id)
        assert history[0]["field_name"] == "status"
        assert history[0]["old_value"] == "open"
        assert history[0]["new_value"] == "closed"
        assert history[0]["reason"] == "status_changed"


# ==============================================================================
# 6. Multi-Threaded & High-Volume Batch Safety (100+ Records)
# ==============================================================================


class TestBatchAndConcurrencySafety:
    """Verifies batch performance with 100+ records and multi-threaded safety."""

    def test_batch_100_records_single_transaction(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies atomic processing of 120 canonical records in a single database transaction."""
        companies = ["Stripe", "Airbnb", "Databricks", "Figma", "Plaid", "Palantir"]
        titles = [
            "Data Engineer", "Senior Data Engineer", "Lead Data Engineer",
            "Analytics Engineer", "Data Infrastructure Engineer",
        ]
        locations = ["San Francisco", "New York", "Seattle", "Austin"]

        records: List[CanonicalJobPosting] = []
        count = 0
        for comp in companies:
            for tit in titles:
                for loc in locations:
                    records.append(
                        make_canonical_posting(
                            company=comp,
                            title=f"{tit} #{count}",
                            city=loc,
                            salary_min=120000.0 + (count * 500),
                            salary_max=160000.0 + (count * 500),
                        )
                    )
                    count += 1

        assert len(records) == 120

        # Run 1: Batch insert all 120
        res1 = storage_engine.upsert_canonical_records(conn=memory_db, records=records)
        assert res1["inserted"] == 120
        assert res1["updated"] == 0
        assert res1["unchanged"] == 0
        assert res1["changes_logged"] == 0

        # Run 2: Consecutive rerun with identical 120
        res2 = storage_engine.upsert_canonical_records(conn=memory_db, records=records)
        assert res2["inserted"] == 0
        assert res2["updated"] == 0
        assert res2["unchanged"] == 120
        assert res2["changes_logged"] == 0

        # Run 3: Modify 30 records, keep 90 unchanged
        modified_records = list(records)
        for i in range(30):
            orig = records[i]
            modified_records[i] = make_canonical_posting(
                company=orig.company,
                title=orig.title,
                city=orig.city,
                salary_min=orig.salary_min + 15000.0,
                salary_max=orig.salary_max + 20000.0,
            )

        res3 = storage_engine.upsert_canonical_records(conn=memory_db, records=modified_records)
        assert res3["inserted"] == 0
        assert res3["updated"] == 30
        assert res3["unchanged"] == 90
        assert res3["changes_logged"] == 60  # 2 salary fields per modified record

        # Total row count remains strictly 120
        assert storage_engine.get_total_records_count(memory_db) == 120

    def test_multithreaded_upsert_safety_with_database_manager(
        self,
        disk_db: Path,
    ) -> None:
        """Verifies concurrent threads performing batch upserts into disk DB via DatabaseManager."""
        db_mgr = DatabaseManager(db_path=disk_db, max_connections=5)
        engine = IdempotentStorageEngine(database_manager=db_mgr)

        batches: List[List[CanonicalJobPosting]] = []
        for thread_id in range(5):
            batch = [
                make_canonical_posting(
                    company=f"CompanyThread{thread_id}",
                    title=f"Engineer {i}",
                    city="Remote",
                )
                for i in range(20)
            ]
            batches.append(batch)

        def worker_task(b: List[CanonicalJobPosting]) -> Dict[str, int]:
            return engine.upsert(records=b)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker_task, b) for b in batches]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        total_inserted = sum(r["inserted"] for r in results)
        assert total_inserted == 100

        # Verify idempotency rerun concurrently
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker_task, b) for b in batches]
            results2 = [f.result() for f in concurrent.futures.as_completed(futures)]

        total_inserted2 = sum(r["inserted"] for r in results2)
        total_unchanged2 = sum(r["unchanged"] for r in results2)
        assert total_inserted2 == 0
        assert total_unchanged2 == 100

        db_mgr.close()


# ==============================================================================
# 7. Multi-Source Provenance & Metadata Merging
# ==============================================================================


class TestMultiSourceMerging:
    """Verifies that multi-source canonical records merge provenance data properly."""

    def test_multisource_provenance_and_duplicate_count(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies that sources, source_urls, and duplicate_count merge upon update."""
        rec1 = make_canonical_posting(
            company="Stripe",
            title="Senior Pipeline Engineer",
            sources=["greenhouse"],
            duplicate_count=2,
            salary_min=160000.0,
        )
        storage_engine.upsert_canonical_records(memory_db, [rec1])

        # Re-run with new salary and additional Hacker News source
        rec2 = make_canonical_posting(
            company="Stripe",
            title="Senior Pipeline Engineer",
            sources=["hackernews"],
            duplicate_count=4,
            salary_min=175000.0,
        )
        res = storage_engine.upsert_canonical_records(memory_db, [rec2])
        assert res["updated"] == 1

        row = storage_engine.get_canonical_record(memory_db, rec1.canonical_id)
        assert row is not None
        assert row["duplicate_count"] == 4

        sources = json.loads(row["sources"])
        assert "greenhouse" in sources
        assert "hackernews" in sources


# ==============================================================================
# 8. Edge Cases, Transaction Rollbacks & Convenience Methods
# ==============================================================================


class TestEdgeCasesAndTransactionalIntegrity:
    """Verifies edge cases, rollbacks, dictionary inputs, and query helpers."""

    def test_location_city_and_country_transition(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies city and country transitions log location_updated."""
        rec1 = make_canonical_posting(company="GitLab", title="Infra Engineer", city="New York", country="US")
        storage_engine.upsert_canonical_records(memory_db, [rec1])

        # Keep canonical_id identical to simulate location update on existing posting
        data = rec1.model_dump()
        data["city"] = "San Francisco"
        res = storage_engine.upsert_canonical_records(memory_db, [data])

        assert res["updated"] == 1
        assert res["changes_logged"] == 1

        history = storage_engine.get_change_history(memory_db, rec1.canonical_id)
        assert history[0]["field_name"] == "city"
        assert history[0]["old_value"] == "New York"
        assert history[0]["new_value"] == "San Francisco"
        assert history[0]["reason"] == "location_updated"

    def test_currency_transition(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies currency transition logs currency_updated."""
        rec1 = make_canonical_posting(company="Cloudflare", title="Data Lead", currency="USD")
        storage_engine.upsert_canonical_records(memory_db, [rec1])

        rec2 = make_canonical_posting(company="Cloudflare", title="Data Lead", currency="EUR")
        res = storage_engine.upsert_canonical_records(memory_db, [rec2])

        assert res["updated"] == 1
        assert res["changes_logged"] == 1

        history = storage_engine.get_change_history(memory_db, rec1.canonical_id)
        assert history[0]["field_name"] == "currency"
        assert history[0]["old_value"] == "USD"
        assert history[0]["new_value"] == "EUR"
        assert history[0]["reason"] == "currency_updated"

    def test_title_update(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies title update logs title_updated."""
        rec1 = make_canonical_posting(company="Stripe", title="Data Engineer")
        storage_engine.upsert_canonical_records(memory_db, [rec1])

        # Use same canonical_id but updated title string
        data = rec1.model_dump()
        data["title"] = "Senior Data Engineer"
        res = storage_engine.upsert_canonical_records(memory_db, [data])

        assert res["updated"] == 1
        assert res["changes_logged"] == 1

        history = storage_engine.get_change_history(memory_db, rec1.canonical_id)
        assert history[0]["field_name"] == "title"
        assert history[0]["old_value"] == "Data Engineer"
        assert history[0]["new_value"] == "Senior Data Engineer"
        assert history[0]["reason"] == "title_updated"

    def test_upsert_with_raw_dictionaries(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies upsert works identically when passed plain dictionaries instead of Pydantic models."""
        cid = generate_canonical_id("Acme", "Analyst", "Austin")
        dict_rec = {
            "canonical_id": cid,
            "company": "Acme",
            "title": "Analyst",
            "city": "Austin",
            "country": "US",
            "salary_min": 90000.0,
            "salary_max": 120000.0,
            "skills": ["SQL", "Looker"],
            "status": "open",
        }

        res = storage_engine.upsert_canonical_records(memory_db, [dict_rec])
        assert res["inserted"] == 1

        rec = storage_engine.get_canonical_record(memory_db, cid)
        assert rec is not None
        assert rec["company"] == "Acme"
        assert rec["title"] == "Analyst"

    def test_transaction_rollback_on_error(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies atomic rollback when an exception occurs midway through a batch."""
        rec1 = make_canonical_posting(company="Stripe", title="Data Engineer 1")
        # Record with an invalid type or failing object that triggers an exception during iteration
        class CorruptedRecord:
            def model_dump(self):
                raise ValueError("Simulated parsing crash")

        with pytest.raises(ValueError, match="Simulated parsing crash"):
            storage_engine.upsert_canonical_records(memory_db, [rec1, CorruptedRecord()])

        # Ensure rec1 was not committed
        cursor = memory_db.cursor()
        cursor.execute("SELECT COUNT(*) FROM canonical_job_postings")
        assert cursor.fetchone()[0] == 0

    def test_verify_idempotency_raises_on_tampered_state(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verifies verify_idempotency raises AssertionError if Run 2 detects mutation."""
        rec = make_canonical_posting(company="Stripe", title="Data Engineer")

        # Monkeypatch upsert_canonical_records to pretend an update occurred on Run 2
        call_count = 0
        original_upsert = storage_engine.upsert_canonical_records

        def fake_upsert(conn, records, current_timestamp=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return {"inserted": 0, "updated": 1, "unchanged": 0, "changes_logged": 1}
            return original_upsert(conn, records, current_timestamp)

        monkeypatch.setattr(storage_engine, "upsert_canonical_records", fake_upsert)

        with pytest.raises(AssertionError, match="Idempotency violation"):
            storage_engine.verify_idempotency(memory_db, [rec])

    def test_get_all_canonical_records_and_limit(
        self,
        memory_db: sqlite3.Connection,
        storage_engine: IdempotentStorageEngine,
    ) -> None:
        """Verifies get_all_canonical_records returns ordered list with optional limit."""
        records = [
            make_canonical_posting(company="Alpha", title="Lead"),
            make_canonical_posting(company="Beta", title="Senior"),
            make_canonical_posting(company="Gamma", title="Staff"),
        ]
        storage_engine.upsert_canonical_records(memory_db, records)

        all_recs = storage_engine.get_all_canonical_records(memory_db)
        assert len(all_recs) == 3
        assert [r["company"] for r in all_recs] == ["Alpha", "Beta", "Gamma"]

        limited = storage_engine.get_all_canonical_records(memory_db, limit=2)
        assert len(limited) == 2

