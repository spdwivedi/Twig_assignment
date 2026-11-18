"""Comprehensive unit and integration tests for JobPostingMatcher and MatchResult.

Tests cover:
- Step 1: Direct URL and requisition ID matching
- Step 2: Company normalization, aliases, and instant mismatch rejection
- Step 3: Seniority compatibility guards (Staff vs Senior/Junior)
- Step 4: Team specialization conflict checks (Platform vs Growth)
- Step 5: Location / Remote policy compatibility (NYC Onsite vs SF Onsite)
- Step 6: RapidFuzz title scoring (exact vs fuzzy high confidence)
- Step 7: Skills overlap Jaccard calculation and confidence bonus
- Multi-source dedup scenarios: Greenhouse vs Hacker News vs Lever
- Pydantic models (JobPostingRecord, RawExtractionOutput) & raw dictionaries
- Extreme edge cases: None values, empty fields, malformed URLs, Unicode characters
"""

from __future__ import annotations

from datetime import date, datetime
import pytest

from pipeline.dedup.matcher import JobPostingMatcher, MatchResult
from pipeline.models.schema import (
    EmploymentTypeEnum,
    JobPostingRecord,
    RawExtractionOutput,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
)


@pytest.fixture
def matcher() -> JobPostingMatcher:
    """Fixture providing a fresh default JobPostingMatcher."""
    return JobPostingMatcher()


# ==============================================================================
# 1. Direct URL and Requisition ID Matching (Step 1)
# ==============================================================================


class TestURLAndRequisitionMatching:
    """Tests for Step 1 direct URL and requisition ID matching."""

    def test_exact_url_match(self, matcher: JobPostingMatcher) -> None:
        """Postings sharing an identical source URL should instantly match."""
        url = "https://boards.greenhouse.io/stripe/jobs/4567890"
        p1 = {"company": "Stripe", "title": "Data Engineer", "source_url": url}
        p2 = {"company": "Stripe", "title": "Data Engineer", "source_url": url}

        res = matcher.compare(p1, p2)
        assert res.is_match is True
        assert res.score == 100.0
        assert res.match_type == "url_match"
        assert "Direct source URL match" in res.reason

    def test_url_match_with_marketing_tracking_params(self, matcher: JobPostingMatcher) -> None:
        """Tracking parameters (utm_*, gh_src) should be stripped before URL comparison."""
        base_url = "https://boards.greenhouse.io/stripe/jobs/4567890"
        p1 = {
            "company": "Stripe",
            "title": "Data Engineer",
            "source_url": f"{base_url}?gh_src=hackernews&utm_campaign=winter2024",
        }
        p2 = {
            "company": "Stripe",
            "title": "Data Engineer",
            "source_url": f"{base_url}?utm_source=linkedin",
        }

        res = matcher.compare(p1, p2)
        assert res.is_match is True
        assert res.score == 100.0
        assert res.match_type == "url_match"

    def test_url_match_with_job_posting_record_model(self, matcher: JobPostingMatcher) -> None:
        """Should support JobPostingRecord instances with source_urls lists."""
        url = "https://jobs.lever.co/databricks/12345678-1234-1234-1234-1234567890ab"
        p1 = JobPostingRecord(
            posting_id="p1",
            company="Databricks",
            title="Senior Data Platform Engineer",
            first_seen=datetime.now(),
            last_seen=datetime.now(),
            source_urls=[url],
        )
        p2 = {"company": "Databricks", "title": "Senior Data Platform Engineer", "source_url": url}

        res = matcher.compare(p1, p2)
        assert res.is_match is True
        assert res.score == 100.0
        assert res.match_type == "url_match"

    def test_hacker_news_url_item_id_preserved(self, matcher: JobPostingMatcher) -> None:
        """Hacker news item?id=... query parameters must NOT be stripped."""
        hn_url = "https://news.ycombinator.com/item?id=38912345"
        p1 = {"company": "Vercel", "title": "Data Engineer", "source_url": hn_url}
        p2 = {"company": "Vercel", "title": "Data Engineer", "source_url": hn_url}

        res = matcher.compare(p1, p2)
        assert res.is_match is True
        assert res.match_type == "url_match"

    def test_hacker_news_different_item_ids_do_not_url_match(self, matcher: JobPostingMatcher) -> None:
        """Different HN comments have different item IDs and should not match by URL."""
        p1 = {
            "company": "Company A",
            "title": "Data Engineer",
            "source_url": "https://news.ycombinator.com/item?id=111111",
        }
        p2 = {
            "company": "Company B",
            "title": "Data Engineer",
            "source_url": "https://news.ycombinator.com/item?id=222222",
        }

        res = matcher.compare(p1, p2)
        # Should not match by URL, and will fail on company mismatch
        assert res.match_type != "url_match"
        assert res.is_match is False

    def test_generic_root_urls_do_not_url_match(self, matcher: JobPostingMatcher) -> None:
        """Generic root ATS domains should not cause a false URL match."""
        p1 = {
            "company": "Company A",
            "title": "Data Engineer",
            "source_url": "https://boards.greenhouse.io/",
        }
        p2 = {
            "company": "Company B",
            "title": "Data Engineer",
            "source_url": "https://boards.greenhouse.io/",
        }

        res = matcher.compare(p1, p2)
        assert res.match_type != "url_match"
        assert res.is_match is False

    def test_requisition_id_match(self, matcher: JobPostingMatcher) -> None:
        """Explicit matching requisition IDs should instantly match."""
        p1 = {"company": "Airbnb", "title": "Data Engineer", "requisition_id": "REQ-9901"}
        p2 = {"company": "Airbnb", "title": "Data Engineer", "requisition_id": "REQ-9901"}

        res = matcher.compare(p1, p2)
        assert res.is_match is True
        assert res.score == 100.0
        assert res.match_type == "url_match"
        assert "Direct requisition ID match" in res.reason


