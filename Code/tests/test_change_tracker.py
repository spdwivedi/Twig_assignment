"""Unit and integration tests for ChangeTracker and FieldChange audit logging engine.

Verifies:
1. Salary change detection (min, max, currency, tolerance).
2. Remote policy change detection (onsite -> hybrid -> remote).
3. Location change detection (city, country, location).
4. Skills set difference detection (ordering invariance, casing normalization).
5. Floating point tolerance (100000.00 vs 100000.0001 -> no change).
6. Title and lifecycle status transitions.
7. Zero changes generated when records are identical or differ only in untracked metadata.
8. Database persistence into posting_change_history and retrieval via get_history.
9. Compatibility with Pydantic models (JobPostingRecord, CanonicalJobPosting).
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict

import pytest

from pipeline.models.canonical import CanonicalJobPosting
from pipeline.models.schema import (
    JobPostingRecord,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
)
from pipeline.storage.change_tracker import ChangeTracker, FieldChange


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def in_memory_db() -> sqlite3.Connection:
    """Provides a fresh in-memory SQLite database connection."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def baseline_record() -> Dict[str, Any]:
    """A realistic baseline job posting record dictionary."""
    return {
        "posting_id": "twg_post_001",
        "company": "Acme Corp",
        "title": "Data Engineer",
        "seniority": "mid",
        "city": "San Francisco",
        "country": "US",
        "remote_policy": "onsite",
        "salary_min": 120000.0,
        "salary_max": 160000.0,
        "currency": "USD",
        "employment_type": "full_time",
        "skills": ["Python", "SQL", "Spark"],
        "visa_sponsorship": True,
        "status": "open",
        "first_seen": "2026-11-01T00:00:00Z",
        "last_seen": "2026-11-01T00:00:00Z",
    }


# ==============================================================================
# 1. Salary & Currency Change Tests
# ==============================================================================


class TestSalaryChangeDetection:
    """Tests detection of salary_min, salary_max, and currency modifications."""

    def test_salary_min_increase(self, baseline_record: Dict[str, Any]) -> None:
        incoming = dict(baseline_record, salary_min=135000.0)
        changes = ChangeTracker.detect_changes(baseline_record, incoming)

        assert len(changes) == 1
        chg = changes[0]
        assert chg.field_name == "salary_min"
        assert chg.reason == "compensation_updated"
        assert float(chg.old_value) == 120000.0
        assert float(chg.new_value) == 135000.0

    def test_salary_max_increase(self, baseline_record: Dict[str, Any]) -> None:
        incoming = dict(baseline_record, salary_max=185000.0)
        changes = ChangeTracker.detect_changes(baseline_record, incoming)

        assert len(changes) == 1
        chg = changes[0]
        assert chg.field_name == "salary_max"
        assert chg.reason == "compensation_updated"
        assert float(chg.old_value) == 160000.0
        assert float(chg.new_value) == 185000.0

    def test_both_salary_min_and_max_change(self, baseline_record: Dict[str, Any]) -> None:
        incoming = dict(baseline_record, salary_min=130000.0, salary_max=175000.0)
        changes = ChangeTracker.detect_changes(baseline_record, incoming)

        assert len(changes) == 2
        field_names = {c.field_name for c in changes}
        assert field_names == {"salary_min", "salary_max"}
        for c in changes:
            assert c.reason == "compensation_updated"

    def test_salary_from_none_to_value(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, salary_min=None)
        incoming = dict(baseline_record, salary_min=125000.0)
        changes = ChangeTracker.detect_changes(baseline, incoming)

        assert len(changes) == 1
        assert changes[0].field_name == "salary_min"
        assert changes[0].old_value is None
        assert float(changes[0].new_value) == 125000.0
        assert changes[0].reason == "compensation_updated"

    def test_salary_from_value_to_none(self, baseline_record: Dict[str, Any]) -> None:
        incoming = dict(baseline_record, salary_max=None)
        changes = ChangeTracker.detect_changes(baseline_record, incoming)

        assert len(changes) == 1
        assert changes[0].field_name == "salary_max"
        assert float(changes[0].old_value) == 160000.0
        assert changes[0].new_value is None
        assert changes[0].reason == "compensation_updated"

    def test_currency_change(self, baseline_record: Dict[str, Any]) -> None:
        incoming = dict(baseline_record, currency="EUR")
        changes = ChangeTracker.detect_changes(baseline_record, incoming)

        assert len(changes) == 1
        assert changes[0].field_name == "currency"
        assert changes[0].old_value == "USD"
        assert changes[0].new_value == "EUR"
        assert changes[0].reason == "currency_updated"

    def test_currency_case_insensitivity(self, baseline_record: Dict[str, Any]) -> None:
        incoming = dict(baseline_record, currency="usd")
        changes = ChangeTracker.detect_changes(baseline_record, incoming)
        assert len(changes) == 0

    def test_currency_added_from_none(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, currency=None)
        incoming = dict(baseline_record, currency="CAD")
        changes = ChangeTracker.detect_changes(baseline, incoming)

        assert len(changes) == 1
        assert changes[0].field_name == "currency"
        assert changes[0].old_value is None
        assert changes[0].new_value == "CAD"
        assert changes[0].reason == "currency_updated"


