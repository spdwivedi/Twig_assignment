"""Comprehensive test suite for DataExporter and export CLI.

Verifies:
- CSV export format, RFC 4180 quoting, headers matching 17 schema fields, and row counts
- Formatted list/dict fields: skills (comma-separated), source_urls (pipe-separated), sources, and salary_range
- JSON export with typed arrays and nested dictionaries
- Status filtering (open, closed, all)
- Change history audit trail export
- Empty database tables and missing tables handling
- CLI execution programmatically and via subprocess
- Summary table generation and file size formatting
"""

from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import pytest

from pipeline.export import create_parser, main, print_summary_table
from pipeline.storage.exporter import (
    DEFAULT_CANONICAL_CSV_HEADERS,
    DEFAULT_HISTORY_CSV_HEADERS,
    SCHEMA_17_FIELDS,
    DataExporter,
    format_file_size,
    format_salary_range,
    format_skills_for_csv,
    format_source_urls_for_csv,
    format_sources_for_csv,
)


# ==============================================================================
# Pytest Fixtures
# ==============================================================================


@pytest.fixture
def memory_db() -> Generator[sqlite3.Connection, None, None]:
    """Provides an in-memory SQLite database pre-populated with test job postings and history."""
    conn = sqlite3.connect(":memory:")

    # 1. Create canonical_job_postings table
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
            skills TEXT NOT NULL,
            visa_sponsorship INTEGER,
            sources TEXT NOT NULL,
            source_urls TEXT NOT NULL,
            source_posting_ids TEXT NOT NULL,
            evidence TEXT NOT NULL,
            first_seen TIMESTAMP NOT NULL,
            last_seen TIMESTAMP NOT NULL,
            duplicate_count INTEGER NOT NULL,
            status TEXT DEFAULT 'open',
            posted_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 2. Create posting_change_history table
    conn.execute(
        """
        CREATE TABLE posting_change_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            posting_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reason TEXT
        )
        """
    )

    # 3. Insert mock canonical records
    records = [
        (
            "canon-001",
            "Stripe",
            "Senior Data Engineer",
            "senior",
            "San Francisco",
            "US",
            "hybrid",
            170000.0,
            220000.0,
            "USD",
            "full_time",
            json.dumps(["Python", "SQL", "Spark", "dbt"]),
            1,
            json.dumps(["greenhouse", "hackernews"]),
            json.dumps(["https://boards.greenhouse.io/stripe/jobs/1", "https://news.ycombinator.com/item?id=101"]),
            json.dumps(["gh-101", "hn-101"]),
            json.dumps({"salary": "$170k - $220k", "skills": "Python, Spark"}),
            "2026-01-15T08:00:00Z",
            "2026-01-20T12:00:00Z",
            2,
            "open",
            "2026-01-15",
        ),
        (
            "canon-002",
            "Palantir",
            "Forward Deployed Engineer",
            "mid",
            "New York",
            "US",
            "onsite",
            150000.0,
            150000.0,
            "USD",
            "full_time",
            json.dumps(["Java", "Go", "Kubernetes"]),
            0,
            json.dumps(["lever"]),
            json.dumps(["https://jobs.lever.co/palantir/jobs/2"]),
            json.dumps(["lev-202"]),
            json.dumps({"salary": "$150,000 USD", "title": "FDE"}),
            "2026-01-10T09:00:00Z",
            "2026-01-18T10:00:00Z",
            1,
            "open",
            "2026-01-10",
        ),
        (
            "canon-003",
            "Airbnb, Inc.",
            "Staff Data Engineer (Core, Platform)\nRemote",
            "staff_plus",
            None,
            "US",
            "remote",
            210000.0,
            260000.0,
            "USD",
            "full_time",
            json.dumps(["Scala", "Kafka", "Airflow"]),
            None,
            json.dumps(["greenhouse"]),
            json.dumps(["https://boards.greenhouse.io/airbnb/jobs/3"]),
            json.dumps(["gh-303"]),
            json.dumps({"desc": 'Role includes "cutting-edge" data systems'}),
            "2026-01-05T10:00:00Z",
            "2026-01-15T11:00:00Z",
            1,
            "closed",
            "2026-01-05",
        ),
    ]

    conn.executemany(
        """
        INSERT INTO canonical_job_postings (
            canonical_id, company, title, seniority, city, country,
            remote_policy, salary_min, salary_max, currency,
            employment_type, skills, visa_sponsorship, sources,
            source_urls, source_posting_ids, evidence, first_seen,
            last_seen, duplicate_count, status, posted_date
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        records,
    )

    # 4. Insert mock change history entries
    history_entries = [
        ("canon-001", "salary_max", "200000.0", "220000.0", "compensation_updated"),
        ("canon-003", "status", "open", "closed", "posting_closed"),
    ]
    conn.executemany(
        """
        INSERT INTO posting_change_history (
            posting_id, field_name, old_value, new_value, reason
        ) VALUES (?, ?, ?, ?, ?)
        """,
        history_entries,
    )

    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def temp_disk_db(tmp_path: Path, memory_db: sqlite3.Connection) -> Path:
    """Creates a temporary on-disk SQLite database file copied from memory_db."""
    db_file = tmp_path / "test_pipeline.db"
    disk_conn = sqlite3.connect(str(db_file))
    memory_db.backup(disk_conn)
    disk_conn.close()
    return db_file


# ==============================================================================
# Helper Formatting Tests
# ==============================================================================


class TestHelperFormatters:
    """Tests formatting helper functions for skills, sources, URLs, salary, and file sizes."""

    def test_format_skills_from_json_array(self):
        json_skills = '["Python", "SQL", "Spark", "dbt"]'
        assert format_skills_for_csv(json_skills) == "Python, SQL, Spark, dbt"

    def test_format_skills_from_python_list(self):
        skills_list = ["Python", "SQL", "Spark", "dbt"]
        assert format_skills_for_csv(skills_list) == "Python, SQL, Spark, dbt"

    def test_format_skills_from_comma_string(self):
        raw_str = "Python, SQL, Spark"
        assert format_skills_for_csv(raw_str) == "Python, SQL, Spark"

    def test_format_skills_empty_and_null(self):
        assert format_skills_for_csv("") == ""
        assert format_skills_for_csv(None) == ""
        assert format_skills_for_csv("[]") == ""
        assert format_skills_for_csv([]) == ""

    def test_format_source_urls_from_json_array(self):
        urls_json = '["https://example.com/1", "https://example.com/2"]'
        assert format_source_urls_for_csv(urls_json) == "https://example.com/1 | https://example.com/2"

    def test_format_source_urls_from_python_list(self):
        urls = ["https://example.com/1", "https://example.com/2"]
        assert format_source_urls_for_csv(urls) == "https://example.com/1 | https://example.com/2"

    def test_format_source_urls_empty_and_null(self):
        assert format_source_urls_for_csv("") == ""
        assert format_source_urls_for_csv(None) == ""
        assert format_source_urls_for_csv("[]") == ""

    def test_format_sources_from_json_array(self):
        sources_json = '["greenhouse", "hackernews"]'
        assert format_sources_for_csv(sources_json) == "greenhouse, hackernews"

    def test_format_sources_from_python_list(self):
        sources = ["lever", "greenhouse"]
        assert format_sources_for_csv(sources) == "lever, greenhouse"

    def test_format_salary_range_standard_interval(self):
        assert format_salary_range(160000, 220000, "USD") == "$160,000 - $220,000 USD"

    def test_format_salary_range_identical_min_max(self):
        assert format_salary_range(150000, 150000, "USD") == "$150,000 USD"

    def test_format_salary_range_min_only(self):
        assert format_salary_range(120000, None, "USD") == "$120,000+ USD"

    def test_format_salary_range_max_only(self):
        assert format_salary_range(None, 180000, "USD") == "Up to $180,000 USD"

    def test_format_salary_range_empty(self):
        assert format_salary_range(None, None, "USD") == ""
        assert format_salary_range(None, None, None) == ""

    def test_format_salary_range_non_integer(self):
        assert format_salary_range(85.5, 100.0, "USD") == "$85.50 - $100 USD"

    def test_format_salary_range_international_currencies(self):
        assert format_salary_range(70000, 90000, "EUR") == "€70,000 - €90,000 EUR"
        assert format_salary_range(60000, 80000, "GBP") == "£60,000 - £80,000 GBP"

    def test_format_file_size(self):
        assert format_file_size(500) == "500 B"
        assert format_file_size(1536) == "1.5 KB"
        assert format_file_size(2097152) == "2.0 MB"
        assert format_file_size(1073741824) == "1.0 GB"


# ==============================================================================
# CSV Export Tests
# ==============================================================================


class TestCSVExport:
    """Tests DataExporter.export_canonical_jobs_to_csv method."""

    def test_export_canonical_jobs_to_csv_headers_and_row_count(self, memory_db: sqlite3.Connection, tmp_path: Path):
        output_csv = tmp_path / "canonical_jobs.csv"
        count = DataExporter.export_canonical_jobs_to_csv(memory_db, output_csv)

        assert count == 3
        assert output_csv.exists()

        with open(output_csv, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []

            # 1. Verify all 17 schema fields are present
            for field in SCHEMA_17_FIELDS:
                assert field in fieldnames, f"Schema field '{field}' missing from CSV headers"

            # 2. Verify additional formatted fields are present
            assert "salary_range" in fieldnames
            assert "sources" in fieldnames
            assert "canonical_id" in fieldnames

            rows = list(reader)
            assert len(rows) == 3

    def test_export_canonical_jobs_to_csv_field_formatting(self, memory_db: sqlite3.Connection, tmp_path: Path):
        output_csv = tmp_path / "canonical_jobs.csv"
        DataExporter.export_canonical_jobs_to_csv(memory_db, output_csv)

        with open(output_csv, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            row_map = {row["company"]: row for row in reader}

            # Inspect Stripe record
            stripe = row_map["Stripe"]
            assert stripe["posting_id"] == "canon-001"
            assert stripe["skills"] == "Python, SQL, Spark, dbt"
            assert stripe["sources"] == "greenhouse, hackernews"
            assert stripe["source_urls"] == "https://boards.greenhouse.io/stripe/jobs/1 | https://news.ycombinator.com/item?id=101"
            assert stripe["salary_min"] == "170000.0"
            assert stripe["salary_max"] == "220000.0"
            assert stripe["salary_range"] == "$170,000 - $220,000 USD"
            assert stripe["visa_sponsorship"] == "true"
            assert stripe["status"] == "open"

            # Inspect Palantir record (identical min and max)
            palantir = row_map["Palantir"]
            assert palantir["salary_range"] == "$150,000 USD"
            assert palantir["visa_sponsorship"] == "false"

    def test_export_canonical_jobs_to_csv_rfc4180_quoting(self, memory_db: sqlite3.Connection, tmp_path: Path):
        """Verifies that special characters (commas, quotes, newlines) are RFC 4180 escaped."""
        output_csv = tmp_path / "canonical_jobs.csv"
        DataExporter.export_canonical_jobs_to_csv(memory_db, output_csv)

        with open(output_csv, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            row_map = {row["company"]: row for row in reader}

            airbnb = row_map["Airbnb, Inc."]
            # Company has a comma
            assert airbnb["company"] == "Airbnb, Inc."
            # Title has a newline and commas
            assert "\n" in airbnb["title"]
            assert "Core, Platform" in airbnb["title"]
            # Evidence has escaped double quotes in JSON
            assert r'Role includes \"cutting-edge\"' in airbnb["evidence"]
            parsed_evidence = json.loads(airbnb["evidence"])
            assert parsed_evidence["desc"] == 'Role includes "cutting-edge" data systems'

    def test_export_canonical_jobs_to_csv_status_filtering(self, memory_db: sqlite3.Connection, tmp_path: Path):
        # Open jobs only
        open_csv = tmp_path / "open_jobs.csv"
        open_count = DataExporter.export_canonical_jobs_to_csv(memory_db, open_csv, status_filter="open")
        assert open_count == 2
        with open(open_csv, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            statuses = [r["status"] for r in reader]
            assert all(s == "open" for s in statuses)

        # Closed jobs only
        closed_csv = tmp_path / "closed_jobs.csv"
        closed_count = DataExporter.export_canonical_jobs_to_csv(memory_db, closed_csv, status_filter="closed")
        assert closed_count == 1
        with open(closed_csv, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            statuses = [r["status"] for r in reader]
            assert all(s == "closed" for s in statuses)

        # All jobs
        all_csv = tmp_path / "all_jobs.csv"
        all_count = DataExporter.export_canonical_jobs_to_csv(memory_db, all_csv, status_filter="all")
        assert all_count == 3

    def test_export_canonical_jobs_to_csv_custom_columns(self, memory_db: sqlite3.Connection, tmp_path: Path):
        custom_cols = ["posting_id", "company", "title", "salary_range", "status"]
        output_csv = tmp_path / "custom_jobs.csv"
        count = DataExporter.export_canonical_jobs_to_csv(memory_db, output_csv, columns=custom_cols)

        assert count == 3
        with open(output_csv, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames == custom_cols
            rows = list(reader)
            assert len(rows) == 3
            assert "salary_range" in rows[0]
            assert "skills" not in rows[0]

    def test_export_canonical_jobs_to_csv_exact_17_schema_fields(self, memory_db: sqlite3.Connection, tmp_path: Path):
        output_csv = tmp_path / "exact_17_fields.csv"
        count = DataExporter.export_canonical_jobs_to_csv(memory_db, output_csv, columns=SCHEMA_17_FIELDS)

        assert count == 3
        with open(output_csv, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames == SCHEMA_17_FIELDS
            assert len(reader.fieldnames) == 17

    def test_export_canonical_jobs_to_csv_empty_table(self, tmp_path: Path):
        empty_conn = sqlite3.connect(":memory:")
        empty_conn.execute(
            """
            CREATE TABLE canonical_job_postings (
                canonical_id TEXT PRIMARY KEY,
                company TEXT,
                title TEXT,
                status TEXT
            )
            """
        )
        output_csv = tmp_path / "empty.csv"
        count = DataExporter.export_canonical_jobs_to_csv(empty_conn, output_csv)

        assert count == 0
        assert output_csv.exists()
        with open(output_csv, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)
            # Only header row exists
            assert len(rows) == 1
            assert rows[0] == DEFAULT_CANONICAL_CSV_HEADERS
        empty_conn.close()

    def test_export_canonical_jobs_to_csv_nonexistent_table(self, tmp_path: Path):
        blank_conn = sqlite3.connect(":memory:")
        output_csv = tmp_path / "no_table.csv"
        count = DataExporter.export_canonical_jobs_to_csv(blank_conn, output_csv)

        assert count == 0
        assert output_csv.exists()
        with open(output_csv, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)
            assert len(rows) == 1
        blank_conn.close()


# ==============================================================================
# JSON Export Tests
# ==============================================================================


class TestJSONExport:
    """Tests DataExporter.export_canonical_jobs_to_json method."""

    def test_export_canonical_jobs_to_json_valid_array(self, memory_db: sqlite3.Connection, tmp_path: Path):
        output_json = tmp_path / "canonical_jobs.json"
        count = DataExporter.export_canonical_jobs_to_json(memory_db, output_json)

        assert count == 3
        assert output_json.exists()

        with open(output_json, mode="r", encoding="utf-8") as f:
            data = json.load(f)

        assert isinstance(data, list)
        assert len(data) == 3

    def test_export_canonical_jobs_to_json_nested_structures(self, memory_db: sqlite3.Connection, tmp_path: Path):
        output_json = tmp_path / "canonical_jobs.json"
        DataExporter.export_canonical_jobs_to_json(memory_db, output_json)

        with open(output_json, mode="r", encoding="utf-8") as f:
            data = json.load(f)

        stripe = next(r for r in data if r["company"] == "Stripe")
        assert isinstance(stripe["skills"], list)
        assert stripe["skills"] == ["Python", "SQL", "Spark", "dbt"]
        assert isinstance(stripe["sources"], list)
        assert stripe["sources"] == ["greenhouse", "hackernews"]
        assert isinstance(stripe["source_urls"], list)
        assert len(stripe["source_urls"]) == 2
        assert isinstance(stripe["evidence"], dict)
        assert stripe["evidence"]["salary"] == "$170k - $220k"
        assert stripe["visa_sponsorship"] is True
        assert stripe["salary_min"] == 170000.0
        assert stripe["salary_max"] == 220000.0
        assert stripe["salary_range"] == "$170,000 - $220,000 USD"

    def test_export_canonical_jobs_to_json_status_filter(self, memory_db: sqlite3.Connection, tmp_path: Path):
        output_json = tmp_path / "open_jobs.json"
        count = DataExporter.export_canonical_jobs_to_json(memory_db, output_json, status_filter="open")

        assert count == 2
        with open(output_json, mode="r", encoding="utf-8") as f:
            data = json.load(f)
        assert all(r["status"] == "open" for r in data)

    def test_export_canonical_jobs_to_json_empty_table(self, tmp_path: Path):
        blank_conn = sqlite3.connect(":memory:")
        output_json = tmp_path / "empty.json"
        count = DataExporter.export_canonical_jobs_to_json(blank_conn, output_json)

        assert count == 0
        with open(output_json, mode="r", encoding="utf-8") as f:
            data = json.load(f)
        assert data == []
        blank_conn.close()


# ==============================================================================
# Change History Audit Trail Export Tests
# ==============================================================================


class TestChangeHistoryExport:
    """Tests DataExporter.export_change_history method."""

    def test_export_change_history_valid(self, memory_db: sqlite3.Connection, tmp_path: Path):
        output_csv = tmp_path / "change_history.csv"
        count = DataExporter.export_change_history(memory_db, output_csv)

        assert count == 2
        assert output_csv.exists()

        with open(output_csv, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames == DEFAULT_HISTORY_CSV_HEADERS
            rows = list(reader)
            assert len(rows) == 2

            sal_change = next(r for r in rows if r["field_name"] == "salary_max")
            assert sal_change["posting_id"] == "canon-001"
            assert sal_change["old_value"] == "200000.0"
            assert sal_change["new_value"] == "220000.0"
            assert sal_change["reason"] == "compensation_updated"

            status_change = next(r for r in rows if r["field_name"] == "status")
            assert status_change["posting_id"] == "canon-003"
            assert status_change["old_value"] == "open"
            assert status_change["new_value"] == "closed"
            assert status_change["reason"] == "posting_closed"

    def test_export_change_history_empty_table(self, tmp_path: Path):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE posting_change_history (
                history_id INTEGER PRIMARY KEY,
                posting_id TEXT,
                field_name TEXT,
                old_value TEXT,
                new_value TEXT,
                detected_at TIMESTAMP,
                reason TEXT
            )
            """
        )
        output_csv = tmp_path / "empty_hist.csv"
        count = DataExporter.export_change_history(conn, output_csv)

        assert count == 0
        assert output_csv.exists()
        with open(output_csv, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)
            assert len(rows) == 1
            assert rows[0] == DEFAULT_HISTORY_CSV_HEADERS
        conn.close()

    def test_export_change_history_missing_table(self, tmp_path: Path):
        blank_conn = sqlite3.connect(":memory:")
        output_csv = tmp_path / "no_hist.csv"
        count = DataExporter.export_change_history(blank_conn, output_csv)

        assert count == 0
        assert output_csv.exists()
        with open(output_csv, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)
            assert len(rows) == 1
        blank_conn.close()


