"""Unit tests for Title Standardization, Seniority Disambiguation, and Specialization Parser.

Validates:
1. Noise and requisition stripping:
   - Req IDs, parentheticals, bracketed locations, remote markers, diversity tags (f/m/d),
     employment commitment noise (Contract, Full-Time).
2. Seniority level categorization across Roman numerals, prefixes, and suffixes:
   - staff_plus (Staff, Principal, Distinguished, Lead, Director, Head of, Architect)
   - senior (Senior, Sr., Sr, III, IV)
   - mid (II, Intermediate, Mid-level, standard unadorned titles)
   - junior (Junior, Jr., Jr, Associate, Entry Level, I, Intern, Internship)
3. Core role family classification:
   - data_engineer, ml_engineer, analytics_engineer, platform_engineer, software_engineer, other
4. Team / domain specialization extraction:
   - Hyphens, commas, pipes, slashes, parentheticals.
   - Guard against mistaking seniority or noise as specialization.
5. Strict seniority compatibility checks:
   - Identical seniorities match.
   - Unadorned / None compatible with mid or senior under fuzzy tolerance.
   - Strict mismatch guards: Junior vs Senior, Junior vs Staff+, Mid vs Staff+, Senior vs Staff+.
6. Edge cases:
   - None/empty input, whitespace, malformed strings, numbers, dict-subscript access.
"""

from __future__ import annotations

import pytest

from pipeline.dedup.title_parser import ParsedTitle, TitleParser
from pipeline.models.schema import SeniorityEnum


@pytest.fixture
def parser() -> TitleParser:
    """Provides a TitleParser instance with default configuration."""
    return TitleParser()


@pytest.fixture
def parser_mid_default() -> TitleParser:
    """Provides a TitleParser instance where unadorned titles default to mid."""
    return TitleParser(default_unadorned_as_mid=True)


# ==============================================================================
# 1. Noise and Requisition Stripping Tests
# ==============================================================================


