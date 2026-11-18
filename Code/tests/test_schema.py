"""Comprehensive pytest suite for schema models and anti-hallucination validation engine.

Covers:
- Strict Enums: SeniorityEnum, RemotePolicyEnum, EmploymentTypeEnum, StatusEnum
- JobPostingRecord: Valid instances, null evaluation, strict enum enforcement,
  salary bounds validation, list/dict coercion, and dict/JSON serialization
- RawExtractionOutput: LLM output parsing, string salary cleaning, visa coercion,
  and conversion to canonical JobPostingRecord with salary annualization
- Anti-Hallucination Validators:
  - normalize_annual_salary: hourly, monthly, weekly, daily, and annual scaling
  - validate_salary: ordering, positivity, realistic annualized bounds
  - validate_evidence_presence: exact matching, alias matching, null evaluation
  - validate_evidence_in_text: verbatim and normalized presence verification
- Deterministic ID Generation: stable idempotent hashing
"""

import json
from datetime import date, datetime, timezone
import pytest
from pydantic import ValidationError

from pipeline.models.schema import (
    EmploymentTypeEnum,
    JobPostingRecord,
    RawExtractionOutput,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
    generate_deterministic_id,
)
from pipeline.models.validators import (
    CURRENCY_ANNUAL_BOUNDS,
    DEFAULT_ANNUAL_BOUNDS,
    normalize_annual_salary,
    validate_evidence_in_text,
    validate_evidence_presence,
    validate_record_evidence,
    validate_salary,
)


# ==============================================================================
# 1. Enums Test Suite
# ==============================================================================


class TestEnums:
    """Tests strict Enum definitions, values, and string behavior."""

    def test_seniority_enum_values(self):
        expected = {"junior", "mid", "senior", "staff_plus"}
        actual = {e.value for e in SeniorityEnum}
        assert actual == expected

    def test_remote_policy_enum_values(self):
        expected = {"onsite", "hybrid", "remote"}
        actual = {e.value for e in RemotePolicyEnum}
        assert actual == expected

    def test_employment_type_enum_values(self):
        expected = {"full_time", "contract", "intern"}
        actual = {e.value for e in EmploymentTypeEnum}
        assert actual == expected

    def test_status_enum_values(self):
        expected = {"open", "closed"}
        actual = {e.value for e in StatusEnum}
        assert actual == expected

    def test_enum_is_str_subclass(self):
        assert isinstance(SeniorityEnum.senior, str)
        assert SeniorityEnum.senior == "senior"
        assert RemotePolicyEnum.remote == "remote"
        assert EmploymentTypeEnum.full_time == "full_time"
        assert StatusEnum.open == "open"

    def test_invalid_enum_instantiation_raises_value_error(self):
        with pytest.raises(ValueError):
            SeniorityEnum("lead")
        with pytest.raises(ValueError):
            RemotePolicyEnum("flexible")
        with pytest.raises(ValueError):
            EmploymentTypeEnum("part_time")
        with pytest.raises(ValueError):
            StatusEnum("archived")


# ==============================================================================
# 2. JobPostingRecord Test Suite
# ==============================================================================