# ==============================================================================
# 2. Company Match Validation (Step 2)
# ==============================================================================


class TestCompanyValidation:
    """Tests for Step 2 company normalization and mismatch rejection."""

    def test_different_companies_instant_rejection(self, matcher: JobPostingMatcher) -> None:
        """Postings from completely different companies must be instantly rejected."""
        p1 = {"company": "Stripe", "title": "Senior Data Engineer"}
        p2 = {"company": "Airbnb", "title": "Senior Data Engineer"}

        res = matcher.compare(p1, p2)
        assert res.is_match is False
        assert res.score == 0.0
        assert res.match_type == "no_match"
        assert "Company mismatch" in res.reason

    def test_company_legal_suffixes_normalization(self, matcher: JobPostingMatcher) -> None:
        """Corporate legal suffixes (Inc, LLC, Corp, Technologies) should be normalized."""
        pairs = [
            ("Stripe, Inc.", "Stripe"),
            ("Airbnb LLC", "Airbnb"),
            ("Databricks, Inc.", "Databricks"),
            ("Snowflake Corporation", "Snowflake"),
            ("Coinbase Technologies", "Coinbase"),
        ]
        for c1, c2 in pairs:
            p1 = {"company": c1, "title": "Data Engineer"}
            p2 = {"company": c2, "title": "Data Engineer"}
            res = matcher.compare(p1, p2)
            assert res.is_match is True, f"Failed for {c1} vs {c2}"
            assert res.match_type in ("exact", "fuzzy_high_confidence")

    def test_missing_company_name_rejection(self, matcher: JobPostingMatcher) -> None:
        """Missing company names should cleanly reject without error."""
        p1 = {"company": None, "title": "Data Engineer"}
        p2 = {"company": "Stripe", "title": "Data Engineer"}

        res = matcher.compare(p1, p2)
        assert res.is_match is False
        assert res.match_type == "no_match"
        assert "Missing company name" in res.reason


# ==============================================================================
# 3. Seniority Compatibility Guard (Step 3)
# ==============================================================================