class TestNoiseAndRequisitionStripping:
    """Tests stripping of requisition IDs, diversity markers, remote tags, and noise."""

    @pytest.mark.parametrize(
        "raw_title, expected_clean, expected_spec",
        [
            (
                "Data Engineer (Req #1234)",
                "Data Engineer",
                None,
            ),
            (
                "Senior Data Engineer [REQ-5678]",
                "Senior Data Engineer",
                None,
            ),
            (
                "Data Engineer (Job ID: 99887)",
                "Data Engineer",
                None,
            ),
            (
                "Machine Learning Engineer (JR-1029)",
                "Machine Learning Engineer",
                None,
            ),
            (
                "Analytics Engineer (Req: 44321)",
                "Analytics Engineer",
                None,
            ),
        ],
    )
    def test_strip_requisition_ids(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_clean: str,
        expected_spec: str | None,
    ):
        """Verifies requisition IDs in various formats are completely stripped."""
        res = parser.parse_title(raw_title)
        assert res.clean_title == expected_clean
        assert res.team_specialization == expected_spec

    @pytest.mark.parametrize(
        "raw_title, expected_clean",
        [
            ("Data Engineer [Remote]", "Data Engineer"),
            ("Senior Data Engineer (Remote)", "Senior Data Engineer"),
            ("Data Engineer (US Remote)", "Data Engineer"),
            ("ML Engineer - Remote (US)", "Machine Learning Engineer"),
            ("Data Platform Engineer 100% Remote", "Data Platform Engineer"),
            ("Analytics Engineer (WFH)", "Analytics Engineer"),
            ("Data Engineer (Hybrid)", "Data Engineer"),
            ("Data Engineer (Onsite)", "Data Engineer"),
            ("Data Engineer (In-Office)", "Data Engineer"),
        ],
    )
    def test_strip_remote_and_workplace_tags(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_clean: str,
    ):
        """Verifies remote, hybrid, onsite, and WFH tags are cleanly stripped."""
        res = parser.parse_title(raw_title)
        assert res.clean_title == expected_clean
        assert res.team_specialization is None

    @pytest.mark.parametrize(
        "raw_title, expected_clean",
        [
            ("Data Engineer (US)", "Data Engineer"),
            ("Senior Data Engineer (USA)", "Senior Data Engineer"),
            ("Data Engineer [US]", "Data Engineer"),
            ("Data Engineer (NYC/SF)", "Data Engineer"),
            ("Senior Data Engineer (SF/NYC)", "Senior Data Engineer"),
            ("Data Engineer (New York, NY)", "Data Engineer"),
            ("Data Engineer (San Francisco, CA)", "Data Engineer"),
            ("Data Engineer (Austin, TX)", "Data Engineer"),
            ("Data Engineer (Bay Area)", "Data Engineer"),
            ("Data Engineer (London)", "Data Engineer"),
        ],
    )
    def test_strip_locations_and_city_tags(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_clean: str,
    ):
        """Verifies country, city, and metro location parentheticals are stripped."""
        res = parser.parse_title(raw_title)
        assert res.clean_title == expected_clean
        assert res.team_specialization is None

    @pytest.mark.parametrize(
        "raw_title, expected_clean",
        [
            ("Data Engineer (f/m/d)", "Data Engineer"),
            ("Data Engineer (m/f/d)", "Data Engineer"),
            ("Data Engineer (d/f/m)", "Data Engineer"),
            ("Data Engineer (m/w/d)", "Data Engineer"),
            ("Data Engineer (all genders)", "Data Engineer"),
            ("Data Engineer (he/him)", "Data Engineer"),
        ],
    )
    def test_strip_gender_diversity_markers(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_clean: str,
    ):
        """Verifies diversity markers (f/m/d, all genders, pronouns) are stripped."""
        res = parser.parse_title(raw_title)
        assert res.clean_title == expected_clean
        assert res.team_specialization is None

    @pytest.mark.parametrize(
        "raw_title, expected_clean",
        [
            ("Data Engineer (Full-Time)", "Data Engineer"),
            ("Data Engineer - Full-Time", "Data Engineer"),
            ("Data Engineer - Full Time", "Data Engineer"),
            ("Data Engineer (Part-Time)", "Data Engineer"),
            ("Data Engineer (Contract)", "Data Engineer"),
            ("Data Engineer - Contract", "Data Engineer"),
            ("Data Engineer (Contractor)", "Data Engineer"),
            ("Data Engineer (Permanent)", "Data Engineer"),
            ("Data Engineer (Direct-Hire)", "Data Engineer"),
            ("Data Engineer (C2C)", "Data Engineer"),
            ("Data Engineer (W2)", "Data Engineer"),
        ],
    )
    def test_strip_employment_commitment_noise(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_clean: str,
    ):
        """Verifies employment types (Contract, Full-Time, C2C) are stripped."""
        res = parser.parse_title(raw_title)
        assert res.clean_title == expected_clean
        assert res.team_specialization is None

    def test_stacked_noise_stripping(self, parser: TitleParser):
        """Verifies that multiple noise items stacked together are all removed."""
        raw = "Senior Data Engineer (Req #1234) [Remote] (f/m/d) Full-Time (NYC/SF)"
        res = parser.parse_title(raw)
        assert res.clean_title == "Senior Data Engineer"
        assert res.seniority == "senior"
        assert res.core_role == "data_engineer"
        assert res.team_specialization is None


# ==============================================================================
# 2. Seniority Level Categorization Tests
# ==============================================================================


