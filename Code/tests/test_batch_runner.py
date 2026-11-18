"""Comprehensive unit and integration tests for Phase 2 BatchExtractor and CLI.

Tests:
1. Batch execution over sample postings list.
2. Fail-soft resilience when individual postings fail.
3. Concurrency handling without race conditions under multi-threading.
4. SQLite persistence of extracted records with idempotency.
5. Metrics report calculations and ASCII summary table formatting.
6. File and SQLite database loading helpers.
7. Standalone `run_extraction` pipeline entrypoint and CLI argument parsing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from pipeline.extractor.batch_runner import BatchExtractor, FallbackJobExtractor
from pipeline.extract import run_extraction, _build_cli_parser, main
from pipeline.models.schema import (
    EmploymentTypeEnum,
    JobPostingRecord,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
    generate_deterministic_id,
)


# ==============================================================================
# Fixtures & Sample Data
# ==============================================================================


@pytest.fixture
def sample_raw_postings() -> List[Dict[str, Any]]:
    """Return a standard list of candidate raw postings for testing."""
    return [
        {
            "company": "Stripe",
            "title": "Staff Data Engineer, Infrastructure",
            "url": "https://stripe.com/jobs/101",
            "content": (
                "Stripe is hiring a Staff Data Engineer in San Francisco, CA. "
                "Base salary range: $210,000 - $260,000 USD per year. "
                "Remote policy: Hybrid (2 days in office). "
                "Requirements: 7+ years with Python, Spark, and Snowflake."
            ),
            "source": "greenhouse",
        },
        {
            "company": "Databricks",
            "title": "Senior Machine Learning Engineer",
            "url": "https://databricks.com/jobs/202",
            "content": (
                "Databricks seeks a Senior Machine Learning Engineer. "
                "Location: Remote (US only). "
                "Compensation: $190,000 - $240,000 per year. "
                "Tech stack: PyTorch, MLflow, distributed training."
            ),
            "source": "greenhouse",
        },
        {
            "company": "Palantir",
            "title": "Forward Deployed Software Engineer - Data",
            "url": "https://palantir.com/jobs/303",
            "content": (
                "Palantir is seeking engineers in New York, NY (Onsite). "
                "Competitive salary and equity package. "
                "Skills: Python, TypeScript, SQL."
            ),
            "source": "lever",
        },
    ]


@pytest.fixture
def mock_job_posting_records() -> List[JobPostingRecord]:
    """Return a pre-constructed list of valid JobPostingRecord instances for metrics tests."""
    now = datetime.now(timezone.utc)
    return [
        JobPostingRecord(
            posting_id="rec_001",
            company="Stripe",
            title="Senior Data Engineer",
            seniority=SeniorityEnum.senior,
            city="San Francisco",
            country="US",
            remote_policy=RemotePolicyEnum.hybrid,
            salary_min=180000.0,
            salary_max=220000.0,
            currency="USD",
            employment_type=EmploymentTypeEnum.full_time,
            skills=["Python", "Spark"],
            visa_sponsorship=True,
            status=StatusEnum.open,
            source_urls=["https://stripe.com/jobs/001"],
            evidence={"salary": "$180,000 - $220,000", "remote_policy": "hybrid"},
            first_seen=now,
            last_seen=now,
        ),
        JobPostingRecord(
            posting_id="rec_002",
            company="Airbnb",
            title="Data Platform Engineer",
            seniority=SeniorityEnum.mid,
            city=None,
            country="US",
            remote_policy=RemotePolicyEnum.remote,
            salary_min=None,
            salary_max=None,
            currency=None,
            employment_type=EmploymentTypeEnum.full_time,
            skills=["SQL", "Trino"],
            visa_sponsorship=None,
            status=StatusEnum.open,
            source_urls=["https://airbnb.com/jobs/002"],
            evidence={"remote_policy": "Remote in US"},
            first_seen=now,
            last_seen=now,
        ),
        JobPostingRecord(
            posting_id="rec_003",
            company="Databricks",
            title="Lead ML Engineer",
            seniority=SeniorityEnum.staff_plus,
            city="Seattle",
            country="US",
            remote_policy=RemotePolicyEnum.onsite,
            salary_min=240000.0,
            salary_max=290000.0,
            currency="USD",
            employment_type=EmploymentTypeEnum.full_time,
            skills=["PyTorch", "Kubernetes"],
            visa_sponsorship=False,
            status=StatusEnum.open,
            source_urls=["https://databricks.com/jobs/003"],
            evidence={"salary": "$240,000 - $290,000"},
            first_seen=now,
            last_seen=now,
        ),
        JobPostingRecord(
            posting_id="rec_004",
            company="Plaid",
            title="Data Analyst",
            seniority=SeniorityEnum.junior,
            city="New York",
            country="US",
            remote_policy=None,  # Null remote policy for testing coverage
            salary_min=None,
            salary_max=None,
            currency=None,
            employment_type=EmploymentTypeEnum.full_time,
            skills=["dbt", "Looker"],
            visa_sponsorship=None,
            status=StatusEnum.open,
            source_urls=["https://plaid.com/jobs/004"],
            evidence={},  # Empty evidence for testing coverage
            first_seen=now,
            last_seen=now,
        ),
    ]


# ==============================================================================
# Unit Tests: BatchExtractor
# ==============================================================================


def test_batch_extractor_initialization():
    """Verify BatchExtractor initializes correctly with defaults and custom parameters."""
    runner = BatchExtractor(mock=True)
    assert runner.mock is True
    assert runner.total_processed == 0
    assert len(runner.successful_extractions) == 0
    assert len(runner.failed_extractions) == 0

    # Custom extractor
    custom_extractor = MagicMock()
    custom_runner = BatchExtractor(extractor=custom_extractor)
    assert custom_runner.extractor is custom_extractor


def test_batch_extractor_empty_postings():
    """Verify run_batch handles an empty input list gracefully."""
    runner = BatchExtractor(mock=True)
    results = runner.run_batch([])
    assert results == []
    assert runner.total_processed == 0


def test_batch_extractor_run_batch_basic(sample_raw_postings):
    """Verify batch extraction completes successfully on sample candidate postings."""
    runner = BatchExtractor(mock=True)
    results = runner.run_batch(sample_raw_postings, max_concurrency=2)

    assert len(results) == len(sample_raw_postings)
    assert runner.total_processed == len(sample_raw_postings)
    assert len(runner.successful_extractions) == len(sample_raw_postings)
    assert len(runner.failed_extractions) == 0

    for rec in results:
        assert isinstance(rec, JobPostingRecord)
        assert rec.company
        assert rec.title
        assert rec.posting_id
        assert rec.status == StatusEnum.open


def test_batch_extractor_fail_soft_resilience():
    """Verify that if one posting raises an error, the batch does not crash and processes the rest."""
    flaky_extractor = MagicMock()

    now = datetime.now(timezone.utc)
    good_record = JobPostingRecord(
        posting_id="good_01",
        company="Good Corp",
        title="Data Engineer",
        status=StatusEnum.open,
        first_seen=now,
        last_seen=now,
    )

    # 1st call succeeds, 2nd raises exception, 3rd succeeds
    flaky_extractor.extract_posting.side_effect = [
        good_record,
        ValueError("Simulated parsing crash on malformed posting"),
        good_record,
    ]

    runner = BatchExtractor(extractor=flaky_extractor)
    postings = [
        {"company": "Good Corp", "title": "DE 1"},
        {"company": "Bad Corp", "title": "Crash Role"},
        {"company": "Good Corp", "title": "DE 2"},
    ]

    results = runner.run_batch(postings, max_concurrency=1)

    # 2 succeeded, 1 failed
    assert len(results) == 2
    assert runner.total_processed == 3
    assert len(runner.successful_extractions) == 2
    assert len(runner.failed_extractions) == 1

    failure_info = runner.failed_extractions[0]
    assert failure_info["company"] == "Bad Corp"
    assert "Simulated parsing crash" in failure_info["error"]
    assert failure_info["exception_type"] == "ValueError"


def test_batch_extractor_concurrency_race_conditions():
    """Stress-test ThreadPool concurrency with 20 parallel items to verify zero race conditions."""
    call_lock = threading.Lock()
    call_count = 0

    class DelayedMockExtractor:
        def extract_posting(self, posting: Dict[str, Any]) -> JobPostingRecord:
            nonlocal call_count
            time.sleep(0.01)  # Trigger thread switching
            with call_lock:
                call_count += 1
            now = datetime.now(timezone.utc)
            return JobPostingRecord(
                posting_id=f"id_{posting['idx']}",
                company=f"Company_{posting['idx']}",
                title=f"Role_{posting['idx']}",
                status=StatusEnum.open,
                first_seen=now,
                last_seen=now,
            )

        def get_cost_summary(self) -> Dict[str, Any]:
            return {"total_cost_usd": 0.001, "total_prompt_tokens": 100, "total_completion_tokens": 50}

    extractor = DelayedMockExtractor()
    runner = BatchExtractor(extractor=extractor)

    items = [{"idx": i, "company": f"Company_{i}", "title": f"Role_{i}"} for i in range(20)]
    results = runner.run_batch(items, max_concurrency=8)

    assert len(results) == 20
    assert runner.total_processed == 20
    assert len(runner.successful_extractions) == 20
    assert len(runner.failed_extractions) == 0
    assert call_count == 20

    # Ensure all distinct IDs are captured
    unique_ids = {r.posting_id for r in results}
    assert len(unique_ids) == 20


def test_batch_extractor_save_extracted_records(tmp_path, mock_job_posting_records):
    """Verify persisting JobPostingRecord items into SQLite table `extracted_postings`."""
    db_file = tmp_path / "test_pipeline.db"
    runner = BatchExtractor(mock=True, db_path=str(db_file))

    # 1. Save records
    saved_count = runner.save_extracted_records(mock_job_posting_records)
    assert saved_count == len(mock_job_posting_records)

    # 2. Query and verify table schema and contents
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT count(*) FROM extracted_postings")
    row_count = cursor.fetchone()[0]
    assert row_count == len(mock_job_posting_records)

    cursor.execute("SELECT * FROM extracted_postings WHERE posting_id = 'rec_001'")
    r1 = cursor.fetchone()
    assert r1["company"] == "Stripe"
    assert r1["title"] == "Senior Data Engineer"
    assert r1["seniority"] == "senior"
    assert r1["salary_min"] == 180000.0
    assert r1["salary_max"] == 220000.0
    assert r1["currency"] == "USD"
    assert r1["remote_policy"] == "hybrid"
    assert r1["visa_sponsorship"] == 1

    # Verify JSON deserialization
    skills = json.loads(r1["skills"])
    assert skills == ["Python", "Spark"]
    evidence = json.loads(r1["evidence"])
    assert evidence["salary"] == "$180,000 - $220,000"

    # 3. Test idempotency: re-saving identical records must not duplicate rows
    saved_again = runner.save_extracted_records(mock_job_posting_records)
    assert saved_again == len(mock_job_posting_records)

    cursor.execute("SELECT count(*) FROM extracted_postings")
    assert cursor.fetchone()[0] == len(mock_job_posting_records)

    conn.close()


def test_batch_extractor_save_extracted_records_empty(tmp_path):
    """Verify saving an empty records list returns 0 without database error."""
    db_file = tmp_path / "empty.db"
    runner = BatchExtractor(mock=True, db_path=str(db_file))
    saved = runner.save_extracted_records([], db_path=str(db_file))
    assert saved == 0


def test_batch_extractor_metrics_report_accuracy(mock_job_posting_records):
    """Verify metrics calculation logic with exact ground truth percentages."""
    runner = BatchExtractor(mock=True)

    # Populate 4 successful extractions
    runner.successful_extractions = list(mock_job_posting_records)
    # Populate 1 failure
    runner.failed_extractions = [{"index": 99, "error": "timeout"}]
    runner.total_processed = 5
    runner.start_time = 100.0
    runner.end_time = 102.0

    report = runner.generate_metrics_report()

    # Total counts
    assert report["total_processed"] == 5
    assert report["total_successful"] == 4
    assert report["total_failed"] == 1
    assert report["success_rate_pct"] == 80.0

    # Field percentages:
    # 2 out of 4 have salary (rec_001, rec_003) -> 50.0%
    assert report["postings_with_salary"] == 2
    assert report["percentage_with_salary"] == 50.0

    # 3 out of 4 have remote_policy (rec_001, rec_002, rec_003) -> 75.0%
    assert report["postings_with_remote_policy"] == 3
    assert report["percentage_with_remote_policy"] == 75.0

    # 3 out of 4 have non-empty evidence (rec_001, rec_002, rec_003) -> 75.0%
    assert report["postings_with_verified_evidence"] == 3
    assert report["percentage_with_verified_evidence"] == 75.0

    # Throughput
    assert report["elapsed_seconds"] == 2.0
    assert report["postings_per_second"] == 2.5

    # ASCII table string verification
    ascii_table = runner.format_ascii_metrics_table(report)
    assert "Twig Phase 2 LLM Extraction Metrics Report" in ascii_table
    assert "50.00% (2/4)" in ascii_table
    assert "75.00% (3/4)" in ascii_table


def test_batch_extractor_load_postings_from_file(tmp_path):
    """Verify loading candidate postings from a JSON file."""
    test_file = tmp_path / "candidates.json"
    data = [
        {"company": "Acme", "title": "DE"},
        {"company": "Beta", "title": "MLE"},
    ]
    with test_file.open("w", encoding="utf-8") as f:
        json.dump(data, f)

    loaded = BatchExtractor.load_postings_from_file(test_file)
    assert len(loaded) == 2
    assert loaded[0]["company"] == "Acme"

    # Non-existent file
    with pytest.raises(FileNotFoundError):
        BatchExtractor.load_postings_from_file(tmp_path / "nonexistent.json")


def test_batch_extractor_load_postings_from_db(tmp_path):
    """Verify loading candidate postings from an SQLite raw_cache table."""
    db_file = tmp_path / "cache.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        CREATE TABLE raw_cache (
            cache_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            url TEXT NOT NULL,
            payload TEXT NOT NULL,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO raw_cache (cache_id, source, url, payload) VALUES (?, ?, ?, ?)",
        ("c1", "greenhouse", "https://gh.com/1", json.dumps({"title": "Role 1", "company": "Co 1"})),
    )
    conn.execute(
        "INSERT INTO raw_cache (cache_id, source, url, payload) VALUES (?, ?, ?, ?)",
        ("c2", "lever", "https://lv.com/2", json.dumps({"title": "Role 2", "company": "Co 2"})),
    )
    conn.commit()
    conn.close()

    # Load all
    all_postings = BatchExtractor.load_postings_from_db(db_path=str(db_file))
    assert len(all_postings) == 2

    # Load filtered by source
    gh_postings = BatchExtractor.load_postings_from_db(db_path=str(db_file), source="greenhouse")
    assert len(gh_postings) == 1
    assert gh_postings[0]["company"] == "Co 1"

    # Load with limit
    lim_postings = BatchExtractor.load_postings_from_db(db_path=str(db_file), limit=1)
    assert len(lim_postings) == 1


# ==============================================================================
# Unit Tests: CLI & run_extraction
# ==============================================================================


def test_run_extraction_with_in_memory_postings(tmp_path, sample_raw_postings):
    """Verify calling run_extraction directly with in-memory candidate postings."""
    db_file = tmp_path / "run_extract.db"
    out_json = tmp_path / "extracted.json"

    results = run_extraction(
        postings=sample_raw_postings,
        limit=2,
        mock=True,
        db_path=str(db_file),
        save_db=True,
        output_json=str(out_json),
    )

    assert len(results) == 2
    assert all(isinstance(r, JobPostingRecord) for r in results)

    # Verify JSON export
    assert out_json.is_file()
    with out_json.open("r", encoding="utf-8") as f:
        data = json.load(f)
    assert len(data) == 2
    assert data[0]["company"] == "Stripe"

    # Verify SQLite persistence
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) FROM extracted_postings")
    assert cursor.fetchone()[0] == 2
    conn.close()


def test_cli_parser():
    """Verify CLI argument parsing flags and default values."""
    parser = _build_cli_parser()

    # Test default values
    args = parser.parse_args([])
    assert args.source == "all"
    assert args.limit is None
    assert args.mock is False
    assert args.concurrency == 5
    assert args.no_save_db is False

    # Test custom flags
    custom_args = parser.parse_args([
        "--source", "greenhouse",
        "--limit", "15",
        "--mock",
        "--concurrency", "8",
        "--no-save-db",
        "-o", "output.json",
    ])
    assert custom_args.source == "greenhouse"
    assert custom_args.limit == 15
    assert custom_args.mock is True
    assert custom_args.concurrency == 8
    assert custom_args.no_save_db is True
    assert custom_args.output_json == "output.json"


def test_cli_main_execution(monkeypatch, tmp_path, sample_raw_postings):
    """Verify CLI main entrypoint executes end-to-end without unhandled exception."""
    json_path = tmp_path / "input.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(sample_raw_postings[:1], f)

    db_path = tmp_path / "cli.db"

    test_args = [
        "pipeline.extract",
        "--input-file", str(json_path),
        "--db-path", str(db_path),
        "--mock",
        "--limit", "1",
    ]
    monkeypatch.setattr("sys.argv", test_args)

    # main() should run and exit cleanly without raising an error
    main()

    # Verify database was written
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) FROM extracted_postings")
    assert cursor.fetchone()[0] == 1
    conn.close()