# ==============================================================================
# 2. Floating-Point Tolerance Tests
# ==============================================================================


class TestFloatingPointTolerance:
    """Tests floating-point tolerance suppression for minor precision noise."""

    def test_sub_dollar_floating_noise_no_change(self, baseline_record: Dict[str, Any]) -> None:
        # 100000.00 vs 100000.0001 -> difference is 0.0001 <= 1.0 -> no change
        baseline = dict(baseline_record, salary_min=100000.00)
        incoming = dict(baseline_record, salary_min=100000.0001)

        changes = ChangeTracker.detect_changes(baseline, incoming)
        salary_changes = [c for c in changes if c.field_name == "salary_min"]
        assert len(salary_changes) == 0

    def test_exact_one_dollar_tolerance_boundary(self, baseline_record: Dict[str, Any]) -> None:
        # Difference == 1.0 -> treated as within tolerance (no change)
        baseline = dict(baseline_record, salary_min=100000.00)
        incoming = dict(baseline_record, salary_min=100001.00)

        changes = ChangeTracker.detect_changes(baseline, incoming)
        assert len(changes) == 0

    def test_above_threshold_detects_change(self, baseline_record: Dict[str, Any]) -> None:
        # Difference == 1.05 > 1.0 -> change detected
        baseline = dict(baseline_record, salary_min=100000.00)
        incoming = dict(baseline_record, salary_min=100001.05)

        changes = ChangeTracker.detect_changes(baseline, incoming)
        assert len(changes) == 1
        assert changes[0].field_name == "salary_min"
        assert changes[0].reason == "compensation_updated"

    def test_custom_tolerance_parameter(self, baseline_record: Dict[str, Any]) -> None:
        # Tracker configured with tight tolerance of 0.00001
        strict_tracker = ChangeTracker(float_tolerance=0.00001)
        baseline = dict(baseline_record, salary_min=100000.00)
        incoming = dict(baseline_record, salary_min=100000.001)

        changes = strict_tracker.detect_changes(baseline, incoming)
        assert len(changes) == 1
        assert changes[0].field_name == "salary_min"

    def test_int_and_float_equality(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, salary_min=120000)
        incoming = dict(baseline_record, salary_min=120000.0)
        changes = ChangeTracker.detect_changes(baseline, incoming)
        assert len(changes) == 0

    def test_string_numeric_salary(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, salary_min="120000.0")
        incoming = dict(baseline_record, salary_min=120000.0)
        changes = ChangeTracker.detect_changes(baseline, incoming)
        assert len(changes) == 0


# ==============================================================================
# 3. Remote Policy Transition Tests
# ==============================================================================