class TestJobPostingRecord:
    """Tests canonical JobPostingRecord schema, validation, defaults, and serialization."""

    @pytest.fixture
    def valid_timestamps(self):
        now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
        return now, now

    def test_valid_full_record(self, valid_timestamps):
        first_seen, last_seen = valid_timestamps
        record = JobPostingRecord(
            posting_id="stripe-de-001",
            company="Stripe",
            title="Senior Data Engineer",
            seniority=SeniorityEnum.senior,
            city="San Francisco",
            country="US",
            remote_policy=RemotePolicyEnum.hybrid,
            salary_min=170000.0,
            salary_max=220000.0,
            currency="USD",
            employment_type=EmploymentTypeEnum.full_time,
            skills=["Python", "Spark", "SQL", "Trino"],
            visa_sponsorship=True,
            posted_date=date(2026, 11, 15),
            status=StatusEnum.open,
            source_urls=["https://boards.greenhouse.io/stripe/jobs/1001"],
            evidence={
                "salary": "Compensation: $170,000 - $220,000 per year",
                "remote_policy": "Hybrid: 2 days in office",
                "visa_sponsorship": "Visa sponsorship is available for this role.",
            },
            first_seen=first_seen,
            last_seen=last_seen,
        )

        assert record.posting_id == "stripe-de-001"
        assert record.company == "Stripe"
        assert record.title == "Senior Data Engineer"
        assert record.seniority == SeniorityEnum.senior
        assert record.salary_min == 170000.0
        assert record.salary_max == 220000.0
        assert record.currency == "USD"
        assert record.skills == ["Python", "Spark", "SQL", "Trino"]
        assert record.visa_sponsorship is True
        assert record.status == StatusEnum.open
        assert len(record.source_urls) == 1
        assert len(record.evidence) == 3

    def test_valid_minimal_record_evaluates_unstated_fields_to_null_and_defaults(self, valid_timestamps):
        first_seen, last_seen = valid_timestamps
        record = JobPostingRecord(
            posting_id="minimal-001",
            company="Acme Corp",
            title="Data Engineer",
            first_seen=first_seen,
            last_seen=last_seen,
        )

        # Unstated fields evaluate to None
        assert record.seniority is None
        assert record.city is None
        assert record.country is None
        assert record.remote_policy is None
        assert record.salary_min is None
        assert record.salary_max is None
        assert record.currency is None
        assert record.employment_type is None
        assert record.visa_sponsorship is None
        assert record.posted_date is None

        # Collections default to empty, status defaults to open
        assert record.skills == []
        assert record.source_urls == []
        assert record.evidence == {}
        assert record.status == StatusEnum.open

    def test_missing_required_fields_raises_validation_error(self, valid_timestamps):
        first_seen, last_seen = valid_timestamps

        # Missing posting_id
        with pytest.raises(ValidationError) as exc:
            JobPostingRecord(
                company="Stripe",
                title="Data Engineer",
                first_seen=first_seen,
                last_seen=last_seen,
            )
        assert "posting_id" in str(exc.value)

        # Missing company
        with pytest.raises(ValidationError) as exc:
            JobPostingRecord(
                posting_id="123",
                title="Data Engineer",
                first_seen=first_seen,
                last_seen=last_seen,
            )
        assert "company" in str(exc.value)

        # Missing title
        with pytest.raises(ValidationError) as exc:
            JobPostingRecord(
                posting_id="123",
                company="Stripe",
                first_seen=first_seen,
                last_seen=last_seen,
            )
        assert "title" in str(exc.value)

        # Missing timestamps
        with pytest.raises(ValidationError) as exc:
            JobPostingRecord(
                posting_id="123",
                company="Stripe",
                title="Data Engineer",
            )
        assert "first_seen" in str(exc.value) or "last_seen" in str(exc.value)

    def test_invalid_enums_raise_validation_error(self, valid_timestamps):
        first_seen, last_seen = valid_timestamps
        base_kwargs = {
            "posting_id": "test-enum",
            "company": "Test Co",
            "title": "Engineer",
            "first_seen": first_seen,
            "last_seen": last_seen,
        }

        # Invalid Seniority
        with pytest.raises(ValidationError):
            JobPostingRecord(**base_kwargs, seniority="lead")
        with pytest.raises(ValidationError):
            JobPostingRecord(**base_kwargs, seniority="principal")

        # Invalid Remote Policy
        with pytest.raises(ValidationError):
            JobPostingRecord(**base_kwargs, remote_policy="flexible")
        with pytest.raises(ValidationError):
            JobPostingRecord(**base_kwargs, remote_policy="anywhere")

        # Invalid Employment Type
        with pytest.raises(ValidationError):
            JobPostingRecord(**base_kwargs, employment_type="part_time")
        with pytest.raises(ValidationError):
            JobPostingRecord(**base_kwargs, employment_type="freelance")

        # Invalid Status
        with pytest.raises(ValidationError):
            JobPostingRecord(**base_kwargs, status="pending")

    def test_enum_string_coercion_works_for_valid_values(self, valid_timestamps):
        first_seen, last_seen = valid_timestamps
        record = JobPostingRecord(
            posting_id="test-coerce",
            company="Test Co",
            title="Engineer",
            seniority="senior",
            remote_policy="remote",
            employment_type="full_time",
            status="closed",
            first_seen=first_seen,
            last_seen=last_seen,
        )
        assert record.seniority == SeniorityEnum.senior
        assert record.remote_policy == RemotePolicyEnum.remote
        assert record.employment_type == EmploymentTypeEnum.full_time
        assert record.status == StatusEnum.closed

    def test_salary_bounds_and_order_validation_in_record(self, valid_timestamps):
        first_seen, last_seen = valid_timestamps
        base_kwargs = {
            "posting_id": "test-sal",
            "company": "Test Co",
            "title": "Engineer",
            "first_seen": first_seen,
            "last_seen": last_seen,
        }

        # min > max raises ValidationError
        with pytest.raises(ValidationError) as exc:
            JobPostingRecord(**base_kwargs, salary_min=200000.0, salary_max=150000.0, currency="USD")
        assert "cannot be greater than salary_max" in str(exc.value)

        # non-positive salary raises ValidationError
        with pytest.raises(ValidationError) as exc:
            JobPostingRecord(**base_kwargs, salary_min=-5000.0, salary_max=150000.0, currency="USD")
        assert "must be positive" in str(exc.value)

        # unrealistically low annualized salary raises ValidationError (e.g. raw hourly wage passed by mistake)
        with pytest.raises(ValidationError) as exc:
            JobPostingRecord(**base_kwargs, salary_min=80.0, salary_max=120.0, currency="USD")
        assert "outside realistic annualized bounds" in str(exc.value)

        # unrealistically high salary raises ValidationError
        with pytest.raises(ValidationError) as exc:
            JobPostingRecord(**base_kwargs, salary_min=100000.0, salary_max=50000000.0, currency="USD")
        assert "outside realistic annualized bounds" in str(exc.value)

        # Valid annualized salary passes
        rec = JobPostingRecord(**base_kwargs, salary_min=120000.0, salary_max=180000.0, currency="USD")
        assert rec.salary_min == 120000.0
        assert rec.salary_max == 180000.0

    def test_field_coercion_for_collections(self, valid_timestamps):
        first_seen, last_seen = valid_timestamps
        # Passing None to fields that default to lists or dicts coerces cleanly
        record = JobPostingRecord(
            posting_id="test-null-collections",
            company="Test Co",
            title="Engineer",
            skills=None,
            source_urls=None,
            evidence=None,
            first_seen=first_seen,
            last_seen=last_seen,
        )
        assert record.skills == []
        assert record.source_urls == []
        assert record.evidence == {}

        # Passing single string to skills coerces to single-element list
        record_single_skill = JobPostingRecord(
            posting_id="test-single-skill",
            company="Test Co",
            title="Engineer",
            skills="Python",
            first_seen=first_seen,
            last_seen=last_seen,
        )
        assert record_single_skill.skills == ["Python"]

    def test_extra_fields_forbidden(self, valid_timestamps):
        first_seen, last_seen = valid_timestamps
        with pytest.raises(ValidationError) as exc:
            JobPostingRecord(
                posting_id="test-extra",
                company="Test Co",
                title="Engineer",
                unknown_random_field="unexpected",
                first_seen=first_seen,
                last_seen=last_seen,
            )
        assert "extra" in str(exc.value).lower()

    def test_serialization_dict_and_json(self, valid_timestamps):
        first_seen, last_seen = valid_timestamps
        record = JobPostingRecord(
            posting_id="stripe-123",
            company="Stripe",
            title="Data Platform Engineer",
            seniority=SeniorityEnum.staff_plus,
            salary_min=210000.0,
            salary_max=275000.0,
            currency="USD",
            skills=["Kafka", "Flink", "Go"],
            first_seen=first_seen,
            last_seen=last_seen,
        )

        # to_dict() test
        d = record.to_dict()
        assert isinstance(d, dict)
        assert d["company"] == "Stripe"
        assert d["seniority"] == SeniorityEnum.staff_plus
        assert d["skills"] == ["Kafka", "Flink", "Go"]

        # to_json() test
        j_str = record.to_json()
        assert isinstance(j_str, str)
        parsed = json.loads(j_str)
        assert parsed["company"] == "Stripe"
        assert parsed["seniority"] == "staff_plus"
        assert parsed["salary_min"] == 210000.0

        # Roundtrip reconstruction from JSON
        restored = JobPostingRecord.model_validate(parsed)
        assert restored.posting_id == record.posting_id
        assert restored.seniority == record.seniority
        assert restored.skills == record.skills


