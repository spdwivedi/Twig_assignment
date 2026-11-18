"""Comprehensive unit tests for the CandidateBlocker and indexing engine.

Validates:
- Multi-pass blocking key generation (Primary, Secondary, Tertiary).
- Company normalization and legal entity stripping.
- Role, remote, state, and seniority extraction.
- Inverted index construction with 1-member block pruning.
- Candidate pair extraction with symmetric deduplication (id1 < id2).
- Zero duplicate comparisons across overlapping multi-pass blocks.
- Scaling efficiency (>95% reduction ratio vs O(N^2) pairwise comparisons).
- Edge cases: missing company, title, location, seniority, empty lists, single items.
- Dict postings and Pydantic JobPostingRecord interoperability.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List

import pytest

from pipeline.dedup.blocker import CandidateBlocker
from pipeline.models.schema import (
    EmploymentTypeEnum,
    JobPostingRecord,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
)


@pytest.fixture
def blocker() -> CandidateBlocker:
    """Fixture providing a fresh CandidateBlocker instance."""
    return CandidateBlocker()


@pytest.fixture
def sample_now() -> datetime:
    """Standard timestamp for testing."""
    return datetime.now(timezone.utc)


# ==============================================================================
# 1. Company Normalization & Legal Entity Stripping Tests
# ==============================================================================


def test_normalize_company_basic(blocker: CandidateBlocker) -> None:
    """Tests basic lowercase and whitespace trimming."""
    assert blocker.normalize_company("Stripe") == "stripe"
    assert blocker.normalize_company("  Google  ") == "google"
    assert blocker.normalize_company("netflix") == "netflix"


def test_normalize_company_legal_suffixes(blocker: CandidateBlocker) -> None:
    """Tests stripping corporate suffixes (Inc, LLC, Corp, Technologies, Labs, etc.)."""
    assert blocker.normalize_company("Stripe, Inc.") == "stripe"
    assert blocker.normalize_company("Stripe Inc") == "stripe"
    assert blocker.normalize_company("Palantir Technologies") == "palantir"
    assert blocker.normalize_company("Databricks, LLC") == "databricks"
    assert blocker.normalize_company("Snowflake Inc.") == "snowflake"
    assert blocker.normalize_company("OpenAI, LLC") == "openai"
    assert blocker.normalize_company("Two Sigma Investments, LLC") == "two_sigma_investments"


def test_normalize_company_aliases(blocker: CandidateBlocker) -> None:
    """Tests canonical entity aliases."""
    assert blocker.normalize_company("Meta Platforms, Inc.") == "meta"
    assert blocker.normalize_company("Facebook") == "meta"
    assert blocker.normalize_company("Google LLC") == "google"
    assert blocker.normalize_company("Amazon.com") == "amazon"


def test_normalize_company_edge_cases(blocker: CandidateBlocker) -> None:
    """Tests None, empty strings, and special characters."""
    assert blocker.normalize_company(None) is None
    assert blocker.normalize_company("") is None
    assert blocker.normalize_company("   ") is None
    assert blocker.normalize_company("---") is None


# ==============================================================================
# 2. Token Disentanglement (Role, Remote/State, Seniority) Tests
# ==============================================================================


def test_extract_core_role_target_roles(blocker: CandidateBlocker) -> None:
    """Tests extraction of standard target core roles."""
    # Data Engineering
    assert blocker.extract_core_role({"title": "Data Engineer"}) == "data_engineer"
    assert blocker.extract_core_role({"title": "Senior Data Engineer"}) == "data_engineer"
    assert blocker.extract_core_role({"title": "Data Engineer, Infrastructure"}) == "data_engineer"
    assert blocker.extract_core_role({"title": "Big Data Engineer"}) == "data_engineer"
    assert blocker.extract_core_role({"title": "Software Engineer - Data"}) == "data_engineer"
    assert blocker.extract_core_role({"title": "ETL Developer"}) == "data_engineer"

    # Data Platform
    assert blocker.extract_core_role({"title": "Data Platform Engineer"}) == "data_platform_engineer"
    assert blocker.extract_core_role({"title": "Data Infrastructure Engineer"}) == "data_platform_engineer"

    # ML & AI
    assert blocker.extract_core_role({"title": "Machine Learning Engineer"}) == "machine_learning_engineer"
    assert blocker.extract_core_role({"title": "Sr. ML Engineer"}) == "machine_learning_engineer"
    assert blocker.extract_core_role({"title": "Software Engineer, Machine Learning"}) == "machine_learning_engineer"
    assert blocker.extract_core_role({"title": "MLOps Engineer"}) == "mlops_engineer"

    # Analytics Engineer
    assert blocker.extract_core_role({"title": "Analytics Engineer"}) == "analytics_engineer"
    assert blocker.extract_core_role({"title": "Lead Analytics Engineer"}) == "analytics_engineer"

    # Data Scientist
    assert blocker.extract_core_role({"title": "Data Scientist"}) == "data_scientist"
    assert blocker.extract_core_role({"title": "Staff Data Scientist"}) == "data_scientist"


def test_extract_core_role_explicit_override(blocker: CandidateBlocker) -> None:
    """Tests explicit core_role field takes priority."""
    posting = {"title": "Full Stack Wizard", "core_role": "data_engineer"}
    assert blocker.extract_core_role(posting) == "data_engineer"


def test_extract_remote_or_state(blocker: CandidateBlocker) -> None:
    """Tests extraction of remote policies and US 2-letter state codes."""
    # Remote
    assert "remote" in blocker.extract_remote_or_state({"location": "Remote, US"})
    assert "remote" in blocker.extract_remote_or_state({"remote_policy": "remote"})
    assert "remote" in blocker.extract_remote_or_state({"title": "Data Engineer (Remote)"})

    # US State codes
    assert "CA" in blocker.extract_remote_or_state({"location": "San Francisco, CA"})
    assert "NY" in blocker.extract_remote_or_state({"location": "New York, NY"})
    assert "TX" in blocker.extract_remote_or_state({"location": "Austin, Texas"})
    assert "WA" in blocker.extract_remote_or_state({"city": "Seattle", "state": "WA"})

    # Remote AND State together
    tokens = blocker.extract_remote_or_state(
        {"location": "San Francisco, CA", "remote_policy": "remote"}
    )
    assert "remote" in tokens
    assert "CA" in tokens


def test_extract_seniority(blocker: CandidateBlocker) -> None:
    """Tests extraction of standardized seniority levels."""
    # From explicit field
    assert blocker.extract_seniority({"seniority": "senior"}) == "senior"
    assert blocker.extract_seniority({"seniority": SeniorityEnum.staff_plus}) == "staff_plus"
    assert blocker.extract_seniority({"seniority": "sr."}) == "senior"
    assert blocker.extract_seniority({"seniority": "intern"}) == "junior"

    # From title
    assert blocker.extract_seniority({"title": "Senior Data Engineer"}) == "senior"
    assert blocker.extract_seniority({"title": "Sr. Analytics Engineer"}) == "senior"
    assert blocker.extract_seniority({"title": "Staff Data Engineer"}) == "staff_plus"
    assert blocker.extract_seniority({"title": "Principal ML Engineer"}) == "staff_plus"
    assert blocker.extract_seniority({"title": "Lead Platform Engineer"}) == "senior"
    assert blocker.extract_seniority({"title": "Junior Data Engineer"}) == "junior"
    assert blocker.extract_seniority({"title": "Associate Data Scientist"}) == "junior"
    assert blocker.extract_seniority({"title": "Mid-level SWE"}) == "mid"
    assert blocker.extract_seniority({"title": "Data Engineer"}) is None


# ==============================================================================
# 3. Multi-Pass Blocking Key Generation Tests
# ==============================================================================


def test_generate_blocking_keys_full_post(blocker: CandidateBlocker) -> None:
    """Tests generating primary, secondary, and tertiary keys for a complete posting."""
    posting = {
        "id": "p1",
        "company": "Stripe, Inc.",
        "title": "Senior Data Engineer",
        "location": "San Francisco, CA",
        "remote_policy": "remote",
        "seniority": "senior",
    }
    keys = blocker.generate_blocking_keys(posting)

    # Primary key: company + core_role
    assert "stripe:data_engineer" in keys
    # Secondary key: company + remote or state
    assert "stripe:remote" in keys
    assert "stripe:CA" in keys
    # Tertiary key: company + seniority
    assert "stripe:senior" in keys


def test_generate_blocking_keys_ambiguous_role(blocker: CandidateBlocker) -> None:
    """Tests secondary key generation for multi-role or ambiguous postings."""
    posting = {
        "id": "p2",
        "company": "Databricks",
        "title": "Member of Technical Staff",
        "location": "Mountain View, CA",
    }
    keys = blocker.generate_blocking_keys(posting)
    assert "databricks:CA" in keys
    assert "databricks:staff_plus" in keys


def test_generate_blocking_keys_with_pydantic_model(
    blocker: CandidateBlocker, sample_now: datetime
) -> None:
    """Tests key generation with official JobPostingRecord Pydantic model."""
    record = JobPostingRecord(
        posting_id="det_12345",
        company="Stripe",
        title="Senior Machine Learning Engineer",
        seniority=SeniorityEnum.senior,
        city="San Francisco",
        country="US",
        remote_policy=RemotePolicyEnum.remote,
        salary_min=170000.0,
        salary_max=240000.0,
        currency="USD",
        employment_type=EmploymentTypeEnum.full_time,
        skills=["Python", "PyTorch"],
        source_urls=["https://stripe.com/jobs/123"],
        first_seen=sample_now,
        last_seen=sample_now,
        status=StatusEnum.open,
    )
    keys = blocker.generate_blocking_keys(record)
    assert "stripe:machine_learning_engineer" in keys
    assert "stripe:remote" in keys
    assert "stripe:senior" in keys


# ==============================================================================
# 4. Inverted Index Construction Tests
# ==============================================================================


def test_index_postings_filters_singletons(blocker: CandidateBlocker) -> None:
    """Tests that blocks with only 1 member are pruned from the inverted index."""
    postings = [
        # Stripe postings (will share stripe:data_engineer block)
        {"id": "p1", "company": "Stripe", "title": "Data Engineer", "location": "Remote"},
        {"id": "p2", "company": "Stripe", "title": "Sr Data Engineer", "location": "Remote"},
        # Lone Google posting (no other Google postings)
        {"id": "p3", "company": "Google", "title": "ML Engineer", "location": "Mountain View, CA"},
    ]
    blocks = blocker.index_postings(postings)

    # stripe:data_engineer has 2 members -> retained
    assert "stripe:data_engineer" in blocks
    assert len(blocks["stripe:data_engineer"]) == 2

    # google:machine_learning_engineer has only 1 member -> pruned!
    assert "google:machine_learning_engineer" not in blocks
    assert "google:CA" not in blocks


def test_index_postings_empty(blocker: CandidateBlocker) -> None:
    """Tests indexing an empty list returns an empty dict."""
    assert blocker.index_postings([]) == {}


# ==============================================================================
# 5. Candidate Pair Extraction & Symmetric Deduplication Tests
# ==============================================================================


def test_get_candidate_pairs_symmetric_dedup(blocker: CandidateBlocker) -> None:
    """Tests that candidate pairs are strictly sorted tuples (id1, id2) with id1 < id2."""
    postings = [
        {"id": "posting_b", "company": "Stripe", "title": "Data Engineer"},
        {"id": "posting_a", "company": "Stripe", "title": "Data Engineer"},
    ]
    pairs = blocker.get_candidate_pairs(postings)

    # Must contain ("posting_a", "posting_b"), NOT ("posting_b", "posting_a")
    assert pairs == {("posting_a", "posting_b")}
    pair = next(iter(pairs))
    assert pair[0] < pair[1]


def test_get_candidate_pairs_zero_duplicate_across_passes(
    blocker: CandidateBlocker,
) -> None:
    """Tests zero duplicate pair comparisons across overlapping blocking passes."""
    # Postings that match across Primary (role), Secondary (location), and Tertiary (seniority)
    postings = [
        {
            "id": "p1",
            "company": "Stripe",
            "title": "Senior Data Engineer",
            "location": "San Francisco, CA",
            "remote_policy": "remote",
            "seniority": "senior",
        },
        {
            "id": "p2",
            "company": "Stripe, Inc.",
            "title": "Sr. Data Engineer",
            "location": "San Francisco, CA",
            "remote_policy": "remote",
            "seniority": "senior",
        },
    ]
    # They share: stripe:data_engineer, stripe:remote, stripe:CA, stripe:senior (4 blocks!)
    pairs = blocker.get_candidate_pairs(postings)

    # Even though they appear in 4 common blocks, candidate_pairs must contain them EXACTLY ONCE
    assert len(pairs) == 1
    assert ("p1", "p2") in pairs


def test_get_candidate_pairs_cross_company_isolation(
    blocker: CandidateBlocker,
) -> None:
    """Tests that postings from different companies are NEVER compared."""
    postings = [
        {"id": "s1", "company": "Stripe", "title": "Data Engineer"},
        {"id": "s2", "company": "Stripe", "title": "Senior Data Engineer"},
        {"id": "g1", "company": "Google", "title": "Data Engineer"},
        {"id": "g2", "company": "Google", "title": "Senior Data Engineer"},
    ]
    pairs = blocker.get_candidate_pairs(postings)

    # Stripe pairs together, Google pairs together
    assert ("s1", "s2") in pairs
    assert ("g1", "g2") in pairs

    # Zero cross-company pairs
    for id1, id2 in pairs:
        assert not (id1.startswith("s") and id2.startswith("g"))


# ==============================================================================
# 6. Scaling Efficiency & O(N^2) Reduction Tests
# ==============================================================================


def test_get_blocking_efficiency_calculation(blocker: CandidateBlocker) -> None:
    """Tests the theoretical N(N-1)/2 vs candidate pairs calculation."""
    # N = 100, candidate pairs = 200
    metrics = blocker.get_blocking_efficiency(num_total_postings=100, num_candidate_pairs=200)

    theoretical = (100 * 99) // 2  # 4950
    assert metrics["num_total_postings"] == 100
    assert metrics["theoretical_pairs"] == theoretical
    assert metrics["num_candidate_pairs"] == 200
    assert metrics["pairs_eliminated"] == 4750

    # Reduction ratio: 4750 / 4950 = 0.9596
    expected_ratio = round(4750.0 / 4950.0, 4)
    assert metrics["reduction_ratio"] == expected_ratio
    assert metrics["reduction_percentage"] == round(expected_ratio * 100.0, 2)
    assert metrics["reduction_ratio"] > 0.95


def test_get_blocking_efficiency_boundary_conditions(blocker: CandidateBlocker) -> None:
    """Tests edge cases for N=0, N=1, N=2."""
    zero_m = blocker.get_blocking_efficiency(0, 0)
    assert zero_m["theoretical_pairs"] == 0
    assert zero_m["reduction_ratio"] == 1.0

    one_m = blocker.get_blocking_efficiency(1, 0)
    assert one_m["theoretical_pairs"] == 0
    assert one_m["reduction_ratio"] == 1.0

    two_m = blocker.get_blocking_efficiency(2, 1)
    assert two_m["theoretical_pairs"] == 1
    assert two_m["reduction_ratio"] == 0.0


def test_scaling_efficiency_realistic_dataset(blocker: CandidateBlocker) -> None:
    """Tests that blocker achieves >95% reduction on a realistic multi-company dataset."""
    companies = [
        "Stripe", "Databricks", "Snowflake", "Meta", "Google",
        "Netflix", "Airbnb", "Uber", "Palantir", "Pinterest",
        "DoorDash", "Robinhood", "Coinbase", "Instacart", "Figma",
        "Notion", "Dropbox", "Square", "Lyft", "Reddit"
    ]
    roles = [
        ("Senior Data Engineer", "San Francisco, CA"),
        ("Data Engineer II", "Remote"),
        ("Lead Analytics Engineer", "New York, NY"),
        ("Staff ML Engineer", "Seattle, WA"),
        ("Machine Learning Engineer", "Remote"),
    ]

    mock_postings: List[Dict[str, Any]] = []
    post_idx = 0
    for company in companies:
        for title, loc in roles:
            mock_postings.append({
                "id": f"job_{post_idx:04d}",
                "company": company,
                "title": title,
                "location": loc,
            })
            post_idx += 1

    total_postings = len(mock_postings)  # 20 * 5 = 100 postings
    candidate_pairs = blocker.get_candidate_pairs(mock_postings)
    efficiency = blocker.get_blocking_efficiency(total_postings, len(candidate_pairs))

    # Theoretical combinations: 100 * 99 / 2 = 4,950
    assert efficiency["theoretical_pairs"] == 4950
    # Search space reduction MUST be > 95%
    assert efficiency["reduction_ratio"] > 0.95, (
        f"Expected > 95% reduction, got {efficiency['reduction_percentage']}% "
        f"({len(candidate_pairs)} pairs evaluated vs {efficiency['theoretical_pairs']})"
    )


# ==============================================================================
# 7. Edge Cases & Robustness Tests
# ==============================================================================


def test_edge_case_missing_company(blocker: CandidateBlocker) -> None:
    """Tests postings with None or missing company do not crash and generate no keys."""
    posting_none = {"id": "1", "company": None, "title": "Data Engineer"}
    posting_missing = {"id": "2", "title": "Data Engineer"}
    posting_empty = {"id": "3", "company": "", "title": "Data Engineer"}

    assert blocker.generate_blocking_keys(posting_none) == []
    assert blocker.generate_blocking_keys(posting_missing) == []
    assert blocker.generate_blocking_keys(posting_empty) == []

    # Ensure get_candidate_pairs handles them safely
    pairs = blocker.get_candidate_pairs([posting_none, posting_missing])
    assert pairs == set()


def test_edge_case_missing_role(blocker: CandidateBlocker) -> None:
    """Tests postings with None or missing title still generate secondary/tertiary keys."""
    posting = {"id": "1", "company": "Stripe", "title": None, "location": "Remote"}
    keys = blocker.generate_blocking_keys(posting)

    # Primary key omitted, secondary key present
    assert "stripe:remote" in keys
    assert not any(k.startswith("stripe:data") for k in keys)


def test_edge_case_missing_location(blocker: CandidateBlocker) -> None:
    """Tests postings with None location still generate primary keys."""
    posting = {"id": "1", "company": "Stripe", "title": "Data Engineer", "location": None}
    keys = blocker.generate_blocking_keys(posting)

    assert "stripe:data_engineer" in keys
    assert "stripe:remote" not in keys
    assert "stripe:CA" not in keys


def test_edge_case_missing_seniority(blocker: CandidateBlocker) -> None:
    """Tests postings without seniority do not generate tertiary keys."""
    posting = {
        "id": "1",
        "company": "Stripe",
        "title": "Data Engineer",
        "seniority": None,
        "location": "Remote",
    }
    keys = blocker.generate_blocking_keys(posting)

    assert "stripe:data_engineer" in keys
    assert "stripe:remote" in keys
    assert "stripe:senior" not in keys
    assert "stripe:junior" not in keys


def test_edge_case_all_fields_none(blocker: CandidateBlocker) -> None:
    """Tests posting with all None fields."""
    posting = {"id": "1", "company": None, "title": None, "location": None, "seniority": None}
    assert blocker.generate_blocking_keys(posting) == []
    assert blocker.get_candidate_pairs([posting]) == set()


def test_edge_case_duplicate_ids_in_input(blocker: CandidateBlocker) -> None:
    """Tests that identical posting IDs do not pair with themselves."""
    postings = [
        {"id": "p1", "company": "Stripe", "title": "Data Engineer"},
        {"id": "p1", "company": "Stripe", "title": "Data Engineer"},
    ]
    pairs = blocker.get_candidate_pairs(postings)
    # A posting is never compared with itself
    assert ("p1", "p1") not in pairs
    assert pairs == set()


def test_edge_case_heterogeneous_types(
    blocker: CandidateBlocker, sample_now: datetime
) -> None:
    """Tests blocking across a mix of dicts and Pydantic JobPostingRecord objects."""
    pydantic_post = JobPostingRecord(
        posting_id="rec_001",
        company="Stripe",
        title="Senior Data Engineer",
        seniority=SeniorityEnum.senior,
        city="San Francisco",
        country="US",
        remote_policy=RemotePolicyEnum.onsite,
        status=StatusEnum.open,
        first_seen=sample_now,
        last_seen=sample_now,
    )
    dict_post = {
        "id": "dict_002",
        "company": "Stripe, Inc.",
        "title": "Sr. Data Engineer",
        "location": "San Francisco, CA",
        "seniority": "senior",
    }

    pairs = blocker.get_candidate_pairs([pydantic_post, dict_post])
    assert len(pairs) == 1
    assert ("dict_002", "rec_001") in pairs or ("rec_001", "dict_002") in pairs