class TestSeniorityLevelCategorization:
    """Tests seniority extraction across Roman numerals, prefixes, and suffixes."""

    @pytest.mark.parametrize(
        "raw_title, expected_tier",
        [
            ("Staff Data Engineer", "staff_plus"),
            ("Principal Machine Learning Engineer", "staff_plus"),
            ("Distinguished Data Architect", "staff_plus"),
            ("Fellow Data Engineer", "staff_plus"),
            ("Lead Data Engineer", "staff_plus"),
            ("Lead Machine Learning Engineer", "staff_plus"),
            ("Lead Analytics Engineer", "staff_plus"),
            ("Director of Data Engineering", "staff_plus"),
            ("Head of Data Engineering", "staff_plus"),
            ("Chief Data Architect", "staff_plus"),
            ("Data Architect", "staff_plus"),
            ("Platform Architect", "staff_plus"),
            ("VP of Data Platform", "staff_plus"),
        ],
    )
    def test_staff_plus_tier(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_tier: str,
    ):
        """Verifies staff_plus tier detection across all executive/staff keywords."""
        res = parser.parse_title(raw_title)
        assert res.seniority == expected_tier

    @pytest.mark.parametrize(
        "raw_title, expected_tier",
        [
            ("Senior Data Engineer", "senior"),
            ("Sr. Data Engineer", "senior"),
            ("Sr Data Engineer", "senior"),
            ("Data Engineer, Senior", "senior"),
            ("Data Engineer - Sr.", "senior"),
            ("Data Engineer (Senior)", "senior"),
            ("Data Engineer III", "senior"),
            ("Data Engineer IV", "senior"),
            ("Data Engineer Level 3", "senior"),
            ("Data Engineer Level 4", "senior"),
            ("Data Engineer Level 5", "senior"),
            ("Senior ML Engineer", "senior"),
            ("Senior Analytics Engineer", "senior"),
            ("Senior Platform Engineer", "senior"),
        ],
    )
    def test_senior_tier(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_tier: str,
    ):
        """Verifies senior tier detection across Roman numerals, prefixes, and suffixes."""
        res = parser.parse_title(raw_title)
        assert res.seniority == expected_tier

    @pytest.mark.parametrize(
        "raw_title, expected_tier",
        [
            ("Data Engineer II", "mid"),
            ("Intermediate Data Engineer", "mid"),
            ("Mid-Level Data Engineer", "mid"),
            ("Mid Data Engineer", "mid"),
            ("Data Engineer Level 2", "mid"),
            ("Data Engineer Level II", "mid"),
            ("ML Engineer II", "mid"),
            ("Analytics Engineer II", "mid"),
        ],
    )
    def test_mid_tier_explicit(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_tier: str,
    ):
        """Verifies mid tier detection for explicit II, Intermediate, or Mid words."""
        res = parser.parse_title(raw_title)
        assert res.seniority == expected_tier

    def test_unadorned_seniority_handling(
        self,
        parser: TitleParser,
        parser_mid_default: TitleParser,
    ):
        """Verifies unadorned titles have seniority=None by default, and 'mid' when configured."""
        raw = "Data Engineer"

        # Default configuration: unadorned titles have seniority=None
        res_default = parser.parse_title(raw)
        assert res_default.seniority is None
        assert res_default.clean_title == "Data Engineer"
        assert res_default.core_role == "data_engineer"

        # Explicit per-call override
        res_override = parser.parse_title(raw, unadorned_as_mid=True)
        assert res_override.seniority == "mid"

        # Instance default configured with default_unadorned_as_mid=True
        res_mid = parser_mid_default.parse_title(raw)
        assert res_mid.seniority == "mid"

    @pytest.mark.parametrize(
        "raw_title, expected_tier",
        [
            ("Junior Data Engineer", "junior"),
            ("Jr. Data Engineer", "junior"),
            ("Jr Data Engineer", "junior"),
            ("Data Engineer, Junior", "junior"),
            ("Associate Data Engineer", "junior"),
            ("Associate ML Engineer", "junior"),
            ("Entry Level Data Engineer", "junior"),
            ("Entry-Level Data Engineer", "junior"),
            ("Data Engineer I", "junior"),
            ("Data Engineer Level 1", "junior"),
            ("Data Engineer Level I", "junior"),
            ("Data Engineer Intern", "junior"),
            ("Data Engineering Internship", "junior"),
            ("Machine Learning Intern", "junior"),
            ("Graduate Data Engineer", "junior"),
        ],
    )
    def test_junior_tier(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_tier: str,
    ):
        """Verifies junior tier detection across prefixes, suffixes, intern, and numeral I."""
        res = parser.parse_title(raw_title)
        assert res.seniority == expected_tier


# ==============================================================================
# 3. Core Role Family Classification Tests
# ==============================================================================