# ==============================================================================
# 3. RawExtractionOutput Test Suite
# ==============================================================================


class TestRawExtractionOutput:
    """Tests the RawExtractionOutput model returned directly by the LLM."""

    def test_default_all_fields_optional_null_over_guessing(self):
        raw = RawExtractionOutput()
        assert raw.company is None
        assert raw.title is None
        assert raw.seniority is None
        assert raw.city is None
        assert raw.country is None
        assert raw.remote_policy is None
        assert raw.salary_min is None
        assert raw.salary_max is None
        assert raw.salary_period is None
        assert raw.currency is None
        assert raw.employment_type is None
        assert raw.skills == []
        assert raw.visa_sponsorship is None
        assert raw.posted_date is None
        assert raw.evidence == {}

    def test_populating_raw_extraction(self):
        raw = RawExtractionOutput(
            company="Anthropic",
            title="Research Engineer, Alignment",
            seniority=SeniorityEnum.mid,
            city="San Francisco",
            remote_policy=RemotePolicyEnum.onsite,
            salary_min=190000.0,
            salary_max=240000.0,
            salary_period="year",
            currency="USD",
            skills=["Python", "PyTorch"],
            visa_sponsorship=True,
            evidence={
                "salary": "$190k - $240k per year",
                "remote_policy": "In-person at our SF office",
            },
        )
        assert raw.company == "Anthropic"
        assert raw.seniority == SeniorityEnum.mid
        assert raw.remote_policy == RemotePolicyEnum.onsite
        assert raw.salary_min == 190000.0
        assert raw.skills == ["Python", "PyTorch"]
        assert raw.visa_sponsorship is True

    def test_raw_extraction_cleans_string_salaries(self):
        raw = RawExtractionOutput(
            salary_min="$150,000",
            salary_max="$200,000.50",
            salary_period="year",
            currency="USD",
        )
        assert raw.salary_min == 150000.0
        assert raw.salary_max == 200000.50

    def test_raw_extraction_parses_visa_strings(self):
        # Truthy visa indicators
        for v in ["yes", "true", "True", "available", "sponsored", "will sponsor"]:
            raw = RawExtractionOutput(visa_sponsorship=v)
            assert raw.visa_sponsorship is True

        # Falsy visa indicators
        for v in ["no", "false", "False", "unable to sponsor", "not sponsored", "unavailable"]:
            raw = RawExtractionOutput(visa_sponsorship=v)
            assert raw.visa_sponsorship is False

        # Unclear / null visa indicators
        for v in ["none", "null", "unknown", "unclear", "n/a", "not mentioned"]:
            raw = RawExtractionOutput(visa_sponsorship=v)
            assert raw.visa_sponsorship is None

    def test_raw_extraction_ignores_extra_hallucinated_fields(self):
        # LLMs often output extra JSON keys; RawExtractionOutput must ignore them without error
        payload = {
            "company": "OpenAI",
            "title": "Data Engineer",
            "hallucinated_perk": "Free lunch",
            "llm_confidence_score": 0.99,
        }
        raw = RawExtractionOutput.model_validate(payload)
        assert raw.company == "OpenAI"
        assert raw.title == "Data Engineer"
        assert not hasattr(raw, "hallucinated_perk")

    def test_to_job_posting_record_with_hourly_annualization(self):
        now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
        raw = RawExtractionOutput(
            company="Databricks",
            title="Staff Systems Engineer",
            seniority=SeniorityEnum.staff_plus,
            salary_min=90.0,
            salary_max=120.0,
            salary_period="hour",
            currency="USD",
            skills=["C++", "Distributed Systems"],
            evidence={"salary": "$90 - $120 / hr"},
        )

        record = raw.to_job_posting_record(
            posting_id="db-001",
            first_seen=now,
            last_seen=now,
            source_urls=["https://databricks.com/jobs/1"],
            annualize_salary=True,
        )

        assert record.posting_id == "db-001"
        assert record.company == "Databricks"
        assert record.seniority == SeniorityEnum.staff_plus
        # 90 * 2080 = 187,200.0; 120 * 2080 = 249,600.0
        assert record.salary_min == 187200.0
        assert record.salary_max == 249600.0
        assert record.evidence["salary"] == "$90 - $120 / hr"
        assert record.source_urls == ["https://databricks.com/jobs/1"]

    def test_to_job_posting_record_with_monthly_annualization(self):
        now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
        raw = RawExtractionOutput(
            company="Snowflake",
            title="Data Analyst",
            seniority=SeniorityEnum.mid,
            salary_min=10000.0,
            salary_max=12500.0,
            salary_period="month",
            currency="USD",
        )

        record = raw.to_job_posting_record(
            posting_id="sf-001",
            first_seen=now,
            last_seen=now,
            annualize_salary=True,
        )

        # 10,000 * 12 = 120,000.0; 12,500 * 12 = 150,000.0
        assert record.salary_min == 120000.0
        assert record.salary_max == 150000.0