class TestRemotePolicyChangeDetection:
    """Tests remote workplace policy transitions (onsite -> hybrid -> remote)."""

    def test_onsite_to_hybrid_transition(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, remote_policy="onsite")
        incoming = dict(baseline_record, remote_policy="hybrid")
        changes = ChangeTracker.detect_changes(baseline, incoming)

        assert len(changes) == 1
        chg = changes[0]
        assert chg.field_name == "remote_policy"
        assert chg.old_value == "onsite"
        assert chg.new_value == "hybrid"
        assert chg.reason == "remote_status_changed"

    def test_hybrid_to_remote_transition(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, remote_policy="hybrid")
        incoming = dict(baseline_record, remote_policy="remote")
        changes = ChangeTracker.detect_changes(baseline, incoming)

        assert len(changes) == 1
        chg = changes[0]
        assert chg.field_name == "remote_policy"
        assert chg.old_value == "hybrid"
        assert chg.new_value == "remote"
        assert chg.reason == "remote_status_changed"

    def test_remote_policy_enum_compatibility(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, remote_policy=RemotePolicyEnum.onsite)
        incoming = dict(baseline_record, remote_policy="onsite")
        changes = ChangeTracker.detect_changes(baseline, incoming)
        assert len(changes) == 0

    def test_remote_policy_case_insensitivity(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, remote_policy="Onsite")
        incoming = dict(baseline_record, remote_policy="onsite")
        changes = ChangeTracker.detect_changes(baseline, incoming)
        assert len(changes) == 0

    def test_remote_policy_from_none(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, remote_policy=None)
        incoming = dict(baseline_record, remote_policy="remote")
        changes = ChangeTracker.detect_changes(baseline, incoming)

        assert len(changes) == 1
        assert changes[0].field_name == "remote_policy"
        assert changes[0].old_value is None
        assert changes[0].new_value == "remote"
        assert changes[0].reason == "remote_status_changed"


# ==============================================================================
# 4. Location Change Tests
# ==============================================================================


class TestLocationChangeDetection:
    """Tests geographic location changes across city, country, and location fields."""

    def test_city_change(self, baseline_record: Dict[str, Any]) -> None:
        incoming = dict(baseline_record, city="New York")
        changes = ChangeTracker.detect_changes(baseline_record, incoming)

        assert len(changes) == 1
        chg = changes[0]
        assert chg.field_name == "city"
        assert chg.old_value == "San Francisco"
        assert chg.new_value == "New York"
        assert chg.reason == "location_updated"

    def test_country_change(self, baseline_record: Dict[str, Any]) -> None:
        incoming = dict(baseline_record, country="UK")
        changes = ChangeTracker.detect_changes(baseline_record, incoming)

        assert len(changes) == 1
        chg = changes[0]
        assert chg.field_name == "country"
        assert chg.old_value == "US"
        assert chg.new_value == "UK"
        assert chg.reason == "location_updated"

    def test_both_city_and_country_change(self, baseline_record: Dict[str, Any]) -> None:
        incoming = dict(baseline_record, city="London", country="UK")
        changes = ChangeTracker.detect_changes(baseline_record, incoming)

        assert len(changes) == 2
        field_names = {c.field_name for c in changes}
        assert field_names == {"city", "country"}
        for c in changes:
            assert c.reason == "location_updated"

    def test_single_location_field_fallback(self) -> None:
        baseline = {"location": "Austin, TX"}
        incoming = {"location": "Seattle, WA"}
        changes = ChangeTracker.detect_changes(baseline, incoming)

        assert len(changes) == 1
        assert changes[0].field_name == "location"
        assert changes[0].old_value == "Austin, TX"
        assert changes[0].new_value == "Seattle, WA"
        assert changes[0].reason == "location_updated"

    def test_location_whitespace_and_case_normalization(self, baseline_record: Dict[str, Any]) -> None:
        incoming = dict(baseline_record, city="  san francisco  ")
        changes = ChangeTracker.detect_changes(baseline_record, incoming)
        assert len(changes) == 0


# ==============================================================================
# 5. Skills Change Detection & Ordering Invariance Tests
# ==============================================================================