class TestCoreRoleClassification:
    """Tests core role family classification across engineering disciplines."""

    @pytest.mark.parametrize(
        "raw_title, expected_role",
        [
            ("Data Engineer", "data_engineer"),
            ("Big Data Engineer", "data_engineer"),
            ("Data Pipeline Engineer", "data_engineer"),
            ("Data Integration Engineer", "data_engineer"),
            ("Data Warehouse Engineer", "data_engineer"),
            ("ETL Developer", "data_engineer"),
            ("ETL Engineer", "data_engineer"),
            ("Database Engineer", "data_engineer"),
            ("Senior Data Engineer", "data_engineer"),
            ("Staff Big Data Engineer", "data_engineer"),
        ],
    )
    def test_data_engineer_family(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_role: str,
    ):
        """Verifies classification into data_engineer family."""
        res = parser.parse_title(raw_title)
        assert res.core_role == expected_role

    @pytest.mark.parametrize(
        "raw_title, expected_role",
        [
            ("Machine Learning Engineer", "ml_engineer"),
            ("ML Engineer", "ml_engineer"),
            ("AI Engineer", "ml_engineer"),
            ("AI/ML Engineer", "ml_engineer"),
            ("AI and ML Engineer", "ml_engineer"),
            ("MLOps Engineer", "ml_engineer"),
            ("Deep Learning Engineer", "ml_engineer"),
            ("Machine Learning Scientist", "ml_engineer"),
            ("ML Scientist", "ml_engineer"),
            ("Computer Vision Engineer", "ml_engineer"),
            ("NLP Engineer", "ml_engineer"),
            ("ML Infrastructure Engineer", "ml_engineer"),
            ("Generative AI Engineer", "ml_engineer"),
        ],
    )
    def test_ml_engineer_family(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_role: str,
    ):
        """Verifies classification into ml_engineer family."""
        res = parser.parse_title(raw_title)
        assert res.core_role == expected_role

    @pytest.mark.parametrize(
        "raw_title, expected_role",
        [
            ("Analytics Engineer", "analytics_engineer"),
            ("Senior Analytics Engineer", "analytics_engineer"),
            ("BI Engineer", "analytics_engineer"),
            ("Business Intelligence Engineer", "analytics_engineer"),
            ("BI Developer", "analytics_engineer"),
            ("Business Intelligence Developer", "analytics_engineer"),
            ("Data Analytics Engineer", "analytics_engineer"),
        ],
    )
    def test_analytics_engineer_family(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_role: str,
    ):
        """Verifies classification into analytics_engineer family."""
        res = parser.parse_title(raw_title)
        assert res.core_role == expected_role

    @pytest.mark.parametrize(
        "raw_title, expected_role",
        [
            ("Platform Engineer", "platform_engineer"),
            ("Data Platform Engineer", "platform_engineer"),
            ("Infrastructure Engineer", "platform_engineer"),
            ("Data Infrastructure Engineer", "platform_engineer"),
            ("Cloud Platform Engineer", "platform_engineer"),
            ("Platform Systems Engineer", "platform_engineer"),
        ],
    )
    def test_platform_engineer_family(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_role: str,
    ):
        """Verifies classification into platform_engineer family."""
        res = parser.parse_title(raw_title)
        assert res.core_role == expected_role

    @pytest.mark.parametrize(
        "raw_title, expected_role",
        [
            ("Software Engineer", "software_engineer"),
            ("Senior Software Engineer", "software_engineer"),
            ("Software Developer", "software_engineer"),
            ("SWE", "software_engineer"),
            ("Backend Engineer", "software_engineer"),
            ("Backend Developer", "software_engineer"),
            ("Systems Engineer", "software_engineer"),
            ("Full Stack Engineer", "software_engineer"),
        ],
    )
    def test_software_engineer_family(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_role: str,
    ):
        """Verifies classification into software_engineer family."""
        res = parser.parse_title(raw_title)
        assert res.core_role == expected_role

    @pytest.mark.parametrize(
        "raw_title, expected_role",
        [
            ("Software Engineer - Data", "data_engineer"),
            ("Software Engineer (Data)", "data_engineer"),
            ("Software Engineer - Machine Learning", "ml_engineer"),
            ("Software Engineer (AI/ML)", "ml_engineer"),
            ("Software Engineer - Analytics", "analytics_engineer"),
            ("Software Engineer (Infrastructure)", "platform_engineer"),
        ],
    )
    def test_reverse_formulations(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_role: str,
    ):
        """Verifies reverse formulations like 'Software Engineer (Data)' resolve to target roles."""
        res = parser.parse_title(raw_title)
        assert res.core_role == expected_role

    @pytest.mark.parametrize(
        "raw_title",
        [
            "Product Manager",
            "Technical Product Manager",
            "Data Analyst",
            "Data Scientist",
            "UX Designer",
            "Account Executive",
            "Recruiter",
            "Sales Engineer",
            "Customer Support Specialist",
        ],
    )
    def test_other_role_family(
        self,
        parser: TitleParser,
        raw_title: str,
    ):
        """Verifies non-target roles correctly classify as 'other'."""
        res = parser.parse_title(raw_title)
        assert res.core_role == "other"