# ==============================================================================
# 4. Salary Validators Test Suite
# ==============================================================================


class TestSalaryValidators:
    """Tests normalize_annual_salary and validate_salary anti-hallucination functions."""

    def test_normalize_annual_salary_hourly(self):
        assert normalize_annual_salary(80.0, "hour") == 166400.0
        assert normalize_annual_salary(80.0, "hourly") == 166400.0
        assert normalize_annual_salary(100.0, "hr") == 208000.0
        assert normalize_annual_salary(50.0, "h") == 104000.0
        assert normalize_annual_salary(75.5, "/hr") == 157040.0
        assert normalize_annual_salary(60.0, "per hour") == 124800.0

    def test_normalize_annual_salary_monthly(self):
        assert normalize_annual_salary(10000.0, "month") == 120000.0
        assert normalize_annual_salary(12500.0, "monthly") == 150000.0
        assert normalize_annual_salary(8000.0, "mo") == 96000.0
        assert normalize_annual_salary(15000.0, "/month") == 180000.0
        assert normalize_annual_salary(9500.0, "per month") == 114000.0

    def test_normalize_annual_salary_yearly(self):
        assert normalize_annual_salary(150000.0, "year") == 150000.0
        assert normalize_annual_salary(180000.0, "yearly") == 180000.0
        assert normalize_annual_salary(210000.0, "annual") == 210000.0
        assert normalize_annual_salary(220000.0, "annually") == 220000.0
        assert normalize_annual_salary(195000.0, "p.a.") == 195000.0
        assert normalize_annual_salary(250000.0, "annum") == 250000.0

    def test_normalize_annual_salary_weekly_and_daily(self):
        # Weekly: 52 weeks
        assert normalize_annual_salary(3000.0, "week") == 156000.0
        assert normalize_annual_salary(2500.0, "weekly") == 130000.0
        # Daily: 260 working days
        assert normalize_annual_salary(600.0, "day") == 156000.0
        assert normalize_annual_salary(500.0, "daily") == 130000.0

    def test_normalize_annual_salary_invalid_inputs(self):
        with pytest.raises(ValueError):
            normalize_annual_salary(0, "hour")
        with pytest.raises(ValueError):
            normalize_annual_salary(-50, "hour")
        with pytest.raises(ValueError):
            normalize_annual_salary(1000, "")
        with pytest.raises(ValueError):
            normalize_annual_salary(1000, "decade")
        with pytest.raises(ValueError):
            normalize_annual_salary(1000, None)

    def test_validate_salary_both_none_is_valid(self):
        assert validate_salary(None, None) is True
        assert validate_salary(None, None, "USD") is True

    def test_validate_salary_valid_ranges(self):
        assert validate_salary(120000.0, 180000.0, "USD") is True
        assert validate_salary(150000.0, 150000.0, "USD") is True
        assert validate_salary(130000.0, None, "USD") is True
        assert validate_salary(None, 220000.0, "USD") is True

    def test_validate_salary_ordering_violation(self):
        # min > max raises ValueError when raise_on_error=True
        with pytest.raises(ValueError) as exc:
            validate_salary(250000.0, 200000.0, "USD")
        assert "cannot be greater than salary_max" in str(exc.value)

        # returns False when raise_on_error=False
        assert validate_salary(250000.0, 200000.0, "USD", raise_on_error=False) is False

    def test_validate_salary_non_positive_violation(self):
        with pytest.raises(ValueError) as exc:
            validate_salary(-50000.0, 100000.0, "USD")
        assert "must be positive" in str(exc.value)

        with pytest.raises(ValueError) as exc:
            validate_salary(0.0, 100000.0, "USD")
        assert "must be positive" in str(exc.value)

        assert validate_salary(-100.0, 100000.0, "USD", raise_on_error=False) is False

    def test_validate_salary_bounds_anti_hallucination(self):
        # Detects unannualized hourly rate passed into annual salary field
        with pytest.raises(ValueError) as exc:
            validate_salary(85.0, 120.0, "USD")
        assert "outside realistic annualized bounds" in str(exc.value)

        # Detects crazy hallucinated numbers ($50 million/year)
        with pytest.raises(ValueError) as exc:
            validate_salary(100000.0, 50000000.0, "USD")
        assert "outside realistic annualized bounds" in str(exc.value)

        assert validate_salary(85.0, 120.0, "USD", raise_on_error=False) is False

    def test_validate_salary_currency_specific_bounds(self):
        # EUR
        assert validate_salary(60000.0, 110000.0, "EUR") is True
        # GBP
        assert validate_salary(50000.0, 95000.0, "GBP") is True
        # JPY (Realistic 4M - 15M JPY)
        assert validate_salary(5000000.0, 12000000.0, "JPY") is True
        # JPY too low (500 JPY is clearly hourly or wrong)
        assert validate_salary(500.0, 1000.0, "JPY", raise_on_error=False) is False
        # INR (Realistic 800k - 4M INR)
        assert validate_salary(1200000.0, 3500000.0, "INR") is True


