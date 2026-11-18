"""Automated pytest suite for hand-labeled benchmark dataset verification.

Validates that:
1. data/labeled_benchmark.json and pipeline/data/labeled_benchmark.json exist and parse cleanly.
2. Dataset contains exactly 20 items adhering to required source distribution:
   - >= 10 from Hacker News ('hackernews')
   - >= 5 from Greenhouse ('greenhouse')
   - >= 5 from Lever ('lever')
3. All 17 schema fields are strictly present on each ground truth entry:
   posting_id, company, title, seniority, city, country, remote_policy,
   salary_min, salary_max, currency, employment_type, skills, visa_sponsorship,
   posted_date, status, source_urls, evidence.
4. Verbatim evidence requirement: every snippet in evidence literally exists in raw_text.
5. Strict null compliance: unstated fields evaluate to None, never fabricated or guessed.
6. Pydantic validation: every record parses cleanly into canonical JobPostingRecord.
7. Realistic diversity: covers all seniority tiers, remote policies, and compensation types.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from pipeline.models.schema import (
    EmploymentTypeEnum,
    JobPostingRecord,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
)
from pipeline.models.validators import (
    validate_evidence_in_text,
    validate_record_evidence,
    validate_salary,
)

CODE_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_PATH = Path("data/labeled_benchmark.json") if Path("data/labeled_benchmark.json").exists() else (CODE_ROOT / "data" / "labeled_benchmark.json")
PIPELINE_BENCHMARK_PATH = Path("pipeline/data/labeled_benchmark.json") if Path("pipeline/data/labeled_benchmark.json").exists() else (CODE_ROOT / "pipeline" / "data" / "labeled_benchmark.json")

SCHEMA_17_FIELDS = [
    "posting_id",
    "company",
    "title",
    "seniority",
    "city",
    "country",
    "remote_policy",
    "salary_min",
    "salary_max",
    "currency",
    "employment_type",
    "skills",
    "visa_sponsorship",
    "posted_date",
    "status",
    "source_urls",
    "evidence",
]


@pytest.fixture(scope="module")
def benchmark_data() -> List[Dict[str, Any]]:
    """Loads and returns benchmark data from data/labeled_benchmark.json."""
    assert BENCHMARK_PATH.exists(), f"Benchmark file does not exist at {BENCHMARK_PATH}"
    with open(BENCHMARK_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, list), "Benchmark dataset root must be a JSON array"
    return data


# ==============================================================================
# 1. Existence, Integrity & Source Distribution Tests
# ==============================================================================


def test_benchmark_files_exist_and_match():
    """Verify both data/ and pipeline/data/ benchmark files exist and are identical."""
    assert BENCHMARK_PATH.exists(), f"Missing {BENCHMARK_PATH}"
    assert PIPELINE_BENCHMARK_PATH.exists(), f"Missing {PIPELINE_BENCHMARK_PATH}"

    with open(BENCHMARK_PATH, "r", encoding="utf-8") as f1, open(
        PIPELINE_BENCHMARK_PATH, "r", encoding="utf-8"
    ) as f2:
        d1 = json.load(f1)
        d2 = json.load(f2)

    assert d1 == d2, "data/labeled_benchmark.json and pipeline/data/labeled_benchmark.json must be identical"


def test_exact_twenty_items(benchmark_data: List[Dict[str, Any]]):
    """Verify the benchmark dataset contains exactly 20 items."""
    assert len(benchmark_data) == 20, f"Expected exactly 20 items, got {len(benchmark_data)}"


def test_source_distribution_requirements(benchmark_data: List[Dict[str, Any]]):
    """Verify source distribution meets rubric: >=10 HN, >=5 Greenhouse, >=5 Lever."""
    hn_items = [d for d in benchmark_data if d.get("source") == "hackernews"]
    gh_items = [d for d in benchmark_data if d.get("source") == "greenhouse"]
    lever_items = [d for d in benchmark_data if d.get("source") == "lever"]

    assert len(hn_items) >= 10, f"Expected >= 10 Hacker News items, got {len(hn_items)}"
    assert len(gh_items) >= 5, f"Expected >= 5 Greenhouse items, got {len(gh_items)}"
    assert len(lever_items) >= 5, f"Expected >= 5 Lever items, got {len(lever_items)}"

    assert len(hn_items) + len(gh_items) + len(lever_items) == 20


def test_benchmark_ids_unique_and_formatted(benchmark_data: List[Dict[str, Any]]):
    """Verify benchmark_ids are unique, non-empty, and follow naming conventions."""
    ids = [d.get("benchmark_id") for d in benchmark_data]
    assert len(ids) == len(set(ids)), f"Duplicate benchmark IDs found: {ids}"

    for d in benchmark_data:
        bid = d["benchmark_id"]
        source = d["source"]
        if source == "hackernews":
            assert bid.startswith("hn_")
        elif source == "greenhouse":
            assert bid.startswith("gh_")
        elif source == "lever":
            assert bid.startswith("lever_")


# ==============================================================================
# 2. Schema Structure & Field Coverage Tests
# ==============================================================================


@pytest.mark.parametrize("idx", range(20))
def test_all_17_schema_fields_present(benchmark_data: List[Dict[str, Any]], idx: int):
    """Verify all 17 schema fields are explicitly present in every ground truth entry."""
    item = benchmark_data[idx]
    bid = item.get("benchmark_id")
    gt = item.get("ground_truth", {})

    assert isinstance(gt, dict), f"[{bid}] ground_truth must be a dictionary"

    for field in SCHEMA_17_FIELDS:
        assert field in gt, f"[{bid}] Missing required schema field '{field}' in ground_truth"

    assert len(gt) == 17, (
        f"[{bid}] Expected exactly 17 schema fields in ground_truth, found {len(gt)}: "
        f"{sorted(list(gt.keys()))}"
    )


@pytest.mark.parametrize("idx", range(20))
def test_pydantic_job_posting_record_validation(benchmark_data: List[Dict[str, Any]], idx: int):
    """Verify every ground truth entry validates successfully against Pydantic JobPostingRecord."""
    item = benchmark_data[idx]
    bid = item.get("benchmark_id")
    gt = item.get("ground_truth", {})

    now = datetime.now(timezone.utc)
    record = JobPostingRecord(
        first_seen=now,
        last_seen=now,
        **gt,
    )

    assert record.posting_id == gt["posting_id"]
    assert record.company == gt["company"]
    assert record.title == gt["title"]
    assert record.status == StatusEnum.open


# ==============================================================================
# 3. Verbatim Evidence Tests
# ==============================================================================


@pytest.mark.parametrize("idx", range(20))
def test_verbatim_evidence_snippets_in_raw_text(benchmark_data: List[Dict[str, Any]], idx: int):
    """Verify every extracted evidence snippet actually exists verbatim in raw_text."""
    item = benchmark_data[idx]
    bid = item.get("benchmark_id")
    raw = item.get("raw_text", "")
    gt = item.get("ground_truth", {})
    evidence = gt.get("evidence", {})

    assert isinstance(raw, str) and len(raw) > 0, f"[{bid}] raw_text must be non-empty"
    assert isinstance(evidence, dict) and len(evidence) > 0, f"[{bid}] evidence dict must not be empty"

    for field, snippet in evidence.items():
        assert isinstance(snippet, str) and snippet.strip(), (
            f"[{bid}] Evidence snippet for '{field}' must be a non-empty string"
        )
        assert validate_evidence_in_text(snippet, raw, case_sensitive=False), (
            f"[{bid}] Evidence snippet for '{field}' was not found in raw_text.\n"
            f"Snippet: {snippet!r}\n"
            f"Raw text excerpt: {raw[:200]!r}..."
        )


@pytest.mark.parametrize("idx", range(20))
def test_evidence_presence_for_populated_fields(benchmark_data: List[Dict[str, Any]], idx: int):
    """Verify that all non-null auditable fields have valid evidence justifications."""
    item = benchmark_data[idx]
    bid = item.get("benchmark_id")
    gt = item.get("ground_truth", {})

    validation_results = validate_record_evidence(gt)
    for field_name, is_valid in validation_results.items():
        assert is_valid, (
            f"[{bid}] Field '{field_name}' is populated ({gt.get(field_name)!r}) "
            f"but lacks a valid non-empty snippet in evidence: {gt.get('evidence')}"
        )


# ==============================================================================
# 4. Strict Null Compliance Tests
# ==============================================================================


@pytest.mark.parametrize("idx", range(20))
def test_strict_null_compliance_and_no_hallucinations(benchmark_data: List[Dict[str, Any]], idx: int):
    """Verify that unstated fields evaluate strictly to None with no fabricated evidence."""
    item = benchmark_data[idx]
    bid = item.get("benchmark_id")
    gt = item.get("ground_truth", {})
    evidence = gt.get("evidence", {})

    # Compensation null rule
    if gt["salary_min"] is None:
        assert gt["salary_max"] is None, f"[{bid}] salary_max must be None if salary_min is None"
        assert gt["currency"] is None, f"[{bid}] currency must be None if compensation is unstated"
        assert "salary" not in evidence, f"[{bid}] 'salary' evidence present despite None salary"
        assert "salary_min" not in evidence
        assert "salary_max" not in evidence
    else:
        assert gt["salary_min"] > 0, f"[{bid}] salary_min must be positive"
        assert gt["salary_max"] >= gt["salary_min"], f"[{bid}] salary_max must be >= salary_min"
        assert gt["currency"] is not None, f"[{bid}] currency must be specified when salary is stated"
        assert validate_salary(gt["salary_min"], gt["salary_max"], gt["currency"])

    # Visa null rule
    if gt["visa_sponsorship"] is None:
        assert "visa_sponsorship" not in evidence, (
            f"[{bid}] visa_sponsorship evidence present despite None value"
        )
    else:
        assert isinstance(gt["visa_sponsorship"], bool)
        assert "visa_sponsorship" in evidence

    # Seniority null rule
    if gt["seniority"] is None:
        assert "seniority" not in evidence, (
            f"[{bid}] seniority evidence present despite None value"
        )
    else:
        assert gt["seniority"] in {e.value for e in SeniorityEnum}

    # Employment type null rule
    if gt["employment_type"] is None:
        assert "employment_type" not in evidence, (
            f"[{bid}] employment_type evidence present despite None value"
        )
    else:
        assert gt["employment_type"] in {e.value for e in EmploymentTypeEnum}


# ==============================================================================
# 5. Diversity & Rubric Representation Tests
# ==============================================================================


def test_seniority_tier_diversity(benchmark_data: List[Dict[str, Any]]):
    """Verify benchmark represents multiple seniority tiers (junior, mid, senior, staff_plus, None)."""
    seniorities = {item["ground_truth"]["seniority"] for item in benchmark_data}
    assert "junior" in seniorities, "Benchmark must contain junior/entry roles"
    assert "senior" in seniorities, "Benchmark must contain senior roles"
    assert "staff_plus" in seniorities, "Benchmark must contain staff_plus roles"
    assert None in seniorities, "Benchmark must contain unadorned roles evaluating to None"


def test_remote_policy_diversity(benchmark_data: List[Dict[str, Any]]):
    """Verify benchmark represents remote, hybrid, and onsite workplace policies."""
    policies = {item["ground_truth"]["remote_policy"] for item in benchmark_data}
    assert "remote" in policies, "Benchmark must contain remote roles"
    assert "hybrid" in policies, "Benchmark must contain hybrid roles"
    assert "onsite" in policies, "Benchmark must contain onsite roles"


def test_employment_type_diversity(benchmark_data: List[Dict[str, Any]]):
    """Verify benchmark represents full_time, contract, intern, and unstated types."""
    emp_types = {item["ground_truth"]["employment_type"] for item in benchmark_data}
    assert "full_time" in emp_types, "Benchmark must contain full_time roles"
    assert "contract" in emp_types, "Benchmark must contain contract roles"
    assert "intern" in emp_types, "Benchmark must contain intern roles"
    assert None in emp_types, "Benchmark must contain postings with unstated employment_type"


def test_visa_sponsorship_diversity(benchmark_data: List[Dict[str, Any]]):
    """Verify benchmark represents True, False, and None visa sponsorship states."""
    visas = {item["ground_truth"]["visa_sponsorship"] for item in benchmark_data}
    assert True in visas, "Benchmark must contain roles offering visa sponsorship"
    assert False in visas, "Benchmark must contain roles explicitly denying visa sponsorship"
    assert None in visas, "Benchmark must contain roles with unstated visa sponsorship"