# ==============================================================================
# 4. Team / Domain Specialization Extraction Tests
# ==============================================================================


class TestTeamSpecializationExtraction:
    """Tests extraction of team and domain specializations."""

    @pytest.mark.parametrize(
        "raw_title, expected_spec",
        [
            ("Data Engineer - Infrastructure", "Infrastructure"),
            ("Senior Data Engineer – Growth", "Growth"),
            ("ML Engineer | NLP", "NLP"),
            ("Data Engineer / Core Platform", "Core Platform"),
            ("Platform Engineer - Cloud Operations", "Cloud Operations"),
            ("Data Engineer - Core Data Pipeline", "Core Data Pipeline"),
        ],
    )
    def test_specialization_via_separators(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_spec: str,
    ):
        """Verifies specializations separated by -, –, |, or / are extracted."""
        res = parser.parse_title(raw_title)
        assert res.team_specialization == expected_spec

    @pytest.mark.parametrize(
        "raw_title, expected_spec",
        [
            ("Senior Data Engineer, Growth", "Growth"),
            ("Data Engineer, Product Analytics", "Product Analytics"),
            ("Lead Machine Learning Engineer, Recommendations", "Recommendations"),
            ("Staff Data Engineer, Core Infrastructure", "Core Infrastructure"),
            ("ML Engineer, Computer Vision", "Computer Vision"),
        ],
    )
    def test_specialization_via_commas(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_spec: str,
    ):
        """Verifies specializations separated by commas are extracted."""
        res = parser.parse_title(raw_title)
        assert res.team_specialization == expected_spec

    @pytest.mark.parametrize(
        "raw_title, expected_spec",
        [
            ("Staff Machine Learning Engineer (Search & Ranking)", "Search & Ranking"),
            ("Data Engineer (Data Warehouse)", "Data Warehouse"),
            ("Senior Data Engineer (Streaming)", "Streaming"),
            ("Platform Engineer (Core Infrastructure)", "Core Infrastructure"),
            ("Staff Machine Learning Engineer (AI/ML)", "AI/ML"),
            ("Data Engineer (Platform)", "Platform"),
        ],
    )
    def test_specialization_via_parentheses(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_spec: str,
    ):
        """Verifies parenthetical team specializations are correctly extracted."""
        res = parser.parse_title(raw_title)
        assert res.team_specialization == expected_spec

    def test_specialization_with_noise_and_requisition(self, parser: TitleParser):
        """Verifies specialization is preserved when noise and requisition IDs are present."""
        raw = "Data Platform Engineer - Core Infrastructure (Req #1234) [Remote] (f/m/d)"
        res = parser.parse_title(raw)
        assert res.clean_title == "Data Platform Engineer"
        assert res.core_role == "platform_engineer"
        assert res.team_specialization == "Core Infrastructure"

    @pytest.mark.parametrize(
        "raw_title, expected_clean, expected_seniority",
        [
            ("Data Engineer, Senior", "Senior Data Engineer", "senior"),
            ("Data Engineer - Sr.", "Senior Data Engineer", "senior"),
            ("Data Engineer - Lead", "Lead Data Engineer", "staff_plus"),
            ("Data Engineer - Principal", "Principal Data Engineer", "staff_plus"),
            ("Data Engineer III", "Data Engineer III", "senior"),
            ("Data Engineer II", "Data Engineer II", "mid"),
        ],
    )
    def test_seniority_suffix_not_mistaken_for_specialization(
        self,
        parser: TitleParser,
        raw_title: str,
        expected_clean: str,
        expected_seniority: str,
    ):
        """Verifies that a suffix indicating seniority is NOT extracted as specialization."""
        res = parser.parse_title(raw_title)
        assert res.team_specialization is None
        assert res.seniority == expected_seniority
        assert res.clean_title == expected_clean


