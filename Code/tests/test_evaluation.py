"""Comprehensive test suite for Phase 5 Benchmark Evaluator and Accuracy & Cost Reporter.

Verifies:
1. Exact match calculations across scalar fields (strings, numbers, enums, booleans, nulls).
2. Set-based Skills Precision, Recall, and F1 calculations.
3. Strict Null Compliance calculations (penalizing ungrounded false guesses).
4. Verbatim Evidence Match Rate calculations against source text.
5. Token and LLM dollar cost accounting.
6. End-to-end BenchmarkEvaluator execution and Markdown report generation.
7. CLI argument parsing and execution via programmatic main and subprocess.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.evaluate import (
    DEFAULT_BENCHMARK_PATH,
    DEFAULT_REPORT_PATH,
    EVALUATED_SCALAR_FIELDS,
    BenchmarkEvaluator,
    build_arg_parser,
    compute_skills_metrics,
    compute_strict_null_compliance,
    compute_token_and_cost_accounting,
    compute_verbatim_evidence_match,
    exact_match_scalar,
    main,
    normalize_source_name,
)
from pipeline.extractor.extractor import JobExtractor
from pipeline.extractor.llm_client import LLMClient
from pipeline.models.schema import (
    EmploymentTypeEnum,
    JobPostingRecord,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
)


# ==============================================================================
# 1. Exact Match Scalar Calculation Tests
# ==============================================================================


class TestExactMatchScalar:
    """Tests for exact_match_scalar across diverse data types."""

    def test_string_matching_case_and_whitespace(self):
        """Verify case-insensitive and whitespace-normalized string equivalence."""
        assert exact_match_scalar("Stripe", "stripe", "company") is True
        assert exact_match_scalar("  Senior Data Engineer  ", "senior data engineer", "title") is True
        assert exact_match_scalar("USD", "usd", "currency") is True
        assert exact_match_scalar("Stripe Inc", "Stripe", "company") is False
        assert exact_match_scalar("Data Engineer", "ML Engineer", "title") is False

    def test_numeric_salary_bounds_with_tolerance(self):
        """Verify numeric salary comparison with float tolerance."""
        assert exact_match_scalar(180000.0, 180000.0, "salary_min") is True
        assert exact_match_scalar(180000, 180000.0, "salary_min") is True
        assert exact_match_scalar(180000.5, 180000.0, "salary_min") is True
        assert exact_match_scalar(180005.0, 180000.0, "salary_min") is False
        assert exact_match_scalar(225000.0, 225000.0, "salary_max") is True
        assert exact_match_scalar("invalid_number", 180000.0, "salary_min") is False

    def test_enum_matching_and_string_coercion(self):
        """Verify enums match raw string equivalents and handle hyphens/underscores."""
        assert exact_match_scalar(SeniorityEnum.senior, "senior", "seniority") is True
        assert exact_match_scalar(SeniorityEnum.staff_plus, "staff_plus", "seniority") is True
        assert exact_match_scalar(RemotePolicyEnum.hybrid, "hybrid", "remote_policy") is True
        assert exact_match_scalar(EmploymentTypeEnum.full_time, "full_time", "employment_type") is True
        assert exact_match_scalar("full-time", "full_time", "employment_type") is True
        assert exact_match_scalar(SeniorityEnum.junior, "senior", "seniority") is False

    def test_boolean_visa_sponsorship(self):
        """Verify boolean equivalence for visa sponsorship."""
        assert exact_match_scalar(True, True, "visa_sponsorship") is True
        assert exact_match_scalar(False, False, "visa_sponsorship") is True
        assert exact_match_scalar(True, False, "visa_sponsorship") is False
        assert exact_match_scalar(False, True, "visa_sponsorship") is False

    def test_null_equivalence(self):
        """Verify dual nulls return True and one-sided nulls return False."""
        assert exact_match_scalar(None, None, "salary_min") is True
        assert exact_match_scalar(None, None, "visa_sponsorship") is True
        assert exact_match_scalar(None, 180000.0, "salary_min") is False
        assert exact_match_scalar(180000.0, None, "salary_min") is False
        assert exact_match_scalar(None, "senior", "seniority") is False


# ==============================================================================
# 2. Skills Precision, Recall, and F1 Tests
# ==============================================================================


class TestSkillsMetrics:
    """Tests for set-based technical skills Precision, Recall, and F1 calculations."""

    def test_perfect_skills_overlap(self):
        """Verify 100% metrics when extracted and expected sets are identical."""
        extracted = ["Python", "SQL", "Spark"]
        expected = ["python", "sql", "spark"]
        res = compute_skills_metrics(extracted, expected)
        assert res["tp"] == 3
        assert res["fp"] == 0
        assert res["fn"] == 0
        assert res["precision"] == 1.0
        assert res["recall"] == 1.0
        assert res["f1"] == 1.0

    def test_partial_skills_overlap(self):
        """Verify precision and recall on partial set overlap."""
        extracted = ["Python", "Spark", "Kafka", "Docker"]
        expected = ["Python", "Spark", "dbt", "Snowflake"]
        # TP: Python, Spark (2)
        # FP: Kafka, Docker (2)
        # FN: dbt, Snowflake (2)
        res = compute_skills_metrics(extracted, expected)
        assert res["tp"] == 2
        assert res["fp"] == 2
        assert res["fn"] == 2
        assert res["precision"] == 0.5
        assert res["recall"] == 0.5
        assert res["f1"] == 0.5

    def test_zero_skills_overlap(self):
        """Verify zero metric scores on completely disjoint skill sets."""
        extracted = ["Java", "C++"]
        expected = ["Python", "SQL"]
        res = compute_skills_metrics(extracted, expected)
        assert res["tp"] == 0
        assert res["fp"] == 2
        assert res["fn"] == 2
        assert res["precision"] == 0.0
        assert res["recall"] == 0.0
        assert res["f1"] == 0.0

    def test_both_skills_empty(self):
        """Verify empty sets return perfect 1.0 scores (correct extraction of absence)."""
        res = compute_skills_metrics([], [])
        assert res["precision"] == 1.0
        assert res["recall"] == 1.0
        assert res["f1"] == 1.0

    def test_empty_extracted_with_expected(self):
        """Verify empty extracted list with non-empty expected returns 0.0."""
        res = compute_skills_metrics([], ["Python", "Spark"])
        assert res["precision"] == 0.0
        assert res["recall"] == 0.0
        assert res["f1"] == 0.0

    def test_whitespace_and_casing_normalization(self):
        """Verify token normalization ignores surrounding spaces and casing."""
        res = compute_skills_metrics(["  Apache Spark  ", "PYTHON"], ["apache spark", "python"])
        assert res["f1"] == 1.0


# ==============================================================================
# 3. Strict Null Compliance Tests
# ==============================================================================


class TestStrictNullCompliance:
    """Tests for Strict Null Rule compliance and false guess penalization."""

    def test_all_unstated_fields_correctly_nulled(self):
        """Verify 100% compliance when all unstated fields evaluate to null."""
        expected = {
            "company": "Stripe",
            "title": "Data Engineer",
            "salary_min": None,
            "salary_max": None,
            "currency": None,
            "visa_sponsorship": None,
        }
        extracted = {
            "company": "Stripe",
            "title": "Data Engineer",
            "salary_min": None,
            "salary_max": None,
            "currency": None,
            "visa_sponsorship": None,
        }
        fields_to_check = ["company", "title", "salary_min", "salary_max", "currency", "visa_sponsorship"]
        res = compute_strict_null_compliance(extracted, expected, fields=fields_to_check)
        assert res["total_unstated"] == 4
        assert res["compliant_count"] == 4
        assert res["compliance_rate"] == 1.0
        assert len(res["false_guesses"]) == 0

    def test_false_guess_penalization(self):
        """Verify that hallucinating an unstated salary or visa status penalizes compliance."""
        expected = {
            "salary_min": None,
            "salary_max": None,
            "visa_sponsorship": None,
            "seniority": None,
        }
        extracted = {
            "salary_min": 150000.0,  # False guess!
            "salary_max": 200000.0,  # False guess!
            "visa_sponsorship": None,  # Compliant
            "seniority": None,  # Compliant
        }
        res = compute_strict_null_compliance(extracted, expected, fields=["salary_min", "salary_max", "visa_sponsorship", "seniority"])
        assert res["total_unstated"] == 4
        assert res["compliant_count"] == 2
        assert res["compliance_rate"] == 0.5
        assert len(res["false_guesses"]) == 2
        fields = [g["field"] for g in res["false_guesses"]]
        assert "salary_min" in fields
        assert "salary_max" in fields

    def test_no_unstated_fields(self):
        """Verify 1.0 rate when ground truth contains no unstated fields."""
        expected = {"company": "Stripe", "title": "Staff Engineer"}
        extracted = {"company": "Stripe", "title": "Staff Engineer"}
        res = compute_strict_null_compliance(extracted, expected, fields=["company", "title"])
        assert res["total_unstated"] == 0
        assert res["compliance_rate"] == 1.0


# ==============================================================================
# 4. Verbatim Evidence Match Tests
# ==============================================================================


class TestVerbatimEvidenceMatch:
    """Tests for verifying evidence snippets against raw posting text."""

    SOURCE_TEXT = (
        "Stripe is hiring a Senior Data Engineer in San Francisco, CA.\n"
        "Compensation: $210,000 - $260,000 USD base salary.\n"
        "Technologies: Apache Spark, Delta Lake, Python, SQL.\n"
        "We do not provide visa sponsorship."
    )

    def test_verbatim_evidence_fully_verified(self):
        """Verify 100% evidence match when all populated fields have literal quotes."""
        record = {
            "company": "Stripe",
            "title": "Senior Data Engineer",
            "salary_min": 210000.0,
            "salary_max": 260000.0,
            "currency": "USD",
            "visa_sponsorship": False,
            "evidence": {
                "company": "Stripe",
                "title": "Senior Data Engineer",
                "salary": "$210,000 - $260,000 USD base salary",
                "visa_sponsorship": "We do not provide visa sponsorship",
            },
        }
        res = compute_verbatim_evidence_match(record, self.SOURCE_TEXT)
        assert res["verified_count"] == res["total_populated"]
        assert res["match_rate"] == 1.0

    def test_fabricated_evidence_unverified(self):
        """Verify fabricated snippet fails verbatim check."""
        record = {
            "company": "Stripe",
            "title": "Principal Architect",  # Not in source text
            "evidence": {
                "company": "Stripe",
                "title": "Principal Architect, Cloud Systems",  # Fabricated!
            },
        }
        res = compute_verbatim_evidence_match(record, self.SOURCE_TEXT)
        assert res["total_populated"] == 2
        assert res["verified_count"] == 1  # Only company is verified
        assert res["match_rate"] == 0.5
        assert res["details"]["title"]["verified"] is False

    def test_missing_evidence_unverified(self):
        """Verify populated field without an evidence snippet fails verification."""
        record = {
            "company": "Stripe",
            "title": "Senior Data Engineer",
            "evidence": {},  # Missing!
        }
        res = compute_verbatim_evidence_match(record, self.SOURCE_TEXT)
        assert res["match_rate"] == 0.0
        assert res["verified_count"] == 0


# ==============================================================================
# 5. Token and Dollar Cost Accounting Tests
# ==============================================================================


class TestCostAccounting:
    """Tests for token accounting and dollar cost calculations."""

    def test_gemini_pricing_calculation(self):
        """Verify Gemini 1.5 Flash cost calculation ($0.075 / 1M prompt, $0.300 / 1M completion)."""
        prompt_tokens = 1_000_000
        completion_tokens = 1_000_000
        res = compute_token_and_cost_accounting(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            num_items=10,
            model="gemini-1.5-flash",
        )
        expected_cost = 0.075 + 0.300  # $0.375
        assert res["total_cost_usd"] == pytest.approx(expected_cost, rel=1e-4)
        assert res["avg_cost_per_job_usd"] == pytest.approx(expected_cost / 10, rel=1e-4)
        assert res["total_tokens"] == 2_000_000

    def test_zero_items_cost_handling(self):
        """Verify zero items returns 0.0 avg cost without DivisionByZero error."""
        res = compute_token_and_cost_accounting(0, 0, 0)
        assert res["total_cost_usd"] == 0.0
        assert res["avg_cost_per_job_usd"] == 0.0


# ==============================================================================
# 6. BenchmarkEvaluator End-to-End Tests
# ==============================================================================


class TestBenchmarkEvaluatorEngine:
    """Tests for the BenchmarkEvaluator orchestrator and reporting."""

    @pytest.fixture
    def sample_benchmark_items(self) -> List[Dict[str, Any]]:
        return [
            {
                "benchmark_id": "test_hn_01",
                "source": "hackernews",
                "raw_text": "Fly.io | Chicago, IL | Full-time | Senior Data Engineer\nCompensation: $170k - $210k USD.\nSkills: Python, Go, Kafka.\nVisa: Yes",
                "ground_truth": {
                    "company": "Fly.io",
                    "title": "Senior Data Engineer",
                    "seniority": "senior",
                    "city": "Chicago",
                    "country": "US",
                    "remote_policy": "onsite",
                    "salary_min": 170000.0,
                    "salary_max": 210000.0,
                    "currency": "USD",
                    "employment_type": "full_time",
                    "skills": ["python", "go", "kafka"],
                    "visa_sponsorship": True,
                },
            },
            {
                "benchmark_id": "test_gh_01",
                "source": "greenhouse",
                "raw_text": "Company: Stripe\nTitle: Staff Data Engineer\nLocation: San Francisco, CA (Hybrid)\nFull-time\nSalary: $200,000 - $250,000 USD\nSkills: Python, Spark, SQL\nNo visa sponsorship",
                "ground_truth": {
                    "company": "Stripe",
                    "title": "Staff Data Engineer",
                    "seniority": "staff_plus",
                    "city": "San Francisco",
                    "country": "US",
                    "remote_policy": "hybrid",
                    "salary_min": 200000.0,
                    "salary_max": 250000.0,
                    "currency": "USD",
                    "employment_type": "full_time",
                    "skills": ["python", "spark", "sql"],
                    "visa_sponsorship": False,
                },
            },
        ]

    def test_load_benchmark_file(self):
        """Verify load_benchmark loads valid JSON file."""
        evaluator = BenchmarkEvaluator(benchmark_path=DEFAULT_BENCHMARK_PATH)
        data = evaluator.load_benchmark()
        assert isinstance(data, list)
        assert len(data) == 20

    def test_load_benchmark_file_not_found(self, tmp_path):
        """Verify FileNotFoundError is raised when benchmark file is missing."""
        missing = tmp_path / "non_existent.json"
        evaluator = BenchmarkEvaluator(benchmark_path=missing)
        with pytest.raises(FileNotFoundError):
            evaluator.load_benchmark()

    def test_evaluate_item_structure(self, sample_benchmark_items):
        """Verify evaluate_item produces all required scalar and skills metrics."""
        evaluator = BenchmarkEvaluator(mock=True)
        res = evaluator.evaluate_item(sample_benchmark_items[0])
        assert "scalar_matches" in res
        assert "skills" in res
        assert "null_compliance" in res
        assert "evidence" in res
        assert res["source"] == "Hacker News"
        assert res["scalar_accuracy"] >= 0.70

    def test_evaluate_all_aggregation(self, sample_benchmark_items):
        """Verify evaluate_all computes overall and source-segmented metrics."""
        evaluator = BenchmarkEvaluator(mock=True)
        results = evaluator.evaluate_all(sample_benchmark_items)
        assert "overall" in results
        assert "field_level" in results
        assert "source_breakdown" in results
        assert "Hacker News" in results["source_breakdown"]
        assert "Greenhouse" in results["source_breakdown"]
        assert results["overall"]["exact_match_accuracy"] > 0.80

    def test_generate_report_markdown(self, sample_benchmark_items, tmp_path):
        """Verify generate_report renders a complete Markdown document and saves to disk."""
        evaluator = BenchmarkEvaluator(mock=True)
        out_report = tmp_path / "test_report.md"
        report_text = evaluator.generate_report(results=sample_benchmark_items, output_path=out_report)

        assert "# Twig Data Engineer Pipeline - Benchmark Evaluation Report" in report_text
        assert "## 1. Executive Summary & Source Breakdown" in report_text
        assert "## 2. Field-Level Exact Match Accuracy" in report_text
        assert "## 3. Technical Skills Extraction Metrics" in report_text
        assert "## 4. Strict Null Compliance Analysis" in report_text
        assert "## 5. Verbatim Evidence Grounding Audit" in report_text
        assert "## 6. Token & LLM Cost Accounting" in report_text
        assert out_report.exists()
        assert len(out_report.read_text(encoding="utf-8")) > 500


# ==============================================================================
# 7. CLI Parser and Execution Tests
# ==============================================================================


class TestCLIExecution:
    """Tests for CLI arguments, programmatic main, and subprocess invocation."""

    def test_arg_parser_defaults(self):
        """Verify default CLI arguments."""
        parser = build_arg_parser()
        args = parser.parse_args([])
        assert args.benchmark == str(DEFAULT_BENCHMARK_PATH)
        assert args.output == str(DEFAULT_REPORT_PATH)
        assert args.mock is True
        assert args.quiet is False

    def test_arg_parser_custom_flags(self):
        """Verify custom CLI flags."""
        parser = build_arg_parser()
        args = parser.parse_args([
            "--benchmark", "custom_benchmark.json",
            "--output", "custom_report.md",
            "--no-mock",
            "--quiet",
            "--model", "gpt-4o-mini",
        ])
        assert args.benchmark == "custom_benchmark.json"
        assert args.output == "custom_report.md"
        assert args.mock is False
        assert args.quiet is True
        assert args.model == "gpt-4o-mini"

    def test_programmatic_main_execution(self, tmp_path):
        """Verify programmatic execution via main() returns exit code 0."""
        out_report = tmp_path / "cli_report.md"
        exit_code = main([
            "--benchmark", str(DEFAULT_BENCHMARK_PATH),
            "--output", str(out_report),
            "--mock",
            "--quiet",
        ])
        assert exit_code == 0
        assert out_report.exists()
        assert out_report.stat().st_size > 500

    def test_programmatic_main_failure_handling(self, tmp_path):
        """Verify programmatic main returns code 1 on missing benchmark file."""
        exit_code = main([
            "--benchmark", str(tmp_path / "missing.json"),
            "--quiet",
        ])
        assert exit_code == 1

    def test_subprocess_module_execution(self, tmp_path):
        """Verify subprocess execution via `python -m pipeline.evaluate`."""
        out_report = tmp_path / "subproc_report.md"
        cmd = [
            sys.executable,
            "-m",
            "pipeline.evaluate",
            "--benchmark",
            str(DEFAULT_BENCHMARK_PATH),
            "--output",
            str(out_report),
            "--mock",
            "--quiet",
        ]
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        assert proc.returncode == 0
        assert out_report.exists()
        assert "Benchmark Evaluation Report" in out_report.read_text(encoding="utf-8")