class TestSeniorityGuard:
    """Tests for Step 3 seniority compatibility guards."""

    def test_staff_vs_senior_rejection(self, matcher: JobPostingMatcher) -> None:
        """Staff Data Engineer vs Senior Data Engineer at the same company must NOT match."""
        p1 = {"company": "Databricks", "title": "Staff Data Engineer", "seniority": "staff_plus"}
        p2 = {"company": "Databricks", "title": "Senior Data Engineer", "seniority": "senior"}

        res = matcher.compare(p1, p2)
        assert res.is_match is False
        assert res.score == 0.0
        assert res.match_type == "no_match"
        assert res.reason == "Incompatible seniority levels (e.g. Staff vs Senior)"
        assert res.details["seniority_1"] == "staff_plus"
        assert res.details["seniority_2"] == "senior"

    def test_staff_vs_junior_rejection(self, matcher: JobPostingMatcher) -> None:
        """Staff Data Engineer vs Junior Data Engineer must NOT match."""
        p1 = {"company": "Stripe", "title": "Principal Data Engineer"}
        p2 = {"company": "Stripe", "title": "Junior Data Engineer"}

        res = matcher.compare(p1, p2)
        assert res.is_match is False
        assert res.match_type == "no_match"
        assert "Incompatible seniority levels" in res.reason

    def test_senior_vs_junior_rejection(self, matcher: JobPostingMatcher) -> None:
        """Senior Data Engineer vs Junior Data Engineer must NOT match."""
        p1 = {"company": "Meta", "title": "Senior Data Engineer", "seniority": "senior"}
        p2 = {"company": "Meta", "title": "Associate Data Engineer", "seniority": "junior"}

        res = matcher.compare(p1, p2)
        assert res.is_match is False
        assert res.match_type == "no_match"
        assert res.reason == "Incompatible seniority levels (Senior vs Junior)"

    def test_title_inferred_seniority_staff_vs_senior(self, matcher: JobPostingMatcher) -> None:
        """Even without explicit seniority fields, title keywords must trigger the guard."""
        p1 = {"company": "Google", "title": "Staff Software Engineer, Data"}
        p2 = {"company": "Google", "title": "Senior Software Engineer, Data"}

        res = matcher.compare(p1, p2)
        assert res.is_match is False
        assert "Incompatible seniority levels" in res.reason

    def test_compatible_seniority_matches(self, matcher: JobPostingMatcher) -> None:
        """Identical or compatible seniorities must pass the guard."""
        p1 = {"company": "Netflix", "title": "Senior Data Engineer", "seniority": SeniorityEnum.senior}
        p2 = {"company": "Netflix", "title": "Sr. Data Engineer", "seniority": SeniorityEnum.senior}

        res = matcher.compare(p1, p2)
        assert res.is_match is True
        assert res.match_type in ("exact", "fuzzy_high_confidence")


# ==============================================================================
# 4. Team Specialization Conflict Check (Step 4)
# ==============================================================================


class TestTeamSpecializationConflict:
    """Tests for Step 4 team specialization conflict checks."""

    def test_platform_vs_growth_rejection(self, matcher: JobPostingMatcher) -> None:
        """Data Engineer - Platform vs Data Engineer - Growth must NOT match."""
        p1 = {"company": "Snowflake", "title": "Data Engineer - Platform"}
        p2 = {"company": "Snowflake", "title": "Data Engineer - Growth"}

        res = matcher.compare(p1, p2)
        assert res.is_match is False
        assert res.score == 0.0
        assert res.match_type == "no_match"
        assert "Opposing team specializations" in res.reason
        assert res.details["specialization_1"] == "platform"
        assert res.details["specialization_2"] == "growth"

    def test_platform_infrastructure_vs_growth_marketing_analytics(
        self, matcher: JobPostingMatcher
    ) -> None:
        """Platform Infrastructure vs Growth & Marketing Analytics must be rejected."""
        p1 = {"company": "DoorDash", "title": "Data Engineer, Platform Infrastructure"}
        p2 = {"company": "DoorDash", "title": "Data Engineer, Growth & Marketing Analytics"}

        res = matcher.compare(p1, p2)
        assert res.is_match is False
        assert res.match_type == "no_match"
        assert "Opposing team specializations" in res.reason

    def test_explicit_team_fields_conflict(self, matcher: JobPostingMatcher) -> None:
        """Explicit team / department fields should trigger conflict check."""
        p1 = {"company": "Uber", "title": "Data Engineer", "team": "Data Platform"}
        p2 = {"company": "Uber", "title": "Data Engineer", "team": "Marketing Analytics"}

        res = matcher.compare(p1, p2)
        assert res.is_match is False
        assert res.match_type == "no_match"
        assert "Opposing team specializations" in res.reason

    def test_one_specialized_one_unspecified_does_not_conflict(
        self, matcher: JobPostingMatcher
    ) -> None:
        """Sr. Data Engineer (general) vs Senior Data Engineer, Core should not conflict."""
        p1 = {"company": "Airbnb", "title": "Sr. Data Engineer"}
        p2 = {"company": "Airbnb", "title": "Senior Data Engineer, Core"}

        res = matcher.compare(p1, p2)
        assert res.is_match is True
        assert res.match_type == "fuzzy_high_confidence"

    def test_matching_specialization_clusters_pass(self, matcher: JobPostingMatcher) -> None:
        """Both postings in the same specialization cluster should not conflict."""
        p1 = {"company": "Stripe", "title": "Data Engineer - Platform"}
        p2 = {"company": "Stripe", "title": "Data Engineer, Platform"}

        res = matcher.compare(p1, p2)
        assert res.is_match is True
        assert res.details["specialization_1"] == "platform"
        assert res.details["specialization_2"] == "platform"


