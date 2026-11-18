"""Comprehensive Test Suite for SQLite Database Schema and Engine Architecture.

Tests cover:
- SQLite connection creation and PRAGMA settings verification (WAL, synchronous, foreign keys, busy timeout).
- Schema initialization across all 3 tables and 5 indexes.
- Foreign key constraint enforcement (posting_change_history -> canonical_job_postings).
- CHECK constraint validations (status, seniority, remote_policy, employment_type).
- In-memory and persistent disk database roundtrip operations.
- Thread-safe DatabaseManager connection pooling and transaction rollback semantics.
"""

import concurrent.futures
import json
import sqlite3
import threading
from pathlib import Path
from typing import Generator

import pytest

from pipeline.storage.database import (
    DatabaseManager,
    create_connection,
    get_connection,
    get_db_connection,
    get_index_names,
    get_table_columns,
    get_table_names,
    init_db,
    verify_schema,
    EXPECTED_INDICES,
    EXPECTED_TABLES,
)


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def memory_db() -> Generator[sqlite3.Connection, None, None]:
    """Yields an initialized in-memory database connection."""
    conn = init_db(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def disk_db(tmp_path: Path) -> Generator[Path, None, None]:
    """Yields path to a temporary persistent database file on disk."""
    db_file = tmp_path / "test_pipeline.db"
    yield db_file
    if db_file.exists():
        try:
            db_file.unlink()
        except Exception:
            pass


@pytest.fixture
def sample_canonical_job() -> dict:
    """Returns valid sample dictionary for canonical_job_postings insertion."""
    return {
        "posting_id": "sha256_mock_job_posting_001_abcdef1234567890",
        "company": "Stripe",
        "title": "Senior Data Engineer",
        "seniority": "senior",
        "city": "San Francisco",
        "country": "US",
        "remote_policy": "hybrid",
        "salary_min": 185000.0,
        "salary_max": 240000.0,
        "currency": "USD",
        "employment_type": "full_time",
        "skills": json.dumps(["Python", "SQL", "Spark", "Kafka"]),
        "visa_sponsorship": 1,
        "posted_date": "2026-02-15",
        "status": "open",
        "sources": json.dumps(["greenhouse", "hackernews"]),
        "source_urls": json.dumps(["https://boards.greenhouse.io/stripe/jobs/12345"]),
        "source_posting_ids": json.dumps(["gh_12345", "hn_9999"]),
        "evidence": json.dumps({"salary": "$185,000 - $240,000 base salary"}),
        "first_seen": "2026-02-15 10:00:00",
        "last_seen": "2026-02-16 12:00:00",
        "duplicate_count": 2,
    }


# ==============================================================================
# 1. Connection & PRAGMA Verification Tests
# ==============================================================================


class TestConnectionPragmas:
    """Verifies SQLite connection configuration and PRAGMA settings."""

    def test_in_memory_pragmas(self, memory_db: sqlite3.Connection) -> None:
        """Verifies foreign_keys, busy_timeout, synchronous, and journal_mode on :memory:."""
        cursor = memory_db.cursor()

        # Foreign keys enabled
        fk = cursor.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert fk == 1, "PRAGMA foreign_keys must be ON (1)"

        # Busy timeout set to 5000ms
        timeout = cursor.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert timeout == 5000, "PRAGMA busy_timeout must be 5000ms"

        # Synchronous set to NORMAL (1)
        sync = cursor.execute("PRAGMA synchronous;").fetchone()[0]
        assert sync == 1, "PRAGMA synchronous must be NORMAL (1)"

        # Row factory produces Row objects
        cursor.execute("SELECT 1 AS num, 'val' AS str_col")
        row = cursor.fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["num"] == 1
        assert row["str_col"] == "val"
        assert row[0] == 1
        assert row[1] == "val"

    def test_disk_pragmas(self, disk_db: Path) -> None:
        """Verifies WAL journal mode and PRAGMAs on a persistent disk database."""
        conn = init_db(disk_db)
        cursor = conn.cursor()

        # WAL mode active on persistent disk database
        journal_mode = cursor.execute("PRAGMA journal_mode;").fetchone()[0]
        assert journal_mode.lower() == "wal", f"Expected WAL journal mode, got {journal_mode}"

        # Foreign keys enabled
        fk = cursor.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert fk == 1

        # Synchronous NORMAL
        sync = cursor.execute("PRAGMA synchronous;").fetchone()[0]
        assert sync == 1

        # Busy timeout 5000ms
        timeout = cursor.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert timeout == 5000

        conn.close()

    def test_alias_get_connection(self) -> None:
        """Verifies that create_connection and get_connection both function identically."""
        conn1 = create_connection(":memory:")
        conn2 = get_connection(":memory:")
        try:
            assert conn1.execute("PRAGMA foreign_keys;").fetchone()[0] == 1
            assert conn2.execute("PRAGMA foreign_keys;").fetchone()[0] == 1
        finally:
            conn1.close()
            conn2.close()


# ==============================================================================
# 2. Schema DDL & Table / Index Creation Tests
# ==============================================================================


class TestSchemaDDL:
    """Verifies schema table definitions, column types, and indices."""

    def test_all_expected_tables_exist(self, memory_db: sqlite3.Connection) -> None:
        """Verifies that all 3 specified tables exist in the initialized database."""
        tables = get_table_names(memory_db)
        for expected in EXPECTED_TABLES:
            assert expected in tables, f"Table '{expected}' must exist"

    def test_all_expected_indices_exist(self, memory_db: sqlite3.Connection) -> None:
        """Verifies that all 5 specified indices exist."""
        indices = get_index_names(memory_db)
        for expected_idx in EXPECTED_INDICES:
            assert expected_idx in indices, f"Index '{expected_idx}' must exist"

    def test_verify_schema_helper(self, memory_db: sqlite3.Connection) -> None:
        """Verifies verify_schema helper reports valid status and empty missing lists."""
        report = verify_schema(memory_db)
        assert report["valid"] is True
        assert len(report["missing_tables"]) == 0
        assert len(report["missing_indices"]) == 0

    def test_raw_ingestion_cache_columns(self, memory_db: sqlite3.Connection) -> None:
        """Verifies column names, primary keys, and nullabilities for raw_ingestion_cache."""
        cols = {c["name"]: c for c in get_table_columns(memory_db, "raw_ingestion_cache")}
        assert "cache_id" in cols
        assert cols["cache_id"]["pk"] == 1
        assert cols["cache_id"]["type"].upper() == "TEXT"

        assert "source" in cols
        assert cols["source"]["notnull"] == 1
        assert cols["source"]["type"].upper() == "TEXT"

        assert "source_url" in cols
        assert cols["source_url"]["notnull"] == 1
        assert cols["source_url"]["type"].upper() == "TEXT"

        assert "payload" in cols
        assert cols["payload"]["notnull"] == 1
        assert cols["payload"]["type"].upper() == "TEXT"

        assert "fetched_at" in cols
        assert "CURRENT_TIMESTAMP" in str(cols["fetched_at"]["dflt_value"])

    def test_canonical_job_postings_columns(self, memory_db: sqlite3.Connection) -> None:
        """Verifies column definitions for canonical_job_postings."""
        cols = {c["name"]: c for c in get_table_columns(memory_db, "canonical_job_postings")}

        assert cols["posting_id"]["pk"] == 1
        assert cols["company"]["notnull"] == 1
        assert cols["title"]["notnull"] == 1
        assert cols["sources"]["notnull"] == 1
        assert cols["source_urls"]["notnull"] == 1
        assert cols["source_posting_ids"]["notnull"] == 1
        assert cols["evidence"]["notnull"] == 1
        assert cols["first_seen"]["notnull"] == 1
        assert cols["last_seen"]["notnull"] == 1

        # Check default status
        assert "'open'" in str(cols["status"]["dflt_value"])

    def test_posting_change_history_columns(self, memory_db: sqlite3.Connection) -> None:
        """Verifies column definitions for posting_change_history."""
        cols = {c["name"]: c for c in get_table_columns(memory_db, "posting_change_history")}

        assert cols["history_id"]["pk"] == 1
        assert cols["posting_id"]["notnull"] == 1
        assert cols["field_name"]["notnull"] == 1
        assert "CURRENT_TIMESTAMP" in str(cols["detected_at"]["dflt_value"])


# ==============================================================================
# 3. Foreign Key Constraint Enforcement Tests
# ==============================================================================


class TestForeignKeyConstraints:
    """Verifies foreign key enforcement between posting_change_history and canonical_job_postings."""

    def test_foreign_key_rejects_nonexistent_parent(self, memory_db: sqlite3.Connection) -> None:
        """Inserting into posting_change_history with non-existent posting_id must raise IntegrityError."""
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            memory_db.execute(
                """
                INSERT INTO posting_change_history (posting_id, field_name, old_value, new_value, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("non_existent_posting_123", "status", "open", "closed", "posting_closed"),
            )
        assert "FOREIGN KEY constraint failed" in str(exc_info.value)

    def test_foreign_key_accepts_valid_parent(
        self,
        memory_db: sqlite3.Connection,
        sample_canonical_job: dict,
    ) -> None:
        """Inserting into posting_change_history succeeds when posting_id exists in canonical_job_postings."""
        # Insert parent
        keys = ", ".join(sample_canonical_job.keys())
        placeholders = ", ".join("?" for _ in sample_canonical_job)
        memory_db.execute(
            f"INSERT INTO canonical_job_postings ({keys}) VALUES ({placeholders})",
            list(sample_canonical_job.values()),
        )

        # Insert child
        cursor = memory_db.execute(
            """
            INSERT INTO posting_change_history (posting_id, field_name, old_value, new_value, reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                sample_canonical_job["posting_id"],
                "salary_max",
                "240000.0",
                "260000.0",
                "compensation_updated",
            ),
        )
        assert cursor.lastrowid is not None
        assert cursor.lastrowid >= 1

        # Query child
        row = memory_db.execute(
            "SELECT * FROM posting_change_history WHERE posting_id = ?",
            (sample_canonical_job["posting_id"],),
        ).fetchone()
        assert row is not None
        assert row["field_name"] == "salary_max"
        assert row["old_value"] == "240000.0"
        assert row["new_value"] == "260000.0"
        assert row["reason"] == "compensation_updated"


# ==============================================================================
# 4. CHECK Constraint Validation Tests
# ==============================================================================


class TestCheckConstraints:
    """Verifies CHECK constraints on seniority, remote_policy, employment_type, and status."""

    def _insert_with_override(
        self,
        conn: sqlite3.Connection,
        base: dict,
        col: str,
        val: object,
        posting_id_suffix: str = "check",
    ) -> sqlite3.Cursor:
        rec = dict(base)
        rec["posting_id"] = f"{rec['posting_id']}_{posting_id_suffix}"
        rec[col] = val
        keys = ", ".join(rec.keys())
        placeholders = ", ".join("?" for _ in rec)
        return conn.execute(
            f"INSERT INTO canonical_job_postings ({keys}) VALUES ({placeholders})",
            list(rec.values()),
        )

    # --- Seniority CHECK ---
    @pytest.mark.parametrize("valid_seniority", ["junior", "mid", "senior", "staff_plus", None])
    def test_seniority_valid_values(
        self,
        memory_db: sqlite3.Connection,
        sample_canonical_job: dict,
        valid_seniority: object,
    ) -> None:
        """Valid seniority values (including NULL) must be accepted."""
        suffix = str(valid_seniority) if valid_seniority else "null"
        cursor = self._insert_with_override(
            memory_db, sample_canonical_job, "seniority", valid_seniority, suffix
        )
        assert cursor.rowcount == 1

    @pytest.mark.parametrize("invalid_seniority", ["executive", "intern", "lead", "principal", ""])
    def test_seniority_invalid_values(
        self,
        memory_db: sqlite3.Connection,
        sample_canonical_job: dict,
        invalid_seniority: str,
    ) -> None:
        """Invalid seniority values must fail the CHECK constraint."""
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            self._insert_with_override(
                memory_db, sample_canonical_job, "seniority", invalid_seniority, "inv"
            )
        assert "CHECK constraint failed" in str(exc_info.value)

    # --- Remote Policy CHECK ---
    @pytest.mark.parametrize("valid_remote", ["onsite", "hybrid", "remote", None])
    def test_remote_policy_valid_values(
        self,
        memory_db: sqlite3.Connection,
        sample_canonical_job: dict,
        valid_remote: object,
    ) -> None:
        """Valid remote_policy values (including NULL) must be accepted."""
        suffix = str(valid_remote) if valid_remote else "null"
        cursor = self._insert_with_override(
            memory_db, sample_canonical_job, "remote_policy", valid_remote, f"remote_{suffix}"
        )
        assert cursor.rowcount == 1

    @pytest.mark.parametrize("invalid_remote", ["wfh", "telecommute", "flexible", "in-office"])
    def test_remote_policy_invalid_values(
        self,
        memory_db: sqlite3.Connection,
        sample_canonical_job: dict,
        invalid_remote: str,
    ) -> None:
        """Invalid remote_policy values must fail the CHECK constraint."""
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            self._insert_with_override(
                memory_db, sample_canonical_job, "remote_policy", invalid_remote, "inv_rem"
            )
        assert "CHECK constraint failed" in str(exc_info.value)

    # --- Employment Type CHECK ---
    @pytest.mark.parametrize("valid_emp_type", ["full_time", "contract", "intern", None])
    def test_employment_type_valid_values(
        self,
        memory_db: sqlite3.Connection,
        sample_canonical_job: dict,
        valid_emp_type: object,
    ) -> None:
        """Valid employment_type values (including NULL) must be accepted."""
        suffix = str(valid_emp_type) if valid_emp_type else "null"
        cursor = self._insert_with_override(
            memory_db, sample_canonical_job, "employment_type", valid_emp_type, f"emp_{suffix}"
        )
        assert cursor.rowcount == 1

    @pytest.mark.parametrize("invalid_emp_type", ["part_time", "freelance", "permanent", "temp"])
    def test_employment_type_invalid_values(
        self,
        memory_db: sqlite3.Connection,
        sample_canonical_job: dict,
        invalid_emp_type: str,
    ) -> None:
        """Invalid employment_type values must fail the CHECK constraint."""
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            self._insert_with_override(
                memory_db, sample_canonical_job, "employment_type", invalid_emp_type, "inv_emp"
            )
        assert "CHECK constraint failed" in str(exc_info.value)

    # --- Status CHECK ---
    @pytest.mark.parametrize("valid_status", ["open", "closed"])
    def test_status_valid_values(
        self,
        memory_db: sqlite3.Connection,
        sample_canonical_job: dict,
        valid_status: str,
    ) -> None:
        """Status can only be 'open' or 'closed'."""
        cursor = self._insert_with_override(
            memory_db, sample_canonical_job, "status", valid_status, f"stat_{valid_status}"
        )
        assert cursor.rowcount == 1

    @pytest.mark.parametrize("invalid_status", ["archived", "pending", "deleted", "draft", "in_review"])
    def test_status_invalid_values(
        self,
        memory_db: sqlite3.Connection,
        sample_canonical_job: dict,
        invalid_status: str,
    ) -> None:
        """Status cannot be arbitrary invalid strings outside ('open', 'closed')."""
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            self._insert_with_override(
                memory_db, sample_canonical_job, "status", invalid_status, "inv_stat"
            )
        assert "CHECK constraint failed" in str(exc_info.value)

    def test_status_default_value(
        self,
        memory_db: sqlite3.Connection,
        sample_canonical_job: dict,
    ) -> None:
        """If status is omitted from INSERT, it defaults to 'open'."""
        rec = dict(sample_canonical_job)
        del rec["status"]
        rec["posting_id"] = "test_default_status_id"

        keys = ", ".join(rec.keys())
        placeholders = ", ".join("?" for _ in rec)
        memory_db.execute(
            f"INSERT INTO canonical_job_postings ({keys}) VALUES ({placeholders})",
            list(rec.values()),
        )

        row = memory_db.execute(
            "SELECT status FROM canonical_job_postings WHERE posting_id = ?",
            ("test_default_status_id",),
        ).fetchone()
        assert row["status"] == "open"


# ==============================================================================
# 5. In-Memory & Persistent Disk Roundtrip Tests
# ==============================================================================


class TestDatabaseRoundtrip:
    """Verifies complete insert, update, query, and persistence across disk lifecycles."""

    def test_raw_cache_roundtrip(self, memory_db: sqlite3.Connection) -> None:
        """Verifies raw_ingestion_cache insert and query roundtrip."""
        memory_db.execute(
            """
            INSERT INTO raw_ingestion_cache (cache_id, source, source_url, payload)
            VALUES (?, ?, ?, ?)
            """,
            (
                "hash_abc_123",
                "greenhouse",
                "https://boards.greenhouse.io/stripe/jobs/101",
                '{"title": "Staff Engineer", "dept": "Infra"}',
            ),
        )

        row = memory_db.execute(
            "SELECT * FROM raw_ingestion_cache WHERE cache_id = ?",
            ("hash_abc_123",),
        ).fetchone()

        assert row is not None
        assert row["cache_id"] == "hash_abc_123"
        assert row["source"] == "greenhouse"
        assert row["source_url"] == "https://boards.greenhouse.io/stripe/jobs/101"
        assert json.loads(row["payload"])["title"] == "Staff Engineer"
        assert row["fetched_at"] is not None

    def test_disk_database_persistence_roundtrip(
        self,
        disk_db: Path,
        sample_canonical_job: dict,
    ) -> None:
        """Inserts data on disk, closes connection, re-opens new connection, and verifies persistence."""
        # 1. Initialize and write
        conn1 = init_db(disk_db)
        keys = ", ".join(sample_canonical_job.keys())
        placeholders = ", ".join("?" for _ in sample_canonical_job)
        conn1.execute(
            f"INSERT INTO canonical_job_postings ({keys}) VALUES ({placeholders})",
            list(sample_canonical_job.values()),
        )
        conn1.execute(
            """
            INSERT INTO posting_change_history (posting_id, field_name, old_value, new_value, reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sample_canonical_job["posting_id"], "status", "open", "closed", "posting_closed"),
        )
        conn1.commit()
        conn1.close()

        # 2. Re-open brand new connection to the file
        conn2 = create_connection(disk_db)
        try:
            # Query canonical posting
            post_row = conn2.execute(
                "SELECT * FROM canonical_job_postings WHERE posting_id = ?",
                (sample_canonical_job["posting_id"],),
            ).fetchone()
            assert post_row is not None
            assert post_row["company"] == "Stripe"
            assert post_row["title"] == "Senior Data Engineer"
            assert post_row["salary_min"] == 185000.0
            assert post_row["salary_max"] == 240000.0
            assert json.loads(post_row["skills"]) == ["Python", "SQL", "Spark", "Kafka"]

            # Query change history
            hist_row = conn2.execute(
                "SELECT * FROM posting_change_history WHERE posting_id = ?",
                (sample_canonical_job["posting_id"],),
            ).fetchone()
            assert hist_row is not None
            assert hist_row["field_name"] == "status"
            assert hist_row["new_value"] == "closed"
            assert hist_row["reason"] == "posting_closed"
        finally:
            conn2.close()


# ==============================================================================
# 6. DatabaseManager, Pooling & Thread-Safety Tests
# ==============================================================================


class TestDatabaseManagerAndConcurrency:
    """Verifies DatabaseManager connection pool, transaction semantics, and concurrent safety."""

    def test_database_manager_in_memory(self, sample_canonical_job: dict) -> None:
        """Verifies DatabaseManager retains state in :memory: across multiple checkout blocks."""
        with DatabaseManager(":memory:") as db:
            # Block 1: insert
            with db.get_connection() as conn:
                keys = ", ".join(sample_canonical_job.keys())
                placeholders = ", ".join("?" for _ in sample_canonical_job)
                conn.execute(
                    f"INSERT INTO canonical_job_postings ({keys}) VALUES ({placeholders})",
                    list(sample_canonical_job.values()),
                )

            # Block 2: query in separate checkout
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT company, title FROM canonical_job_postings WHERE posting_id = ?",
                    (sample_canonical_job["posting_id"],),
                ).fetchone()
                assert row is not None
                assert row["company"] == "Stripe"

    def test_database_manager_helpers(self, sample_canonical_job: dict) -> None:
        """Verifies DatabaseManager execute, executemany, fetchone, fetchall helpers."""
        with DatabaseManager(":memory:") as db:
            keys = ", ".join(sample_canonical_job.keys())
            placeholders = ", ".join("?" for _ in sample_canonical_job)
            db.execute(
                f"INSERT INTO canonical_job_postings ({keys}) VALUES ({placeholders})",
                list(sample_canonical_job.values()),
            )

            # fetchone
            row = db.fetchone(
                "SELECT company, salary_max FROM canonical_job_postings WHERE posting_id = ?",
                (sample_canonical_job["posting_id"],),
            )
            assert row is not None
            assert row["company"] == "Stripe"
            assert row["salary_max"] == 240000.0

            # executemany history rows
            history_rows = [
                (sample_canonical_job["posting_id"], "salary_min", "170k", "185k", "comp_update"),
                (sample_canonical_job["posting_id"], "remote_policy", "onsite", "hybrid", "remote_update"),
            ]
            db.executemany(
                """
                INSERT INTO posting_change_history (posting_id, field_name, old_value, new_value, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                history_rows,
            )

            # fetchall
            all_hist = db.fetchall(
                "SELECT field_name FROM posting_change_history WHERE posting_id = ? ORDER BY history_id",
                (sample_canonical_job["posting_id"],),
            )
            assert len(all_hist) == 2
            assert [r["field_name"] for r in all_hist] == ["salary_min", "remote_policy"]

    def test_transaction_rollback_on_error(self, disk_db: Path) -> None:
        """Verifies that unhandled exceptions inside get_connection rollback the transaction."""
        with DatabaseManager(disk_db) as db:
            with pytest.raises(ValueError):
                with db.get_connection(auto_commit=True) as conn:
                    conn.execute(
                        """
                        INSERT INTO raw_ingestion_cache (cache_id, source, source_url, payload)
                        VALUES ('cid_rollback', 'lever', 'http://example.com', '{}')
                        """
                    )
                    raise ValueError("Simulated unexpected pipeline error during batch")

            # Check that row was rolled back
            row = db.fetchone("SELECT * FROM raw_ingestion_cache WHERE cache_id = 'cid_rollback'")
            assert row is None

    def test_concurrent_multithreaded_access(self, disk_db: Path) -> None:
        """Verifies multi-threaded concurrent reads and writes across pooled WAL connections."""
        manager = DatabaseManager(disk_db, max_connections=5)
        num_threads = 10
        records_per_thread = 15

        def worker_task(thread_id: int) -> int:
            inserted = 0
            for i in range(records_per_thread):
                cache_id = f"worker_{thread_id}_item_{i}"
                with manager.get_connection() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO raw_ingestion_cache (cache_id, source, source_url, payload)
                        VALUES (?, ?, ?, ?)
                        """,
                        (cache_id, "thread_worker", f"http://worker/{thread_id}/{i}", f"payload_{i}"),
                    )
                    inserted += 1
            return inserted

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker_task, tid) for tid in range(num_threads)]
            results = [f.result() for f in futures]

        assert sum(results) == num_threads * records_per_thread

        # Verify all records exist in DB
        with manager.get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM raw_ingestion_cache WHERE source = 'thread_worker'"
            ).fetchone()[0]
            assert count == num_threads * records_per_thread

        manager.close()

    def test_standalone_get_db_connection_context_manager(self, disk_db: Path) -> None:
        """Verifies standalone get_db_connection context manager auto-commits and closes."""
        with get_db_connection(disk_db) as conn:
            init_db(conn=conn)
            conn.execute(
                """
                INSERT INTO raw_ingestion_cache (cache_id, source, source_url, payload)
                VALUES ('standalone_1', 'hn', 'http://news.ycombinator.com', 'test')
                """
            )

        # Re-open and verify
        with get_db_connection(disk_db) as conn2:
            row = conn2.execute(
                "SELECT payload FROM raw_ingestion_cache WHERE cache_id = 'standalone_1'"
            ).fetchone()
            assert row is not None
            assert row["payload"] == "test"