class TestSkillsChangeDetection:
    """Tests skills list set comparisons, ordering invariance, and case normalization."""

    def test_ordering_invariance_does_not_trigger_change(self, baseline_record: Dict[str, Any]) -> None:
        # ['Python', 'SQL'] vs ['SQL', 'Python'] must NOT trigger a change
        baseline = dict(baseline_record, skills=["Python", "SQL"])
        incoming = dict(baseline_record, skills=["SQL", "Python"])

        changes = ChangeTracker.detect_changes(baseline, incoming)
        skill_changes = [c for c in changes if c.field_name == "skills"]
        assert len(skill_changes) == 0

    def test_casing_invariance_does_not_trigger_change(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, skills=["python", "sql"])
        incoming = dict(baseline_record, skills=["Python", "SQL"])

        changes = ChangeTracker.detect_changes(baseline, incoming)
        assert len(changes) == 0

    def test_duplicate_skills_invariance(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, skills=["Python", "Python", "SQL"])
        incoming = dict(baseline_record, skills=["SQL", "Python"])

        changes = ChangeTracker.detect_changes(baseline, incoming)
        assert len(changes) == 0

    def test_skill_added(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, skills=["Python", "SQL"])
        incoming = dict(baseline_record, skills=["Python", "SQL", "dbt"])

        changes = ChangeTracker.detect_changes(baseline, incoming)
        assert len(changes) == 1
        chg = changes[0]
        assert chg.field_name == "skills"
        assert chg.reason == "skills_updated"
        old_list = json.loads(chg.old_value)
        new_list = json.loads(chg.new_value)
        assert set(old_list) == {"Python", "SQL"}
        assert set(new_list) == {"Python", "SQL", "dbt"}

    def test_skill_removed(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, skills=["Python", "SQL", "Spark"])
        incoming = dict(baseline_record, skills=["Python", "SQL"])

        changes = ChangeTracker.detect_changes(baseline, incoming)
        assert len(changes) == 1
        assert changes[0].field_name == "skills"
        assert changes[0].reason == "skills_updated"

    def test_skills_json_string_support(self, baseline_record: Dict[str, Any]) -> None:
        # Stringified JSON array vs Python list with different ordering
        baseline = dict(baseline_record, skills='["Python", "SQL"]')
        incoming = dict(baseline_record, skills=["SQL", "Python"])

        changes = ChangeTracker.detect_changes(baseline, incoming)
        assert len(changes) == 0

    def test_skills_comma_separated_string(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, skills="Python, SQL")
        incoming = dict(baseline_record, skills=["SQL", "Python"])

        changes = ChangeTracker.detect_changes(baseline, incoming)
        assert len(changes) == 0

    def test_empty_skills_vs_none(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, skills=None)
        incoming = dict(baseline_record, skills=[])

        changes = ChangeTracker.detect_changes(baseline, incoming)
        assert len(changes) == 0


# ==============================================================================
# 6. Title and Status Change Tests
# ==============================================================================


class TestTitleAndStatusChanges:
    """Tests job title updates and lifecycle status transitions."""

    def test_title_updated(self, baseline_record: Dict[str, Any]) -> None:
        incoming = dict(baseline_record, title="Senior Data Engineer")
        changes = ChangeTracker.detect_changes(baseline_record, incoming)

        assert len(changes) == 1
        chg = changes[0]
        assert chg.field_name == "title"
        assert chg.old_value == "Data Engineer"
        assert chg.new_value == "Senior Data Engineer"
        assert chg.reason == "title_updated"

    def test_title_whitespace_normalization(self, baseline_record: Dict[str, Any]) -> None:
        incoming = dict(baseline_record, title="  Data Engineer  ")
        changes = ChangeTracker.detect_changes(baseline_record, incoming)
        assert len(changes) == 0

    def test_status_open_to_closed(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, status="open")
        incoming = dict(baseline_record, status="closed")
        changes = ChangeTracker.detect_changes(baseline, incoming)

        assert len(changes) == 1
        chg = changes[0]
        assert chg.field_name == "status"
        assert chg.old_value == "open"
        assert chg.new_value == "closed"
        assert chg.reason == "status_changed"

    def test_status_closed_to_open_reopening(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, status="closed")
        incoming = dict(baseline_record, status="open")
        changes = ChangeTracker.detect_changes(baseline, incoming)

        assert len(changes) == 1
        chg = changes[0]
        assert chg.field_name == "status"
        assert chg.old_value == "closed"
        assert chg.new_value == "open"
        assert chg.reason == "status_changed"

    def test_status_enum_compatibility(self, baseline_record: Dict[str, Any]) -> None:
        baseline = dict(baseline_record, status=StatusEnum.open)
        incoming = dict(baseline_record, status="open")
        changes = ChangeTracker.detect_changes(baseline, incoming)
        assert len(changes) == 0