# ==============================================================================
# 5. Location / Remote Policy Compatibility (Step 5)
# ==============================================================================


class TestLocationCompatibility:
    """Tests for Step 5 location and remote compatibility."""

    def test_nyc_onsite_vs_sf_onsite_rejection(self, matcher: JobPostingMatcher) -> None:
        """Data Engineer (NYC Onsite) vs Data Engineer (SF Onsite) must NOT match."""
        p1 = {"company": "Citadel", "title": "Data Engineer (NYC Onsite)"}
        p2 = {"company": "Citadel", "title": "Data Engineer (SF Onsite)"}

        res = matcher.compare(p1, p2)
        assert res.is_match is False
        assert res.score == 0.0
        assert res.match_type == "no_match"
        assert "Incompatible onsite locations" in res.reason
        assert res.details["location_1"] == "new york"
        assert res.details["location_2"] == "san francisco"

    def test_explicit_city_and_onsite_policy_rejection(self, matcher: JobPostingMatcher) -> None:
        """Explicit city fields with onsite policy should reject different cities."""
        p1 = {
            "company": "Bloomberg",
            "title": "Data Engineer",
            "city": "New York",
            "remote_policy": "onsite",
        }
        p2 = {
            "company": "Bloomberg",
            "title": "Data Engineer",
            "city": "San Francisco",
            "remote_policy": "onsite",
        }

        res = matcher.compare(p1, p2)
        assert res.is_match is False
        assert "Incompatible onsite locations" in res.reason

    def test_city_aliases_resolve_compatibly(self, matcher: JobPostingMatcher) -> None:
        """Metropolitan aliases (NYC vs New York City) should resolve compatibly."""
        p1 = {"company": "Spotify", "title": "Data Engineer", "city": "NYC", "remote_policy": "onsite"}
        p2 = {
            "company": "Spotify",
            "title": "Data Engineer",
            "city": "New York City",
            "remote_policy": "onsite",
        }

        res = matcher.compare(p1, p2)
        assert res.is_match is True

    def test_both_remote_postings_are_compatible(self, matcher: JobPostingMatcher) -> None:
        """Postings that are both remote should be fully compatible."""
        p1 = {"company": "GitLab", "title": "Data Engineer", "remote_policy": "remote"}
        p2 = {"company": "GitLab", "title": "Data Engineer", "remote_policy": "remote"}

        res = matcher.compare(p1, p2)
        assert res.is_match is True

    def test_remote_vs_specific_city_compatible(self, matcher: JobPostingMatcher) -> None:
        """A remote posting and a posting with a home office city should be compatible."""
        p1 = {"company": "Elastic", "title": "Data Engineer (Remote)"}
        p2 = {"company": "Elastic", "title": "Data Engineer - San Francisco"}

        res = matcher.compare(p1, p2)
        assert res.is_match is True


# ==============================================================================
# 6. Title Fuzzy Scoring & RapidFuzz Integration (Step 6)
# ==============================================================================