# ==============================================================================
# CLI Argument Parser & Execution Tests
# ==============================================================================


class TestCLIExecution:
    """Tests pipeline.export CLI argument parsing, programmatic execution, and subprocess invocation."""

    def test_create_parser_defaults(self):
        parser = create_parser()
        args = parser.parse_args([])
        assert args.format == "csv"
        assert args.output is None
        assert args.status == "all"
        assert args.include_history is False
        assert args.history_output == "data/export/change_history.csv"

    def test_cli_programmatic_default_csv(self, temp_disk_db: Path, tmp_path: Path, capsys):
        out_csv = tmp_path / "canonical.csv"
        exit_code = main([
            "--db-path", str(temp_disk_db),
            "--output", str(out_csv),
            "--format", "csv",
        ])

        assert exit_code == 0
        assert out_csv.exists()
        captured = capsys.readouterr()
        assert "Twig Pipeline Export Summary Report" in captured.out
        assert "Canonical Jobs" in captured.out
        assert "Total Records Exported : 3" in captured.out

    def test_cli_programmatic_json_export(self, temp_disk_db: Path, tmp_path: Path, capsys):
        out_json = tmp_path / "canonical.json"
        exit_code = main([
            "--db-path", str(temp_disk_db),
            "--output", str(out_json),
            "--format", "json",
            "--status", "open",
        ])

        assert exit_code == 0
        assert out_json.exists()
        with open(out_json, mode="r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data) == 2

    def test_cli_programmatic_include_history(self, temp_disk_db: Path, tmp_path: Path, capsys):
        out_csv = tmp_path / "canonical.csv"
        hist_csv = tmp_path / "history.csv"
        exit_code = main([
            "--db-path", str(temp_disk_db),
            "--output", str(out_csv),
            "--include-history",
            "--history-output", str(hist_csv),
        ])

        assert exit_code == 0
        assert out_csv.exists()
        assert hist_csv.exists()
        captured = capsys.readouterr()
        assert "Change History" in captured.out
        assert "Total Output Files     : 2" in captured.out

    def test_cli_missing_database_file(self, tmp_path: Path, capsys):
        missing_db = tmp_path / "nonexistent.db"
        exit_code = main(["--db-path", str(missing_db)])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Error: Database file does not exist" in captured.err

    def test_cli_subprocess_invocation(self, temp_disk_db: Path, tmp_path: Path):
        """Runs the CLI as a separate Python subprocess to test 'python -m pipeline.export'."""
        out_csv = tmp_path / "subp_canonical.csv"
        hist_csv = tmp_path / "subp_history.csv"
        project_root = Path(__file__).resolve().parent.parent

        cmd = [
            sys.executable,
            "-m",
            "pipeline.export",
            "--db-path",
            str(temp_disk_db),
            "--output",
            str(out_csv),
            "--include-history",
            "--history-output",
            str(hist_csv),
            "--status",
            "all",
        ]

        import os

        env = os.environ.copy()
        env["PYTHONPATH"] = str(project_root)

        result = subprocess.run(
            cmd,
            cwd=str(project_root),
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Subprocess failed with stderr:\n{result.stderr}"
        assert out_csv.exists()
        assert hist_csv.exists()
        assert "Twig Pipeline Export Summary Report" in result.stdout
        assert "Total Records Exported : 5" in result.stdout
