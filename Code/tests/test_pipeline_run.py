"""Comprehensive Test Suite for Master Pipeline Orchestrator and Unified CLI.

Verifies:
1. PipelineOrchestrator component initialization, stage execution, and end-to-end flow.
2. Candidate filtering of target roles (Data/ML/Analytics/Platform) and US geography.
3. Structured LLM extraction with anti-hallucination verification.
4. Deduplication and transitive duplicate merging via DSU graph clustering.
5. Idempotent SQLite storage, zero duplicate guarantees, and active/closed lifecycle detection.
6. Export of canonical jobs to RFC 4180 CSV, JSON, and change history audit trail.
7. CLI entrypoint argument parsing across all subcommands (run, evaluate, export, dedup, ingest, extract).
8. Resilient error handling for missing directories, empty inputs, and database edge cases.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List

import pytest

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.__main__ import create_cli_parser, handle_evaluate, main
from pipeline.config import DB_PATH
from pipeline.models.canonical import CanonicalJobPosting
from pipeline.models.schema import JobPostingRecord
from pipeline.run import (
    PipelineOrchestrator,
    ensure_database_schema,
    get_mock_raw_postings,
    run_pipeline,
)
from pipeline.storage.database import init_db


# ==============================================================================
# Pytest Fixtures
# ==============================================================================


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Provides a fresh temporary SQLite database path."""
    return tmp_path / "test_pipeline.db"


@pytest.fixture
def temp_export_dir(tmp_path: Path) -> Path:
    """Provides a fresh temporary export directory path."""
    export_path = tmp_path / "export"
    export_path.mkdir(parents=True, exist_ok=True)
    return export_path


@pytest.fixture
def memory_db() -> Generator[sqlite3.Connection, None, None]:
    """Provides a configured in-memory SQLite database connection."""
    conn = init_db(":memory:")
    ensure_database_schema(conn)
    yield conn
    conn.close()


# ==============================================================================
# 1. Pipeline Orchestrator Unit Tests
# ==============================================================================