class TestTitleFuzzyScoring:
    """Tests for Step 6 title fuzzy scoring."""

    def test_exact_match_identical_titles(self, matcher: JobPostingMatcher) -> None:
        """Exact identical titles should yield match_type='exact' and score=100.0."""
        p1 = {"company": "Stripe", "title": "Senior Data Engineer"}
        p2 = {"company": "Stripe", "title": "Senior Data Engineer"}

        res = matcher.compare(p1, p2)
        assert res.is_match is True
        assert res.score == 100.0
        assert res.match_type == "exact"
        assert "Exact company and job title match" in res.reason

    def test_fuzzy_title_matching_sr_vs_senior_core(self, matcher: JobPostingMatcher) -> None:
        """'Sr. Data Engineer' vs 'Senior Data Engineer, Core' must match with high confidence."""
        p1 = {
            "company": "Airbnb",
            "title": "Sr. Data Engineer",
            "skills": ["Python", "Presto", "Airflow"],
        }
        p2 = {
            "company": "Airbnb",
            "title": "Senior Data Engineer, Core",
            "skills": ["Python", "Presto", "Spark"],
        }

        res = matcher.compare(p1, p2)
        assert res.is_match is True
        assert res.match_type == "fuzzy_high_confidence"
        assert res.score >= 88.0
        assert res.details["token_set_ratio"] >= 88.0

    def test_abbreviation_expansion_swe_to_software_engineer(
        self, matcher: JobPostingMatcher
    ) -> None:
        """Abbreviation SWE should normalize to Software Engineer."""
        p1 = {"company": "Apple", "title": "SWE - Data Infrastructure"}
        p2 = {"company": "Apple", "title": "Software Engineer, Data Infrastructure"}

        res = matcher.compare(p1, p2)
        assert res.is_match is True
        assert res.score == 100.0

    def test_abbreviation_expansion_mle_to_machine_learning_engineer(
        self, matcher: JobPostingMatcher
    ) -> None:
        """Abbreviation MLE should expand to machine learning engineer."""
        p1 = {"company": "OpenAI", "title": "Staff MLE"}
        p2 = {"company": "OpenAI", "title": "Staff Machine Learning Engineer"}

        res = matcher.compare(p1, p2)
        assert res.is_match is True

    def test_unrelated_job_titles_rejection(self, matcher: JobPostingMatcher) -> None:
        """Completely different titles should not match."""
        p1 = {"company": "Stripe", "title": "Data Engineer"}
        p2 = {"company": "Stripe", "title": "Backend Software Engineer"}

        res = matcher.compare(p1, p2)
        assert res.is_match is False
        assert res.match_type == "no_match"
        assert res.score < 85.0
        assert "below threshold" in res.reason


# ==============================================================================
# 7. Skills Overlap Bonus & Heavy Alignment (Step 7)
# ==============================================================================


class TestSkillsOverlapBonus:
    """Tests for Step 7 skills Jaccard calculation and confidence bonus."""

    def test_skills_overlap_jaccard_calculation(self, matcher: JobPostingMatcher) -> None:
        """High skills overlap should add a bonus to the confidence score."""
        p1 = {
            "company": "Snowflake",
            "title": "Data Platform Engineer",
            "skills": ["Python", "Spark", "SQL", "dbt"],
        }
        p2 = {
            "company": "Snowflake",
            "title": "Data Platform Engineer - Foundations",
            "skills": ["Python", "Spark", "SQL", "Kafka"],
        }

        res = matcher.compare(p1, p2)
        assert res.is_match is True
        assert res.details["skills_jaccard"] > 0.5
        assert res.details["skills_bonus"] > 0.0

    def test_relaxed_threshold_with_skills_and_location_alignment(
        self, matcher: JobPostingMatcher
    ) -> None:
        """Postings with token_set_ratio >= 85.0 match when location and skills align."""
        p1 = {
            "company": "Databricks",
            "title": "Data Infrastructure Engineer",
            "city": "San Francisco",
            "skills": ["Python", "Spark", "Scala", "Kubernetes"],
        }
        p2 = {
            "company": "Databricks",
            "title": "Infrastructure Software Engineer, Data",
            "city": "San Francisco",
            "skills": ["Python", "Spark", "Scala", "Docker"],
        }

        res = matcher.compare(p1, p2)
        assert res.is_match is True
        assert res.match_type == "fuzzy_high_confidence"
        assert res.details["location_aligns"] is True
        assert res.details["skills_align"] is True


# ==============================================================================
# 8. Cross-Source Deduplication Scenarios (Greenhouse vs Lever vs HN)
# ==============================================================================