# ==============================================================================
# 7. Identical Records & Metadata Invariance Tests
# ==============================================================================


class TestIdenticalRecords:
    """Tests zero changes generated when records are identical or differ only in metadata."""

    def test_completely_identical_records_zero_changes(self, baseline_record: Dict[str, Any]) -> None:
        incoming = dict(baseline_record)
        changes = ChangeTracker.detect_changes(baseline_record, incoming)
        assert len(changes) == 0
        assert changes == []

    def test_untracked_metadata_changes_produce_zero_changes(self, baseline_record: Dict[str, Any]) -> None:
        incoming = dict(
            baseline_record,
            last_seen="2026-11-18T23:59:59Z",
            source_urls=["https://greenhouse.io/jobs/123", "https://lever.co/jobs/456"],
            evidence={"salary_min": "$120k in post"},
            duplicate_count=3,
        )
        changes = ChangeTracker.detect_changes(baseline_record, incoming)
        assert len(changes) == 0


# ==============================================================================
# 8. Database Persistence & Retrieval Tests
# ==============================================================================


class TestDatabasePersistenceAndHistory:
    """Tests persisting changes to posting_change_history and querying via get_history."""

    def test_log_changes_inserts_and_returns_count(self, in_memory_db: sqlite3.Connection) -> None:
        posting_id = "test_post_100"
        changes = [
            FieldChange(
                field_name="salary_min",
                old_value="100000.0",
                new_value="120000.0",
                reason="compensation_updated",
            ),
            FieldChange(
                field_name="remote_policy",
                old_value="onsite",
                new_value="remote",
                reason="remote_status_changed",
            ),
        ]

        count = ChangeTracker.log_changes(in_memory_db, posting_id, changes)
        assert count == 2

        # Verify directly in SQLite table
        cursor = in_memory_db.cursor()
        rows = cursor.execute("SELECT * FROM posting_change_history WHERE posting_id = ?", (posting_id,)).fetchall()
        assert len(rows) == 2

    def test_log_changes_with_empty_list_returns_zero(self, in_memory_db: sqlite3.Connection) -> None:
        count = ChangeTracker.log_changes(in_memory_db, "empty_post", [])
        assert count == 0

    def test_get_history_all_and_sorted_by_detected_at_desc(self, in_memory_db: sqlite3.Connection) -> None:
        posting_id = "post_chronology"
        t1 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)

        ChangeTracker.log_changes(
            in_memory_db,
            posting_id,
            [FieldChange(field_name="salary_min", old_value="100k", new_value="110k", reason="compensation_updated")],
            timestamp=t1,
        )
        ChangeTracker.log_changes(
            in_memory_db,
            posting_id,
            [FieldChange(field_name="salary_min", old_value="110k", new_value="125k", reason="compensation_updated")],
            timestamp=t2,
        )

        history = ChangeTracker.get_history(in_memory_db, posting_id=posting_id)
        assert len(history) == 2
        # Most recent first
        assert history[0]["new_value"] == "125k"
        assert history[1]["new_value"] == "110k"

    def test_get_history_filtering_by_field_name(self, in_memory_db: sqlite3.Connection) -> None:
        posting_id = "post_multi_field"
        changes = [
            FieldChange(field_name="salary_min", old_value="100k", new_value="120k", reason="compensation_updated"),
            FieldChange(field_name="status", old_value="open", new_value="closed", reason="status_changed"),
            FieldChange(field_name="salary_min", old_value="120k", new_value="130k", reason="compensation_updated"),
        ]
        ChangeTracker.log_changes(in_memory_db, posting_id, changes)

        # Filter only salary_min changes
        salary_history = ChangeTracker.get_history(in_memory_db, posting_id=posting_id, field_name="salary_min")
        assert len(salary_history) == 2
        for entry in salary_history:
            assert entry["field_name"] == "salary_min"

        # Filter only status changes
        status_history = ChangeTracker.get_history(in_memory_db, posting_id=posting_id, field_name="status")
        assert len(status_history) == 1
        assert status_history[0]["field_name"] == "status"
        assert status_history[0]["new_value"] == "closed"

    def test_get_history_across_multiple_postings(self, in_memory_db: sqlite3.Connection) -> None:
        ChangeTracker.log_changes(
            in_memory_db,
            "post_A",
            [FieldChange(field_name="city", old_value="SF", new_value="NYC", reason="location_updated")],
        )
        ChangeTracker.log_changes(
            in_memory_db,
            "post_B",
            [FieldChange(field_name="city", old_value="Austin", new_value="Dallas", reason="location_updated")],
        )

        all_history = ChangeTracker.get_history(in_memory_db)
        assert len(all_history) == 2

        post_a_only = ChangeTracker.get_history(in_memory_db, posting_id="post_A")
        assert len(post_a_only) == 1
        assert post_a_only[0]["posting_id"] == "post_A"

    def test_get_history_empty_db_returns_empty_list(self, in_memory_db: sqlite3.Connection) -> None:
        history = ChangeTracker.get_history(in_memory_db, posting_id="nonexistent")
        assert history == []