# ==============================================================================
# 5. Seniority Compatibility Matrix Tests
# ==============================================================================


class TestSeniorityCompatibility:
    """Tests are_seniorities_compatible for fuzzy deduplication and mismatch guards."""

    @pytest.mark.parametrize(
        "s1, s2",
        [
            ("senior", "senior"),
            ("junior", "junior"),
            ("mid", "mid"),
            ("staff_plus", "staff_plus"),
            (None, None),
            ("SENIOR", "senior"),
            ("sr", "senior"),
            ("lead", "staff_plus"),
            ("associate", "junior"),
        ],
    )
    def test_identical_seniorities_are_compatible(
        self,
        s1: str | None,
        s2: str | None,
    ):
        """Verifies identical seniorities always evaluate to True."""
        assert TitleParser.are_seniorities_compatible(s1, s2) is True

    @pytest.mark.parametrize(
        "s1, s2",
        [
            (None, "mid"),
            ("mid", None),
            (None, "senior"),
            ("senior", None),
        ],
    )
    def test_unadorned_compatible_with_mid_and_senior(
        self,
        s1: str | None,
        s2: str | None,
    ):
        """Verifies unadorned (None) is compatible with mid or senior under fuzzy tolerance."""
        assert TitleParser.are_seniorities_compatible(s1, s2) is True

    @pytest.mark.parametrize(
        "s1, s2",
        [
            (None, "junior"),
            ("junior", None),
            (None, "staff_plus"),
            ("staff_plus", None),
        ],
    )
    def test_unadorned_incompatible_with_junior_and_staff(
        self,
        s1: str | None,
        s2: str | None,
    ):
        """Verifies unadorned (None) is NEVER compatible with junior or staff_plus."""
        assert TitleParser.are_seniorities_compatible(s1, s2) is False

    @pytest.mark.parametrize(
        "s1, s2, rationale",
        [
            ("junior", "senior", "Junior vs Senior must NEVER merge"),
            ("senior", "junior", "Senior vs Junior must NEVER merge"),
            ("junior", "staff_plus", "Junior vs Staff+ must NEVER merge"),
            ("staff_plus", "junior", "Staff+ vs Junior must NEVER merge"),
            ("mid", "staff_plus", "Mid vs Staff+ must NEVER merge"),
            ("staff_plus", "mid", "Staff+ vs Mid must NEVER merge"),
            ("senior", "staff_plus", "Staff and Senior are distinct concurrent requisition levels"),
            ("staff_plus", "senior", "Senior and Staff are distinct concurrent requisition levels"),
            ("junior", "mid", "Junior and Mid are distinct levels"),
            ("mid", "junior", "Mid and Junior are distinct levels"),
            ("mid", "senior", "Mid and Senior are distinct requisition levels"),
            ("senior", "mid", "Senior and Mid are distinct requisition levels"),
        ],
    )
    def test_strict_mismatch_guards(
        self,
        s1: str,
        s2: str,
        rationale: str,
    ):
        """Verifies explicit mismatch guards between distinct seniority tiers."""
        result = TitleParser.are_seniorities_compatible(s1, s2)
        assert result is False, f"Failed guard: {rationale}"

    def test_seniority_enum_compatibility(self):
        """Verifies are_seniorities_compatible seamlessly works with SeniorityEnum instances."""
        assert TitleParser.are_seniorities_compatible(SeniorityEnum.senior, SeniorityEnum.senior) is True
        assert TitleParser.are_seniorities_compatible(SeniorityEnum.senior, "senior") is True
        assert TitleParser.are_seniorities_compatible(SeniorityEnum.senior, SeniorityEnum.staff_plus) is False
        assert TitleParser.are_seniorities_compatible(None, SeniorityEnum.senior) is True
        assert TitleParser.are_seniorities_compatible(None, SeniorityEnum.mid) is True
        assert TitleParser.are_seniorities_compatible(None, SeniorityEnum.junior) is False
        assert TitleParser.are_seniorities_compatible(None, SeniorityEnum.staff_plus) is False


