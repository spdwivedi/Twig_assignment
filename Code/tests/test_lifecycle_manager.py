"""Unit and integration test suite for LifecycleManager.

Tests:
- Detection of disappeared job postings after fresh crawl.
- Transition of status from 'open' to 'closed' in SQLite database.
- Logging of 'posting_closed' audit events in posting_change_history.
- Re-opening detection: when a previously closed job reappears in a subsequent crawl,
  status transitions back to 'open' with 'posting_reopened' audit entries.
- Multi-source resilience: canonical record remains 'open' if at least one underlying active
  source is still present.
- Edge cases: all jobs still active (0 closures), brand new database (0 false closures).
- Schema compatibility across both 'posting_id' and 'canonical_id' column variations.
- Isolation between different scraper sources.
- Calling patterns (instance method vs class method).
- Change history retrieval and lifecycle summary metrics.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Set

import pytest

from pipeline.storage.lifecycle_manager import LifecycleManager


# ==============================================================================
# Pytest Fixtures & Helpers
# ==============================================================================


@pytest.fixture
def memory_db() -> sqlite3.Connection:
    """Provides a fresh in-memory SQLite connection with the canonical schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(
        """
        CREATE TABLE canonical_job_postings (
            posting_id TEXT PRIMARY KEY,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            seniority TEXT,
            city TEXT,
            country TEXT,
            remote_policy TEXT,
            salary_min REAL,
            salary_max REAL,
            currency TEXT,
            employment_type TEXT,
            skills TEXT,
            visa_sponsorship BOOLEAN,
            posted_date DATE,
            status TEXT DEFAULT 'open',
            sources TEXT NOT NULL,
            source_urls TEXT NOT NULL,
            source_posting_ids TEXT NOT NULL,
            evidence TEXT NOT NULL,
            first_seen TIMESTAMP NOT NULL,
            last_seen TIMESTAMP NOT NULL,
            duplicate_count INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return conn


@pytest.fixture
def canonical_id_schema_db() -> sqlite3.Connection:
    """Provides an in-memory SQLite connection using 'canonical_id' as primary key."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(
        """
        CREATE TABLE canonical_job_postings (
            canonical_id TEXT PRIMARY KEY,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            seniority TEXT,
            city TEXT,
            country TEXT,
            remote_policy TEXT,
            salary_min REAL,
            salary_max REAL,
            currency TEXT,
            employment_type TEXT,
            skills TEXT,
            visa_sponsorship BOOLEAN,
            posted_date DATE,
            status TEXT DEFAULT 'open',
            sources TEXT NOT NULL,
            source_urls TEXT NOT NULL,
            source_posting_ids TEXT NOT NULL,
            evidence TEXT NOT NULL,
            first_seen TIMESTAMP NOT NULL,
            last_seen TIMESTAMP NOT NULL,
            duplicate_count INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return conn


def _insert_posting(
    conn: sqlite3.Connection,
    posting_id: str,
    company: str = "Acme Corp",
    title: str = "Senior Data Engineer",
    sources: list[str] | None = None,
    source_posting_ids: list[str] | None = None,
    status: str = "open",
    first_seen: str = "2026-11-01T00:00:00Z",
    last_seen: str = "2026-11-01T00:00:00Z",
    id_col: str = "posting_id",
) -> None:
    """Helper to insert a test job posting into canonical_job_postings."""
    sources_val = json.dumps(sources or ["greenhouse"])
    s_ids_val = json.dumps(source_posting_ids or [posting_id])
    conn.execute(
        f"""
        INSERT INTO canonical_job_postings (
            {id_col}, company, title, seniority, city, country,
            remote_policy, salary_min, salary_max, currency,
            employment_type, skills, visa_sponsorship, posted_date,
            status, sources, source_urls, source_posting_ids, evidence,
            first_seen, last_seen, duplicate_count
        ) VALUES (
            ?, ?, ?, 'senior', 'San Francisco', 'US',
            'remote', 160000, 200000, 'USD',
            'full_time', '["Python", "SQL"]', 1, '2026-11-01',
            ?, ?, '["https://example.com/job"]', ?, '{{}}',
            ?, ?, 1
        )
        """,
        (
            posting_id,
            company,
            title,
            status,
            sources_val,
            s_ids_val,
            first_seen,
            last_seen,
        ),
    )
    conn.commit()


# ==============================================================================
# Test Cases
# ==============================================================================


class TestLifecycleManagerClosureDetection:
    """Tests for detect_closed_postings."""

    def test_detect_closed_postings_single_disappeared(self, memory_db: sqlite3.Connection) -> None:
        """When a job disappears from a fresh crawl, its status becomes 'closed' and audit log is written."""
        manager = LifecycleManager()

        # Insert 3 open greenhouse postings
        _insert_posting(memory_db, "gh_001", sources=["greenhouse"], source_posting_ids=["gh_001"])
        _insert_posting(memory_db, "gh_002", sources=["greenhouse"], source_posting_ids=["gh_002"])
        _insert_posting(memory_db, "gh_003", sources=["greenhouse"], source_posting_ids=["gh_003"])

        # Fresh crawl only returns gh_001 and gh_002. gh_003 has disappeared!
        active_ids: Set[str] = {"gh_001", "gh_002"}
        fixed_now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)

        closed = manager.detect_closed_postings(
            memory_db,
            source_name="greenhouse",
            active_source_posting_ids=active_ids,
            current_timestamp=fixed_now,
        )

        assert len(closed) == 1
        assert closed[0]["posting_id"] == "gh_003"
        assert closed[0]["status"] == "closed"
        assert closed[0]["previous_status"] == "open"
        assert closed[0]["reason"] == "posting_closed"
        assert closed[0]["closed_at"] == fixed_now.isoformat()

        # Verify DB status for all rows
        cursor = memory_db.execute("SELECT posting_id, status, last_seen FROM canonical_job_postings ORDER BY posting_id")
        rows = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}
        assert rows["gh_001"][0] == "open"
        assert rows["gh_002"][0] == "open"
        assert rows["gh_003"][0] == "closed"
        assert rows["gh_003"][1] == fixed_now.isoformat()

        # Verify posting_change_history audit log
        history = manager.get_change_history(memory_db, posting_id="gh_003")
        status_events = [h for h in history if h["field_name"] == "status"]
        assert len(status_events) == 1
        assert status_events[0]["old_value"] == "open"
        assert status_events[0]["new_value"] == "closed"
        assert status_events[0]["reason"] == "posting_closed"
        assert status_events[0]["detected_at"] == fixed_now.isoformat()

    def test_detect_closed_postings_multiple_closures(self, memory_db: sqlite3.Connection) -> None:
        """Multiple disappeared postings are all transitioned to closed in one run."""
        manager = LifecycleManager()

        for i in range(1, 6):
            _insert_posting(memory_db, f"gh_{i:03d}", sources=["greenhouse"])

        # Only gh_001 is active; gh_002 through gh_005 disappeared
        active_ids = {"gh_001"}
        closed = manager.detect_closed_postings(memory_db, "greenhouse", active_ids)

        assert len(closed) == 4
        closed_ids = {c["posting_id"] for c in closed}
        assert closed_ids == {"gh_002", "gh_003", "gh_004", "gh_005"}

        # Verify DB status
        cursor = memory_db.execute("SELECT COUNT(*) FROM canonical_job_postings WHERE status = 'closed'")
        assert cursor.fetchone()[0] == 4

        cursor = memory_db.execute("SELECT COUNT(*) FROM canonical_job_postings WHERE status = 'open'")
        assert cursor.fetchone()[0] == 1

    def test_detect_closed_postings_idempotency(self, memory_db: sqlite3.Connection) -> None:
        """Running closure detection multiple times consecutively is strictly idempotent."""
        manager = LifecycleManager()

        _insert_posting(memory_db, "job_1", sources=["greenhouse"])
        _insert_posting(memory_db, "job_2", sources=["greenhouse"])

        # First run: job_2 disappears
        closed_first = manager.detect_closed_postings(memory_db, "greenhouse", {"job_1"})
        assert len(closed_first) == 1
        assert closed_first[0]["posting_id"] == "job_2"

        # Second run with same active set: job_2 is already closed, so 0 closures detected
        closed_second = manager.detect_closed_postings(memory_db, "greenhouse", {"job_1"})
        assert closed_second == []

        # Audit history must NOT contain duplicate 'posting_closed' entries
        history = manager.get_change_history(memory_db, posting_id="job_2")
        status_changes = [h for h in history if h["field_name"] == "status" and h["reason"] == "posting_closed"]
        assert len(status_changes) == 1

    def test_detect_closed_postings_edge_case_all_active(self, memory_db: sqlite3.Connection) -> None:
        """When all jobs remain active, 0 closures are reported and 0 audit records are logged."""
        manager = LifecycleManager()

        _insert_posting(memory_db, "job_a", sources=["lever"])
        _insert_posting(memory_db, "job_b", sources=["lever"])

        closed = manager.detect_closed_postings(memory_db, "lever", {"job_a", "job_b"})
        assert closed == []

        cursor = memory_db.execute("SELECT COUNT(*) FROM canonical_job_postings WHERE status = 'open'")
        assert cursor.fetchone()[0] == 2

        # No status change in history
        history = manager.get_change_history(memory_db)
        assert len([h for h in history if h["field_name"] == "status"]) == 0

    def test_detect_closed_postings_brand_new_empty_db(self) -> None:
        """Brand new database without tables handles closure detection gracefully without raising errors."""
        conn = sqlite3.connect(":memory:")
        manager = LifecycleManager()

        closed = manager.detect_closed_postings(conn, "greenhouse", {"gh_101"})
        assert closed == []

    def test_detect_closed_postings_source_isolation(self, memory_db: sqlite3.Connection) -> None:
        """Closure detection for source A never affects active postings from source B."""
        manager = LifecycleManager()

        _insert_posting(memory_db, "gh_job", sources=["greenhouse"])
        _insert_posting(memory_db, "lever_job", sources=["lever"])
        _insert_posting(memory_db, "hn_job", sources=["hackernews"])

        # Greenhouse crawl runs; gh_job has disappeared
        closed = manager.detect_closed_postings(memory_db, "greenhouse", set())
        assert len(closed) == 1
        assert closed[0]["posting_id"] == "gh_job"

        # lever_job and hn_job must remain 'open'
        cursor = memory_db.execute("SELECT posting_id, status FROM canonical_job_postings WHERE posting_id != 'gh_job'")
        rows = {r[0]: r[1] for r in cursor.fetchall()}
        assert rows["lever_job"] == "open"
        assert rows["hn_job"] == "open"


class TestLifecycleManagerReopeningDetection:
    """Tests for detect_reopened_postings."""

    def test_reopen_closed_posting_by_posting_id(self, memory_db: sqlite3.Connection) -> None:
        """A closed job that reappears transitions back to 'open' with 'posting_reopened' audit entry."""
        manager = LifecycleManager()

        _insert_posting(memory_db, "job_closed", status="closed")
        fixed_now = datetime(2026, 11, 19, 14, 30, 0, tzinfo=timezone.utc)

        # Detect reopening
        reopened = manager.detect_reopened_postings(
            memory_db,
            incoming_posting_id="job_closed",
            current_timestamp=fixed_now,
        )
        assert reopened is True

        # Check DB status
        cursor = memory_db.execute("SELECT status, last_seen FROM canonical_job_postings WHERE posting_id = 'job_closed'")
        row = cursor.fetchone()
        assert row[0] == "open"
        assert row[1] == fixed_now.isoformat()

        # Check audit log
        history = manager.get_change_history(memory_db, posting_id="job_closed")
        reopened_events = [h for h in history if h["reason"] == "posting_reopened"]
        assert len(reopened_events) == 1
        assert reopened_events[0]["field_name"] == "status"
        assert reopened_events[0]["old_value"] == "closed"
        assert reopened_events[0]["new_value"] == "open"
        assert reopened_events[0]["detected_at"] == fixed_now.isoformat()

    def test_reopen_closed_posting_by_source_posting_id(self, memory_db: sqlite3.Connection) -> None:
        """Reopening works when matching by raw source posting ID inside source_posting_ids array."""
        manager = LifecycleManager()

        _insert_posting(
            memory_db,
            "canon_hash_12345",
            sources=["greenhouse"],
            source_posting_ids=["raw_gh_job_999"],
            status="closed",
        )

        # Incoming crawl provides raw_gh_job_999
        reopened = manager.detect_reopened_postings(memory_db, "raw_gh_job_999")
        assert reopened is True

        # Verify canonical entity is now open
        cursor = memory_db.execute(
            "SELECT status FROM canonical_job_postings WHERE posting_id = 'canon_hash_12345'"
        )
        assert cursor.fetchone()[0] == "open"

        history = manager.get_change_history(memory_db, posting_id="canon_hash_12345")
        assert any(h["reason"] == "posting_reopened" for h in history)

    def test_reopen_already_open_posting_returns_false(self, memory_db: sqlite3.Connection) -> None:
        """Calling detect_reopened_postings on an already-open posting returns False."""
        manager = LifecycleManager()

        _insert_posting(memory_db, "job_active", status="open")

        reopened = manager.detect_reopened_postings(memory_db, "job_active")
        assert reopened is False

        # Status remains open
        cursor = memory_db.execute("SELECT status FROM canonical_job_postings WHERE posting_id = 'job_active'")
        assert cursor.fetchone()[0] == "open"

        # No audit entry written
        history = manager.get_change_history(memory_db, posting_id="job_active")
        assert len(history) == 0

    def test_reopen_nonexistent_posting_returns_false(self, memory_db: sqlite3.Connection) -> None:
        """Calling detect_reopened_postings on a non-existent ID returns False."""
        manager = LifecycleManager()
        reopened = manager.detect_reopened_postings(memory_db, "does_not_exist")
        assert reopened is False

    def test_reopen_brand_new_empty_db(self) -> None:
        """Brand new database without tables returns False safely."""
        conn = sqlite3.connect(":memory:")
        manager = LifecycleManager()
        assert manager.detect_reopened_postings(conn, "job_any") is False


class TestMultiSourceResilience:
    """Tests for multi-source canonical records (e.g. Greenhouse + Hacker News)."""

    def test_multi_source_record_remains_open_if_one_source_active(self, memory_db: sqlite3.Connection) -> None:
        """Canonical record remains open when one source drops the job, provided another source is still active."""
        manager = LifecycleManager()

        # Multi-source job found on both Greenhouse and Hacker News
        _insert_posting(
            memory_db,
            "multi_job_1",
            company="Stripe",
            title="Infrastructure Engineer",
            sources=["greenhouse", "hackernews"],
            source_posting_ids=["stripe_gh_42", "hn_comment_84"],
            status="open",
        )

        # 1. Greenhouse crawl runs, but stripe_gh_42 is NOT in the active crawl
        active_gh_ids = {"stripe_gh_99"}
        closed = manager.detect_closed_postings(memory_db, "greenhouse", active_gh_ids)

        # Multi-source resilience: canonical record remains OPEN because Hacker News is still active
        assert closed == []

        cursor = memory_db.execute("SELECT status FROM canonical_job_postings WHERE posting_id = 'multi_job_1'")
        assert cursor.fetchone()[0] == "open"

        # Source-level deprecation is recorded in change history
        history = manager.get_change_history(memory_db, posting_id="multi_job_1")
        source_events = [h for h in history if h["field_name"] == "source:greenhouse"]
        assert len(source_events) == 1
        assert source_events[0]["old_value"] == "active"
        assert source_events[0]["new_value"] == "inactive"
        assert source_events[0]["reason"] == "source_closed"

        # Check active sources tracking
        active_sources = manager.get_active_sources_for_posting(memory_db, "multi_job_1")
        assert active_sources == {"hackernews"}

    def test_multi_source_record_closes_when_all_linked_sources_disappear(self, memory_db: sqlite3.Connection) -> None:
        """Canonical record transitions to 'closed' only when ALL linked sources no longer observe the job."""
        manager = LifecycleManager()

        _insert_posting(
            memory_db,
            "multi_job_2",
            sources=["greenhouse", "hackernews"],
            source_posting_ids=["gh_10", "hn_20"],
            status="open",
        )

        # 1. Greenhouse crawl runs; job missing from Greenhouse
        manager.detect_closed_postings(memory_db, "greenhouse", {"gh_other"})
        # Remains open
        cursor = memory_db.execute("SELECT status FROM canonical_job_postings WHERE posting_id = 'multi_job_2'")
        assert cursor.fetchone()[0] == "open"

        # 2. Hacker News crawl runs; job missing from Hacker News as well
        closed_hn = manager.detect_closed_postings(memory_db, "hackernews", {"hn_other"})

        # NOW all linked sources are inactive -> overall canonical record is marked CLOSED
        assert len(closed_hn) == 1
        assert closed_hn[0]["posting_id"] == "multi_job_2"
        assert closed_hn[0]["status"] == "closed"

        cursor = memory_db.execute("SELECT status FROM canonical_job_postings WHERE posting_id = 'multi_job_2'")
        assert cursor.fetchone()[0] == "closed"

        # Change history contains both source deprecations and the final status: open -> closed
        history = manager.get_change_history(memory_db, posting_id="multi_job_2")
        status_events = [h for h in history if h["field_name"] == "status"]
        assert len(status_events) == 1
        assert status_events[0]["old_value"] == "open"
        assert status_events[0]["new_value"] == "closed"
        assert status_events[0]["reason"] == "posting_closed"

    def test_multi_source_reopened_when_job_reappears(self, memory_db: sqlite3.Connection) -> None:
        """When a multi-source posting was closed, reappearing in a crawl transitions it back to open."""
        manager = LifecycleManager()

        _insert_posting(
            memory_db,
            "multi_job_3",
            sources=["greenhouse", "hackernews"],
            source_posting_ids=["gh_30", "hn_30"],
            status="closed",
        )

        # Job reappears on Greenhouse
        reopened = manager.detect_reopened_postings(memory_db, "gh_30")
        assert reopened is True

        cursor = memory_db.execute("SELECT status FROM canonical_job_postings WHERE posting_id = 'multi_job_3'")
        assert cursor.fetchone()[0] == "open"


class TestSchemaCompatibilityAndCallingPatterns:
    """Tests for schema flexibility ('canonical_id' column) and call conventions."""

    def test_canonical_id_column_schema_compatibility(self, canonical_id_schema_db: sqlite3.Connection) -> None:
        """Works seamlessly when primary key column is named 'canonical_id' instead of 'posting_id'."""
        manager = LifecycleManager()

        _insert_posting(
            canonical_id_schema_db,
            "canon_sha256_abcdef",
            sources=["lever"],
            id_col="canonical_id",
            status="open",
        )

        # 1. Closure detection
        closed = manager.detect_closed_postings(canonical_id_schema_db, "lever", set())
        assert len(closed) == 1
        assert closed[0]["posting_id"] == "canon_sha256_abcdef"
        assert closed[0]["status"] == "closed"

        cursor = canonical_id_schema_db.execute(
            "SELECT status FROM canonical_job_postings WHERE canonical_id = 'canon_sha256_abcdef'"
        )
        assert cursor.fetchone()[0] == "closed"

        # 2. Reopening detection
        reopened = manager.detect_reopened_postings(canonical_id_schema_db, "canon_sha256_abcdef")
        assert reopened is True

        cursor = canonical_id_schema_db.execute(
            "SELECT status FROM canonical_job_postings WHERE canonical_id = 'canon_sha256_abcdef'"
        )
        assert cursor.fetchone()[0] == "open"

    def test_class_method_and_instance_method_calling_conventions(self, memory_db: sqlite3.Connection) -> None:
        """LifecycleManager methods can be called as either instance methods or class methods."""
        _insert_posting(memory_db, "job_call_test", sources=["greenhouse"])

        # Call detect_closed_postings directly on class
        closed = LifecycleManager.detect_closed_postings(
            memory_db,
            "greenhouse",
            set(),
        )
        assert len(closed) == 1
        assert closed[0]["posting_id"] == "job_call_test"

        # Call detect_reopened_postings directly on class
        reopened = LifecycleManager.detect_reopened_postings(
            memory_db,
            "job_call_test",
        )
        assert reopened is True

    def test_lifecycle_metrics_utility(self, memory_db: sqlite3.Connection) -> None:
        """LifecycleManager.get_lifecycle_metrics returns accurate summary metrics."""
        manager = LifecycleManager()

        _insert_posting(memory_db, "m_job_1", sources=["greenhouse"], status="open")
        _insert_posting(memory_db, "m_job_2", sources=["greenhouse"], status="open")
        _insert_posting(memory_db, "m_job_3", sources=["greenhouse"], status="closed")

        metrics_before = manager.get_lifecycle_metrics(memory_db)
        assert metrics_before["total_canonical"] == 3
        assert metrics_before["open_postings"] == 2
        assert metrics_before["closed_postings"] == 1

        # Close m_job_2
        manager.detect_closed_postings(memory_db, "greenhouse", {"m_job_1"})

        metrics_after = manager.get_lifecycle_metrics(memory_db)
        assert metrics_after["open_postings"] == 1
        assert metrics_after["closed_postings"] == 2
        assert metrics_after["closure_events"] == 1

        # Reopen m_job_3
        manager.detect_reopened_postings(memory_db, "m_job_3")

        metrics_final = manager.get_lifecycle_metrics(memory_db)
        assert metrics_final["open_postings"] == 2
        assert metrics_final["closed_postings"] == 1
        assert metrics_final["reopened_events"] == 1