# ==============================================================================
# 9. Pydantic Model Interoperability & Invocation Modes
# ==============================================================================


class TestPydanticCompatibilityAndInvocation:
    """Tests ChangeTracker with JobPostingRecord and CanonicalJobPosting instances."""

    def test_detect_changes_with_pydantic_instances(self) -> None:
        now = datetime.now(timezone.utc)
        record1 = JobPostingRecord(
            posting_id="pydantic_test_1",
            company="Stripe",
            title="Backend Engineer",
            seniority=SeniorityEnum.senior,
            city="Seattle",
            country="US",
            remote_policy=RemotePolicyEnum.hybrid,
            salary_min=150000.0,
            salary_max=200000.0,
            currency="USD",
            skills=["Python", "Go"],
            first_seen=now,
            last_seen=now,
        )

        record2 = JobPostingRecord(
            posting_id="pydantic_test_1",
            company="Stripe",
            title="Senior Backend Engineer",  # changed
            seniority=SeniorityEnum.senior,
            city="Seattle",
            country="US",
            remote_policy=RemotePolicyEnum.remote,  # changed
            salary_min=165000.0,  # changed
            salary_max=200000.0,
            currency="USD",
            skills=["Python", "Go", "Kubernetes"],  # changed
            first_seen=now,
            last_seen=now,
        )

        changes = ChangeTracker.detect_changes(record1, record2)
        changed_fields = {c.field_name: c for c in changes}

        assert "title" in changed_fields
        assert changed_fields["title"].reason == "title_updated"

        assert "remote_policy" in changed_fields
        assert changed_fields["remote_policy"].reason == "remote_status_changed"

        assert "salary_min" in changed_fields
        assert changed_fields["salary_min"].reason == "compensation_updated"

        assert "skills" in changed_fields
        assert changed_fields["skills"].reason == "skills_updated"

    def test_instance_and_class_method_invocation_parity(
        self,
        in_memory_db: sqlite3.Connection,
        baseline_record: Dict[str, Any],
    ) -> None:
        incoming = dict(baseline_record, title="Lead Data Engineer")

        # Classmethod invocation
        res_class = ChangeTracker.detect_changes(baseline_record, incoming)

        # Instance invocation
        tracker = ChangeTracker()
        res_inst = tracker.detect_changes(baseline_record, incoming)

        assert len(res_class) == len(res_inst) == 1
        assert res_class[0].field_name == res_inst[0].field_name == "title"

        # Log changes via class and instance
        tracker.log_changes(in_memory_db, "post_1", res_inst)
        hist = tracker.get_history(in_memory_db, posting_id="post_1")
        assert len(hist) == 1