# ==============================================================================
# 6. Edge Cases and Robustness Tests
# ==============================================================================


class TestEdgeCasesAndRobustness:
    """Tests edge cases: None, empty strings, whitespace, numbers, malformed text."""

    def test_none_input(self, parser: TitleParser):
        """Verifies None input returns a graceful empty ParsedTitle without crashing."""
        res = parser.parse_title(None)
        assert isinstance(res, ParsedTitle)
        assert res.raw_title == ""
        assert res.clean_title == ""
        assert res.seniority is None
        assert res.core_role == "other"
        assert res.team_specialization is None

    def test_empty_string(self, parser: TitleParser):
        """Verifies empty string returns a graceful empty ParsedTitle."""
        res = parser.parse_title("")
        assert res.clean_title == ""
        assert res.seniority is None
        assert res.core_role == "other"
        assert res.team_specialization is None

    def test_whitespace_only(self, parser: TitleParser):
        """Verifies whitespace-only string returns a graceful empty ParsedTitle."""
        res = parser.parse_title("   \t\n   ")
        assert res.clean_title == ""
        assert res.seniority is None
        assert res.core_role == "other"

    def test_malformed_punctuation(self, parser: TitleParser):
        """Verifies bizarre punctuation symbols are stripped cleanly."""
        raw = "*** Senior Data Engineer *** /// Infrastructure --- [Remote] !!!"
        res = parser.parse_title(raw)
        assert res.clean_title == "Senior Data Engineer"
        assert res.seniority == "senior"
        assert res.core_role == "data_engineer"
        assert res.team_specialization == "Infrastructure"

    def test_titles_with_numbers(self, parser: TitleParser):
        """Verifies titles containing years, cohorts, or levels parse properly."""
        # Year in title
        res_year = parser.parse_title("Data Engineer - 2024 Hiring Cohort [Remote]")
        assert res_year.clean_title == "Data Engineer"
        assert res_year.core_role == "data_engineer"

        # Levels 1, 2, 3
        res_l1 = parser.parse_title("Data Engineer Level 1")
        assert res_l1.seniority == "junior"
        res_l2 = parser.parse_title("Data Engineer Level 2")
        assert res_l2.seniority == "mid"
        res_l3 = parser.parse_title("Data Engineer Level 3")
        assert res_l3.seniority == "senior"

    def test_case_insensitivity(self, parser: TitleParser):
        """Verifies parsing is fully case-insensitive across uppercase and mixed case."""
        res_upper = parser.parse_title("SENIOR DATA ENGINEER - INFRASTRUCTURE [REMOTE]")
        assert res_upper.clean_title == "Senior Data Engineer"
        assert res_upper.seniority == "senior"
        assert res_upper.core_role == "data_engineer"
        assert res_upper.team_specialization == "INFRASTRUCTURE"

        res_mixed = parser.parse_title("sEnIoR dAtA eNgInEeR")
        assert res_mixed.clean_title == "Senior Data Engineer"
        assert res_mixed.seniority == "senior"

    def test_dict_subscript_and_get_compatibility(self, parser: TitleParser):
        """Verifies ParsedTitle supports dict-like subscript and get() access."""
        res = parser.parse_title("Senior Data Engineer, Growth")

        # Property access
        assert res.clean_title == "Senior Data Engineer"
        assert res.seniority == "senior"

        # Dict subscript access
        assert res["title"] == "Senior Data Engineer"
        assert res["clean_title"] == "Senior Data Engineer"
        assert res["seniority"] == "senior"
        assert res["core_role"] == "data_engineer"
        assert res["team_specialization"] == "Growth"

        # Dict get() access
        assert res.get("title") == "Senior Data Engineer"
        assert res.get("seniority") == "senior"
        assert res.get("non_existent", "fallback") == "fallback"

    def test_parse_alias_method(self, parser: TitleParser):
        """Verifies that parse() behaves identically to parse_title()."""
        raw = "Staff Machine Learning Engineer (Search & Ranking)"
        res1 = parser.parse_title(raw)
        res2 = parser.parse(raw)
        assert res1 == res2