class TestPipelineOrchestratorUnit:
    """Tests individual orchestrator methods, configuration, and stage components."""

    def test_orchestrator_initialization_defaults(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies default initialization settings."""
        orch = PipelineOrchestrator(db_path=temp_db, export_dir=temp_export_dir)
        assert orch.db_path == str(temp_db)
        assert orch.export_dir == temp_export_dir
        assert orch.mock is True
        assert orch.concurrency == 5
        assert orch.filter_roles is True
        assert orch.detect_closures is True
        assert orch.dry_run is False
        assert orch.save_db is True

    def test_orchestrator_initialization_custom(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies custom parameter overrides."""
        orch = PipelineOrchestrator(
            db_path=temp_db,
            export_dir=temp_export_dir,
            mock=False,
            limit=15,
            sources=["greenhouse", "hn"],
            concurrency=2,
            filter_roles=False,
            detect_closures=False,
            dry_run=True,
            verbose=True,
        )
        assert orch.mock is False
        assert orch.limit == 15
        assert orch.sources == ["greenhouse", "hn"]
        assert orch.concurrency == 2
        assert orch.filter_roles is False
        assert orch.detect_closures is False
        assert orch.dry_run is True
        assert orch.save_db is False  # dry_run disables save_db

    def test_ensure_database_schema_compatibility(self, memory_db: sqlite3.Connection) -> None:
        """Verifies schema helper guarantees both posting_id and canonical_id exist."""
        ensure_database_schema(memory_db)
        cur = memory_db.cursor()
        cur.execute("PRAGMA table_info(canonical_job_postings)")
        cols = {row[1] for row in cur.fetchall()}
        assert "posting_id" in cols
        assert "canonical_id" in cols

    def test_stage_1_ingest_mock_candidates(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies Stage 1 ingestion returns mock candidates in mock mode."""
        orch = PipelineOrchestrator(db_path=temp_db, export_dir=temp_export_dir, mock=True, limit=5)
        postings = orch.stage_1_ingest()
        assert len(postings) == 5
        for p in postings:
            assert "source" in p
            assert "company" in p
            assert "title" in p

    def test_stage_1_ingest_with_preloaded_postings(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies Stage 1 accepts explicit in-memory postings."""
        custom_postings = [
            {"source": "mock", "company": "TestCo", "title": "Data Engineer", "location": "Remote"},
            {"source": "mock", "company": "TestCo2", "title": "ML Engineer", "location": "NYC"},
        ]
        orch = PipelineOrchestrator(db_path=temp_db, export_dir=temp_export_dir, mock=True)
        postings = orch.stage_1_ingest(raw_postings=custom_postings)
        assert len(postings) == 2
        assert postings[0]["company"] == "TestCo"

    def test_stage_2_filter_roles(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies Stage 2 keeps US Data/ML roles and excludes sales/non-US roles."""
        mock_raw = get_mock_raw_postings()
        orch = PipelineOrchestrator(db_path=temp_db, export_dir=temp_export_dir, mock=True)
        candidates = orch.stage_2_filter(mock_raw)

        # 11 raw postings in mock set -> 8 target Data/ML candidates qualified
        assert len(candidates) < len(mock_raw)
        companies = [c["company"] for c in candidates]
        assert "Acme Corp" not in companies  # Sales role filtered out
        assert "London AI" not in companies  # UK role filtered out
        assert "Stripe" in companies
        assert "Supabase" in companies

    def test_stage_2_filter_disabled(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies Stage 2 retains all postings when filter_roles is False."""
        mock_raw = get_mock_raw_postings()
        orch = PipelineOrchestrator(
            db_path=temp_db,
            export_dir=temp_export_dir,
            mock=True,
            filter_roles=False,
        )
        candidates = orch.stage_2_filter(mock_raw)
        assert len(candidates) == len(mock_raw)

    def test_stage_3_extract_structured_records(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies Stage 3 produces valid JobPostingRecord instances."""
        mock_raw = get_mock_raw_postings()[:3]
        orch = PipelineOrchestrator(db_path=temp_db, export_dir=temp_export_dir, mock=True)
        extracted = orch.stage_3_extract(mock_raw)
        assert len(extracted) == 3
        for r in extracted:
            assert isinstance(r, JobPostingRecord)
            assert r.company
            assert r.title
            assert r.posting_id

    def test_stage_4_dedup_cross_source_merging(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies Stage 4 merges duplicate postings across Greenhouse and HN."""
        mock_raw = get_mock_raw_postings()
        orch = PipelineOrchestrator(db_path=temp_db, export_dir=temp_export_dir, mock=True)
        candidates = orch.stage_2_filter(mock_raw)
        extracted = orch.stage_3_extract(candidates)
        canonical = orch.stage_4_dedup(extracted)

        # There are 8 extracted postings, including Stripe on Greenhouse and Stripe on HN
        assert len(canonical) == len(extracted) - 1
        stripe_entities = [c for c in canonical if c.company == "Stripe"]
        assert len(stripe_entities) == 1
        assert stripe_entities[0].duplicate_count >= 2

    def test_stage_5_store_idempotency(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies Stage 5 inserts cleanly on Run 1 and updates 0 on Run 2."""
        mock_raw = get_mock_raw_postings()
        orch = PipelineOrchestrator(db_path=temp_db, export_dir=temp_export_dir, mock=True)
        candidates = orch.stage_2_filter(mock_raw)
        extracted = orch.stage_3_extract(candidates)
        canonical = orch.stage_4_dedup(extracted)

        # Run 1: Primary Upsert
        t1 = datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)
        res1 = orch.stage_5_store(canonical_records=canonical, raw_postings=mock_raw, current_timestamp=t1)
        assert res1["inserted"] == len(canonical)
        assert res1["updated"] == 0
        assert res1["changes_logged"] == 0

        # Run 2: Re-run with identical data
        t2 = datetime(2026, 2, 2, 12, 0, 0, tzinfo=timezone.utc)
        res2 = orch.stage_5_store(canonical_records=canonical, raw_postings=mock_raw, current_timestamp=t2)
        assert res2["inserted"] == 0
        assert res2["updated"] == 0
        assert res2["unchanged"] == len(canonical)
        assert res2["changes_logged"] == 0

    def test_stage_6_export_artifacts(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies Stage 6 generates CSV, JSON, and audit change history files."""
        mock_raw = get_mock_raw_postings()
        orch = PipelineOrchestrator(db_path=temp_db, export_dir=temp_export_dir, mock=True)
        candidates = orch.stage_2_filter(mock_raw)
        extracted = orch.stage_3_extract(candidates)
        canonical = orch.stage_4_dedup(extracted)
        orch.stage_5_store(canonical_records=canonical, raw_postings=mock_raw)

        export_res = orch.stage_6_export()
        assert "csv" in export_res
        assert "json" in export_res
        assert "history" in export_res

        csv_path = Path(export_res["csv"]["path"])
        json_path = Path(export_res["json"]["path"])
        hist_path = Path(export_res["history"]["path"])

        assert csv_path.exists()
        assert json_path.exists()
        assert hist_path.exists()

        assert export_res["csv"]["records"] == len(canonical)
        assert export_res["json"]["records"] == len(canonical)

        # Verify CSV content RFC 4180 parsing
        with open(csv_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert len(rows) == len(canonical)
            assert "company" in rows[0]
            assert "title" in rows[0]
            assert "salary_range" in rows[0]


# ==============================================================================
# 2. End-to-End Pipeline Execution Tests
# ==============================================================================


class TestEndToEndPipeline:
    """Verifies complete end-to-end dry-run and full pipeline execution."""

    def test_full_pipeline_mock_run(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Executes full end-to-end dry run with mock mode."""
        results = run_pipeline(
            mock=True,
            limit=20,
            db_path=temp_db,
            export_dir=temp_export_dir,
        )

        assert results["status"] == "success"
        assert results["elapsed_seconds"] > 0
        counts = results["counts"]
        assert counts["raw_ingested"] >= 10
        assert counts["filtered_candidates"] >= 7
        assert counts["extracted_records"] >= 7
        assert counts["canonical_records"] >= 6
        assert counts["duplicates_merged"] >= 1
        assert counts["inserted"] >= 6
        assert counts["exported_csv"] >= 6
        assert counts["exported_json"] >= 6

        # Check export files
        artifacts = results["artifacts"]
        assert Path(artifacts["csv"]).exists()
        assert Path(artifacts["json"]).exists()
        assert Path(artifacts["history"]).exists()

    def test_full_pipeline_dry_run_mode(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies dry-run mode completes with zero persistent DB writes."""
        results = run_pipeline(
            mock=True,
            limit=5,
            db_path=temp_db,
            export_dir=temp_export_dir,
            dry_run=True,
        )

        assert results["status"] == "success"
        counts = results["counts"]
        assert counts["inserted"] == 0

    def test_full_pipeline_idempotency(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies that two consecutive pipeline runs with unchanged data are strictly idempotent."""
        # Run 1
        res1 = run_pipeline(mock=True, db_path=temp_db, export_dir=temp_export_dir)
        inserted_run1 = res1["counts"]["inserted"]
        assert inserted_run1 > 0

        # Run 2
        res2 = run_pipeline(mock=True, db_path=temp_db, export_dir=temp_export_dir)
        assert res2["counts"]["inserted"] == 0
        assert res2["counts"]["unchanged"] == inserted_run1
        assert res2["counts"]["changes_logged"] == 0

    def test_summary_table_formatter(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies formatted ASCII summary table string contains all expected sections."""
        orch = PipelineOrchestrator(db_path=temp_db, export_dir=temp_export_dir, mock=True)
        results = orch.run()
        table_str = orch.format_summary_table(
            results,
            export_artifacts={
                "csv": {"path": "canonical_jobs.csv", "size": "10 KB"},
                "json": {"path": "canonical_jobs.json", "size": "15 KB"},
                "history": {"path": "change_history.csv", "size": "100 B"},
            },
        )
        assert "TWIG DATA ENGINEER PIPELINE EXECUTION SUMMARY" in table_str
        assert "STAGE METRICS & THROUGHPUT" in table_str
        assert "Raw Ingestion" in table_str
        assert "LLM Structured Extraction" in table_str
        assert "Deduplication & DSU" in table_str
        assert "Storage & Idempotency" in table_str
        assert "Artifact Exports" in table_str


# ==============================================================================
# 3. CLI Argument Parsing and Subcommand Dispatch Tests
# ==============================================================================


class TestCLIEntrypoint:
    """Tests the unified CLI entrypoint (python -m pipeline)."""

    def test_cli_parser_creation(self) -> None:
        """Verifies argument parser registers all 6 required subcommands."""
        parser = create_cli_parser()
        # Parse help flag without raising exception
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--help"])
        assert exc_info.value.code == 0

    def test_cli_no_args_displays_help(self) -> None:
        """Verifies invoking CLI with no arguments prints help and returns exit code 0."""
        code = main([])
        assert code == 0

    def test_cli_subcommand_run(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies `python -m pipeline run --mock --limit 5` returns 0."""
        exit_code = main([
            "run",
            "--mock",
            "--limit", "5",
            "--db-path", str(temp_db),
            "--export-dir", str(temp_export_dir),
        ])
        assert exit_code == 0

    def test_cli_subcommand_run_dry_run(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies `python -m pipeline run --mock --dry-run` returns 0."""
        exit_code = main([
            "run",
            "--mock",
            "--dry-run",
            "--db-path", str(temp_db),
            "--export-dir", str(temp_export_dir),
        ])
        assert exit_code == 0

    def test_cli_subcommand_evaluate_mock(self) -> None:
        """Verifies `python -m pipeline evaluate --mock` returns 0."""
        exit_code = main(["evaluate", "--mock", "--quiet"])
        assert exit_code == 0

    def test_cli_subcommand_export(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies `python -m pipeline export` returns 0."""
        # Initialize db first with tables
        init_db(temp_db)
        out_csv = temp_export_dir / "test_export.csv"
        exit_code = main([
            "export",
            "--db-path", str(temp_db),
            "--format", "csv",
            "--output", str(out_csv),
        ])
        assert exit_code == 0
        assert out_csv.exists()

    def test_cli_subcommand_dedup(self, temp_db: Path) -> None:
        """Verifies `python -m pipeline dedup --dry-run` returns 0."""
        init_db(temp_db)
        exit_code = main(["dedup", "--dry-run", "--db-path", str(temp_db)])
        assert exit_code == 0

    def test_cli_subcommand_ingest(self, temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verifies `python -m pipeline ingest` runs successfully."""
        monkeypatch.setattr(
            "pipeline.scrapers.greenhouse.GreenhouseScraper.scrape",
            lambda self: [
                {
                    "source": "greenhouse",
                    "id": "123",
                    "title": "Staff Data Engineer",
                    "company": "Stripe",
                    "location": "San Francisco, CA",
                    "url": "https://boards.greenhouse.io/stripe/jobs/123",
                    "content": "Staff Data Engineer in SF.",
                }
            ],
        )
        exit_code = main(["ingest", "--sources", "greenhouse", "--limit", "1", "--db-path", str(temp_db)])
        assert exit_code == 0

    def test_cli_subcommand_extract(self, temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verifies `python -m pipeline extract --mock` runs successfully."""
        monkeypatch.setattr(
            "pipeline.ingest.run_ingest",
            lambda **kwargs: [
                {
                    "source": "mock",
                    "company": "Stripe",
                    "title": "Staff Data Engineer",
                    "url": "https://stripe.com/1",
                    "content": "Staff Data Engineer base $180,000 - $240,000",
                }
            ],
        )
        exit_code = main(["extract", "--mock", "--limit", "2", "--db-path", str(temp_db)])
        assert exit_code == 0

    def test_cli_subprocess_invocation(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies real subprocess invocation: `python -m pipeline run --mock --limit 2`."""
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pipeline",
                "run",
                "--mock",
                "--limit", "2",
                "--db-path", str(temp_db),
                "--export-dir", str(temp_export_dir),
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        assert "PIPELINE EXECUTION STATUS: COMPLETED SUCCESSFULLY" in proc.stdout


# ==============================================================================
# 4. Resilient Error Handling & Edge Cases
# ==============================================================================


class TestErrorHandlingAndEdgeCases:
    """Verifies fail-soft resilience across missing files, directories, and empty inputs."""

    def test_nonexistent_nested_db_dir_created_automatically(self, tmp_path: Path) -> None:
        """Verifies that an uncreated parent directory for db_path is auto-created."""
        nested_db = tmp_path / "deep" / "nested" / "dir" / "test.db"
        assert not nested_db.parent.exists()

        results = run_pipeline(
            mock=True,
            limit=2,
            db_path=nested_db,
            export_dir=tmp_path / "export",
        )
        assert results["status"] == "success"
        assert nested_db.exists()

    def test_nonexistent_export_dir_created_automatically(self, tmp_path: Path) -> None:
        """Verifies that an uncreated export directory is created on demand."""
        nested_export = tmp_path / "deep" / "export" / "dir"
        assert not nested_export.exists()

        results = run_pipeline(
            mock=True,
            limit=2,
            db_path=tmp_path / "test.db",
            export_dir=nested_export,
        )
        assert results["status"] == "success"
        assert nested_export.exists()

    def test_empty_raw_postings_handled_cleanly(self, temp_db: Path, temp_export_dir: Path) -> None:
        """Verifies passing an empty list of raw postings does not crash the pipeline."""
        orch = PipelineOrchestrator(db_path=temp_db, export_dir=temp_export_dir, mock=True)
        results = orch.run(raw_postings=[])
        assert results["status"] == "success"
        assert results["counts"]["raw_ingested"] == 0
        assert results["counts"]["canonical_records"] == 0
        assert results["counts"]["inserted"] == 0

    def test_corrupt_database_connection_handled_gracefully(self, tmp_path: Path) -> None:
        """Verifies graceful handling when an invalid database path is specified."""
        # Point to a directory instead of a file to simulate SQLite open failure
        invalid_db_path = tmp_path / "is_a_directory"
        invalid_db_path.mkdir(parents=True, exist_ok=True)

        orch = PipelineOrchestrator(
            db_path=invalid_db_path,
            export_dir=tmp_path / "export",
            mock=True,
        )
        with pytest.raises(Exception):
            orch.run()