class TestCrossSourceScenarios:
    """Tests realistic cross-source deduplication matching scenarios."""

    def test_greenhouse_vs_hackernews_exact_match(self, matcher: JobPostingMatcher) -> None:
        """Identical posting scraped from Greenhouse API and Hacker News 'Who is Hiring'."""
        greenhouse_posting = JobPostingRecord(
            posting_id="gh_stripe_001",
            company="Stripe",
            title="Senior Data Engineer",
            seniority=SeniorityEnum.senior,
            city="San Francisco",
            country="US",
            remote_policy=RemotePolicyEnum.hybrid,
            salary_min=180000.0,
            salary_max=240000.0,
            currency="USD",
            employment_type=EmploymentTypeEnum.full_time,
            skills=["Python", "SQL", "Spark", "Airflow", "Kafka"],
            source_urls=["https://boards.greenhouse.io/stripe/jobs/55443322"],
            first_seen=datetime(2024, 1, 1),
            last_seen=datetime(2024, 1, 1),
        )

        hn_posting = {
            "company": "Stripe",
            "title": "Senior Data Engineer",
            "seniority": "senior",
            "city": "San Francisco",
            "remote_policy": "hybrid",
            "skills": ["Python", "SQL", "Spark", "Airflow"],
            "source_url": "https://news.ycombinator.com/item?id=38889900",
        }

        res = matcher.compare(greenhouse_posting, hn_posting)
        assert res.is_match is True
        assert res.match_type == "exact"
        assert res.score == 100.0

    def test_lever_vs_greenhouse_fuzzy_title_match(self, matcher: JobPostingMatcher) -> None:
        """Slight title divergence between Lever and Greenhouse for the same role."""
        lever_posting = {
            "company": "Palantir Technologies",
            "title": "Data Engineer, Infrastructure",
            "source_url": "https://jobs.lever.co/palantir/abc-123",
            "skills": ["Python", "Spark", "AWS"],
        }
        greenhouse_posting = {
            "company": "Palantir",
            "title": "Data Engineer - Infrastructure",
            "source_url": "https://boards.greenhouse.io/palantir/jobs/998877",
            "skills": ["Python", "Spark", "Kubernetes"],
        }

        res = matcher.compare(lever_posting, greenhouse_posting)
        assert res.is_match is True
        assert res.score == 100.0


# ==============================================================================
# 9. Robustness & Resilience Against None / Empty / Malformed Inputs
# ==============================================================================


class TestRobustnessAndEdgeCases:
    """Tests extreme edge cases and malformed input resilience."""

    def test_both_postings_none(self, matcher: JobPostingMatcher) -> None:
        """Passing None for both postings returns clean no_match."""
        res = matcher.compare(None, None)
        assert res.is_match is False
        assert res.score == 0.0
        assert res.match_type == "no_match"
        assert "None" in res.reason

    def test_one_posting_none(self, matcher: JobPostingMatcher) -> None:
        """Passing None for one posting returns clean no_match."""
        p1 = {"company": "Stripe", "title": "Data Engineer"}
        res1 = matcher.compare(p1, None)
        assert res1.is_match is False
        assert res1.score == 0.0

        res2 = matcher.compare(None, p1)
        assert res2.is_match is False
        assert res2.score == 0.0

    def test_both_empty_dictionaries(self, matcher: JobPostingMatcher) -> None:
        """Empty dictionaries should cleanly return no_match."""
        res = matcher.compare({}, {})
        assert res.is_match is False
        assert res.match_type == "no_match"

    def test_all_none_fields_in_dict(self, matcher: JobPostingMatcher) -> None:
        """Postings with explicit None values for all fields should not crash."""
        p1 = {
            "company": None,
            "title": None,
            "skills": None,
            "source_url": None,
            "city": None,
            "remote_policy": None,
        }
        p2 = {
            "company": None,
            "title": None,
            "skills": None,
            "source_url": None,
            "city": None,
            "remote_policy": None,
        }
        res = matcher.compare(p1, p2)
        assert res.is_match is False
        assert res.match_type == "no_match"

    def test_raw_extraction_output_pydantic_model(self, matcher: JobPostingMatcher) -> None:
        """RawExtractionOutput models should seamlessly compare."""
        r1 = RawExtractionOutput(
            company="Stripe",
            title="Senior Data Engineer",
            seniority=SeniorityEnum.senior,
            skills=["Python", "SQL"],
        )
        r2 = RawExtractionOutput(
            company="Stripe",
            title="Senior Data Engineer",
            seniority=SeniorityEnum.senior,
            skills=["Python", "Spark"],
        )

        res = matcher.compare(r1, r2)
        assert res.is_match is True
        assert res.match_type == "exact"
        assert res.score == 100.0

    def test_whitespace_and_special_characters_resilience(
        self, matcher: JobPostingMatcher
    ) -> None:
        """Extra whitespace, tabs, and punctuation in titles should not prevent matching."""
        p1 = {"company": "  Stripe  ", "title": "\tSenior   Data   Engineer  \n"}
        p2 = {"company": "Stripe", "title": "Senior Data Engineer"}

        res = matcher.compare(p1, p2)
        assert res.is_match is True
        assert res.score == 100.0