# ==============================================================================
# 5. Evidence Validators Test Suite
# ==============================================================================


class TestEvidenceValidators:
    """Tests validate_evidence_presence and validate_evidence_in_text functions."""

    def test_validate_evidence_presence_null_fields_require_no_evidence(self):
        evidence = {}
        assert validate_evidence_presence("salary_min", None, evidence) is True
        assert validate_evidence_presence("seniority", None, evidence) is True
        assert validate_evidence_presence("remote_policy", None, evidence) is True
        assert validate_evidence_presence("skills", [], evidence) is True
        assert validate_evidence_presence("city", "", evidence) is True

    def test_validate_evidence_presence_exact_field_match(self):
        evidence = {
            "seniority": "We are seeking a Senior Data Engineer with 5+ years experience.",
            "remote_policy": "This is a 100% remote opportunity across the United States.",
        }
        assert validate_evidence_presence("seniority", "senior", evidence) is True
        assert validate_evidence_presence("remote_policy", "remote", evidence) is True

    def test_validate_evidence_presence_alias_mapping(self):
        # LLMs often group salary under 'salary' or 'compensation'
        evidence = {
            "salary": "The base salary range for this position is $160,000 - $210,000 USD.",
            "location": "Located in Seattle, WA. Remote options available.",
            "visa": "We do not offer H1B transfer or sponsorship at this time.",
        }
        assert validate_evidence_presence("salary_min", 160000.0, evidence) is True
        assert validate_evidence_presence("salary_max", 210000.0, evidence) is True
        assert validate_evidence_presence("currency", "USD", evidence) is True
        assert validate_evidence_presence("remote_policy", "remote", evidence) is True
        assert validate_evidence_presence("city", "Seattle", evidence) is True
        assert validate_evidence_presence("visa_sponsorship", False, evidence) is True

    def test_validate_evidence_presence_missing_or_empty_snippet(self):
        evidence = {
            "title": "Data Engineer",
            "empty_field": "   ",
        }
        # Non-null field completely missing from evidence
        assert validate_evidence_presence("salary_min", 150000.0, evidence) is False
        assert validate_evidence_presence("seniority", "senior", evidence) is False
        # Field with empty whitespace snippet
        assert validate_evidence_presence("empty_field", "value", evidence) is False
        # None evidence dict
        assert validate_evidence_presence("title", "Data Engineer", None) is False

    def test_validate_evidence_in_text_verbatim(self):
        source = (
            "About the Role: Stripe is looking for a Staff Data Engineer. "
            "Compensation: The expected base salary range is $220,000 - $280,000 USD."
        )
        snip = "The expected base salary range is $220,000 - $280,000 USD."
        assert validate_evidence_in_text(snip, source) is True

    def test_validate_evidence_in_text_whitespace_normalization(self):
        source = "Location:\n    Remote   (US or Canada only)\nBenefits included."
        snip = "Remote (US or Canada only)"
        assert validate_evidence_in_text(snip, source) is True

    def test_validate_evidence_in_text_fabricated_fails(self):
        source = "We are seeking a Python developer in New York City."
        hallucinated_snip = "Salary: $300,000 per year with equity bonus"
        assert validate_evidence_in_text(hallucinated_snip, source) is False

    def test_validate_record_evidence_comprehensive(self):
        record = {
            "seniority": "senior",
            "remote_policy": "remote",
            "salary_min": 175000.0,
            "salary_max": 220000.0,
            "employment_type": "full_time",
            "visa_sponsorship": None,
            "skills": ["Python", "Spark"],
            "evidence": {
                "seniority": "Senior Data Engineer needed",
                "salary": "Comp: $175,000 - $220,000",
                "remote": "Remote role",
            },
        }
        results = validate_record_evidence(record)
        assert results["seniority"] is True
        assert results["remote_policy"] is True
        assert results["salary_min"] is True
        assert results["salary_max"] is True
        assert results["visa_sponsorship"] is True  # unstated -> True
        # employment_type and skills have values but no evidence provided
        assert results["employment_type"] is False
        assert results["skills"] is False


# ==============================================================================
# 6. Deterministic Posting ID Test Suite
# ==============================================================================


class TestDeterministicId:
    """Tests the stable, idempotent posting ID generator."""

    def test_deterministic_id_reproducibility(self):
        id_1 = generate_deterministic_id("Stripe", "Staff Data Engineer", "San Francisco, CA")
        id_2 = generate_deterministic_id("Stripe", "Staff Data Engineer", "San Francisco, CA")
        assert id_1 == id_2
        assert len(id_1) == 16

    def test_deterministic_id_normalizes_casing_and_whitespace(self):
        id_1 = generate_deterministic_id("  Stripe, Inc  ", "staff data engineer", "San Francisco")
        id_2 = generate_deterministic_id("stripe, inc", "Staff Data Engineer  ", "san francisco")
        assert id_1 == id_2

    def test_deterministic_id_different_inputs_produce_distinct_ids(self):
        id_1 = generate_deterministic_id("Stripe", "Senior Data Engineer")
        id_2 = generate_deterministic_id("Stripe", "Staff Data Engineer")
        id_3 = generate_deterministic_id("Databricks", "Senior Data Engineer")
        assert id_1 != id_2
        assert id_1 != id_3
