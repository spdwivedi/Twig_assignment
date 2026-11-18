"""Comprehensive pytest suite for Company & Location Normalizer (Phase 3 Deduplication).

Tests:
1. Company suffix stripping across all legal forms.
2. Known company canonical alias mappings.
3. Case insensitivity and punctuation resilience.
4. Location parsing for US cities and states (both full names and abbreviations).
5. Remote flag detection across diverse ATS phrasing.
6. Edge cases: None, empty strings, international locations, weird whitespace, zip codes.
7. Class, classmethod, and standalone function APIs.
"""

from __future__ import annotations

import pytest

from pipeline.dedup.normalizer import (
    CANONICAL_COMPANY_ALIASES,
    COUNTRY_MAP,
    KNOWN_US_CITIES,
    LEGAL_SUFFIXES,
    US_STATE_CODES,
    US_STATES,
    CompanyNormalizer,
    LocationNormalizer,
    normalize_company,
    normalize_location,
)


# ==============================================================================
# 1. Company Suffix Stripping Tests
# ==============================================================================


class TestCompanySuffixStripping:
    """Verifies that all specified corporate legal suffixes are cleanly stripped."""

    @pytest.mark.parametrize(
        "suffix",
        [
            "Inc",
            "Inc.",
            "LLC",
            "L.L.C.",
            "Corp",
            "Corp.",
            "Corporation",
            "Technologies",
            "Technology",
            "Tech",
            "Labs",
            "Laboratories",
            "Co",
            "Co.",
            "Company",
            "Group",
            "Software",
            "Global",
            "Solutions",
            "Systems",
            "Holdings",
            "LLP",
        ],
    )
    def test_strip_specified_legal_suffixes(self, suffix: str) -> None:
        """Every suffix explicitly named in the assignment objectives must be stripped."""
        raw = f"Acme {suffix}"
        assert normalize_company(raw) == "Acme"

    @pytest.mark.parametrize(
        "suffix",
        [
            "Ltd",
            "Ltd.",
            "Limited",
            "GmbH",
            "B.V.",
            "BV",
            "Pvt",
            "Pvt.",
            "PLC",
            "P.L.C.",
            "P.C.",
            "PC",
            "L.P.",
            "LP",
            "S.A.",
            "PBC",
        ],
    )
    def test_strip_international_and_specialized_suffixes(self, suffix: str) -> None:
        """International and specialized corporate entities must also be stripped."""
        raw = f"Acme {suffix}"
        assert normalize_company(raw) == "Acme"

    def test_chained_multiple_suffixes(self) -> None:
        """Multiple nested or chained legal suffixes must be stripped iteratively."""
        assert normalize_company("Palantir Tech Inc.") == "Palantir"
        assert normalize_company("Palantir Technologies, Inc.") == "Palantir"
        assert normalize_company("Acme Global Solutions LLC") == "Acme"
        assert normalize_company("Acme Holdings Group Inc.") == "Acme"
        assert normalize_company("Apex Software Labs Co.") == "Apex"
        assert normalize_company("Summit Systems & Technology LLC") == "Summit"

    def test_suffix_is_entire_company_name_preserved(self) -> None:
        """If a company name is composed solely of a suffix word, it must not be wiped out."""
        assert normalize_company("Tech") == "Tech"
        assert normalize_company("Company") == "Company"
        assert normalize_company("Technologies") == "Technologies"
        assert normalize_company("Solutions") == "Solutions"
        assert normalize_company("Holdings") == "Holdings"
        assert normalize_company("Software") == "Software"


# ==============================================================================
# 2. Known Company Alias Mapping Tests
# ==============================================================================


class TestCompanyCanonicalAliases:
    """Verifies canonical alias dictionary mappings requested by the assignment."""

    @pytest.mark.parametrize(
        ("input_name", "expected_canonical"),
        [
            # Stripe
            ("stripe", "Stripe"),
            ("stripe, inc.", "Stripe"),
            ("stripe inc", "Stripe"),
            ("stripe inc.", "Stripe"),
            ("Stripe, Inc.", "Stripe"),
            ("STRIPE", "Stripe"),
            # Airbnb
            ("airbnb", "Airbnb"),
            ("airbnb, inc.", "Airbnb"),
            ("airbnb inc.", "Airbnb"),
            ("airbnb inc", "Airbnb"),
            ("AIRBNB, INC.", "Airbnb"),
            # Databricks
            ("databricks", "Databricks"),
            ("databricks inc.", "Databricks"),
            ("databricks software", "Databricks"),
            ("DATABRICKS SOFTWARE", "Databricks"),
            ("Databricks, Inc.", "Databricks"),
            # Palantir
            ("palantir", "Palantir"),
            ("palantir technologies", "Palantir"),
            ("palantir tech inc", "Palantir"),
            ("Palantir Technologies, Inc.", "Palantir"),
            ("PALANTIR TECH INC", "Palantir"),
            # Plaid
            ("plaid", "Plaid"),
            ("plaid inc", "Plaid"),
            ("plaid inc.", "Plaid"),
            ("Plaid, Inc.", "Plaid"),
            # Figma
            ("figma", "Figma"),
            ("figma inc.", "Figma"),
            ("figma inc", "Figma"),
            ("FIGMA INC.", "Figma"),
            # GitLab
            ("gitlab", "GitLab"),
            ("gitlab inc.", "GitLab"),
            ("gitlab bv", "GitLab"),
            ("GitLab B.V.", "GitLab"),
            ("GITLAB INC", "GitLab"),
            # Cloudflare
            ("cloudflare", "Cloudflare"),
            ("cloudflare inc.", "Cloudflare"),
            ("Cloudflare, Inc.", "Cloudflare"),
            ("CLOUDFLARE INC", "Cloudflare"),
        ],
    )
    def test_canonical_alias_mappings_mandated(
        self, input_name: str, expected_canonical: str
    ) -> None:
        """All canonical brand alias mappings must resolve to canonical brand casing."""
        assert normalize_company(input_name) == expected_canonical

    @pytest.mark.parametrize(
        ("input_name", "expected_canonical"),
        [
            ("google llc", "Google"),
            ("meta platforms inc", "Meta"),
            ("apple inc.", "Apple"),
            ("microsoft corp.", "Microsoft"),
            ("amazon.com inc", "Amazon"),
            ("netflix inc.", "Netflix"),
            ("uber technologies", "Uber"),
            ("snowflake inc", "Snowflake"),
            ("openai llc", "OpenAI"),
            ("anthropic pbc", "Anthropic"),
            ("github inc", "GitHub"),
        ],
    )
    def test_extended_tech_company_aliases(
        self, input_name: str, expected_canonical: str
    ) -> None:
        """Common tier-1 tech companies must resolve to standard brand casing."""
        assert normalize_company(input_name) == expected_canonical


# ==============================================================================
# 3. Case Insensitivity & Punctuation Resilience
# ==============================================================================


class TestCompanyPunctuationAndCasing:
    """Verifies behavior with weird punctuation, whitespace, and brand casing."""

    def test_extraneous_punctuation_and_whitespace(self) -> None:
        """Punctuation, symbols, quotes, and whitespace must be stripped cleanly."""
        assert normalize_company("  -- Stripe, Inc. .. ") == "Stripe"
        assert normalize_company("  \"Airbnb, Inc.\"  ") == "Airbnb"
        assert normalize_company("Databricks - Software") == "Databricks"
        assert normalize_company("Figma / Inc.") == "Figma"
        assert normalize_company("Plaid (Inc.)") == "Plaid"
        assert normalize_company("GitLab [BV]") == "GitLab"
        assert normalize_company("Cloudflare, Inc.") == "Cloudflare"

    def test_preserves_non_alias_brand_casing(self) -> None:
        """Mixed brand casing for unaliased companies should be preserved."""
        assert normalize_company("OpenAI Labs") == "OpenAI"
        assert normalize_company("eBay Inc.") == "eBay"
        assert normalize_company("FedEx Express Corp.") == "FedEx Express"
        assert normalize_company("McGraw-Hill Education") == "McGraw-Hill Education"

    def test_title_cases_lowercase_unknown_companies(self) -> None:
        """All-lowercase unknown company names should be formatted in clean Title Case."""
        assert normalize_company("quantum leap technologies") == "Quantum Leap"
        assert normalize_company("deep horizon software") == "Deep Horizon"
        assert normalize_company("blue river labs") == "Blue River"

    def test_preserves_short_acronyms(self) -> None:
        """Short uppercase company acronyms (<=4 chars) should be preserved."""
        assert normalize_company("IBM Corp.") == "IBM"
        assert normalize_company("AWS") == "AWS"
        assert normalize_company("AMD Technologies") == "AMD"
        assert normalize_company("HP Inc.") == "HP"

    def test_title_cases_long_all_caps_strings(self) -> None:
        """Long all-caps strings (>4 chars) should be converted to clean Title Case."""
        assert normalize_company("VALLEY DYNAMICS CORP") == "Valley Dynamics"
        assert normalize_company("ENTERPRISE HOLDINGS LLC") == "Enterprise"


# ==============================================================================
# 4. Company Normalizer Edge Cases
# ==============================================================================


class TestCompanyEdgeCases:
    """Verifies None, empty, whitespace, and fallback parameter behaviors."""

    def test_none_input_returns_empty_or_default(self) -> None:
        assert normalize_company(None) == ""
        assert normalize_company(None, default="Unknown") == "Unknown"

    def test_empty_string_returns_empty_or_default(self) -> None:
        assert normalize_company("") == ""
        assert normalize_company("   ") == ""
        assert normalize_company("\t \n ", default="Unknown") == "Unknown"

    def test_punctuation_only_input_returns_empty(self) -> None:
        assert normalize_company("---") == ""
        assert normalize_company("...", default="Unknown") == "Unknown"
        assert normalize_company(",,,") == ""


# ==============================================================================
# 5. Location Parsing for US Cities & States
# ==============================================================================


class TestLocationUSParsing:
    """Verifies US city and state resolution across full names and abbreviations."""

    def test_standard_city_st_pattern(self) -> None:
        """Standard 'City, ST' formats must parse city, 2-letter state code, and country='US'."""
        loc = normalize_location("San Francisco, CA")
        assert loc["city"] == "San Francisco"
        assert loc["state"] == "CA"
        assert loc["country"] == "US"
        assert loc["is_remote"] is False
        assert loc["raw"] == "San Francisco, CA"

        loc_austin = normalize_location("Austin, TX")
        assert loc_austin["city"] == "Austin"
        assert loc_austin["state"] == "TX"
        assert loc_austin["country"] == "US"

        loc_nyc = normalize_location("New York, NY")
        assert loc_nyc["city"] == "New York"
        assert loc_nyc["state"] == "NY"
        assert loc_nyc["country"] == "US"

    def test_full_state_name_mapping(self) -> None:
        """'City, State Full Name' must resolve the state to its 2-letter postal code."""
        loc = normalize_location("San Francisco, California")
        assert loc["city"] == "San Francisco"
        assert loc["state"] == "CA"
        assert loc["country"] == "US"

        loc_chi = normalize_location("Chicago, Illinois")
        assert loc_chi["city"] == "Chicago"
        assert loc_chi["state"] == "IL"
        assert loc_chi["country"] == "US"

        loc_sea = normalize_location("Seattle, Washington")
        assert loc_sea["city"] == "Seattle"
        assert loc_sea["state"] == "WA"
        assert loc_sea["country"] == "US"

    def test_city_state_country_three_parts(self) -> None:
        """'City, State, Country' formats must resolve city, state code, and country='US'."""
        loc = normalize_location("San Francisco, California, United States")
        assert loc["city"] == "San Francisco"
        assert loc["state"] == "CA"
        assert loc["country"] == "US"
        assert loc["is_remote"] is False

        loc_tx = normalize_location("Austin, TX, USA")
        assert loc_tx["city"] == "Austin"
        assert loc_tx["state"] == "TX"
        assert loc_tx["country"] == "US"

        loc_wa = normalize_location("Seattle, Washington, US")
        assert loc_wa["city"] == "Seattle"
        assert loc_wa["state"] == "WA"
        assert loc_wa["country"] == "US"

    def test_standalone_known_cities_deduce_state(self) -> None:
        """Prominent tech hubs without state qualifiers must deduce state and country."""
        assert normalize_location("San Francisco")["state"] == "CA"
        assert normalize_location("New York City")["state"] == "NY"
        assert normalize_location("New York City")["city"] == "New York"
        assert normalize_location("NYC")["city"] == "New York"
        assert normalize_location("NYC")["state"] == "NY"
        assert normalize_location("SF")["city"] == "San Francisco"
        assert normalize_location("Seattle")["state"] == "WA"
        assert normalize_location("Austin")["state"] == "TX"
        assert normalize_location("Chicago")["state"] == "IL"
        assert normalize_location("Boston")["state"] == "MA"
        assert normalize_location("Los Angeles")["state"] == "CA"

    def test_city_st_without_comma(self) -> None:
        """Regex parses 'City ST' when comma delimiter is omitted."""
        loc = normalize_location("San Francisco CA")
        assert loc["city"] == "San Francisco"
        assert loc["state"] == "CA"
        assert loc["country"] == "US"

        loc_atx = normalize_location("Austin TX")
        assert loc_atx["city"] == "Austin"
        assert loc_atx["state"] == "TX"
        assert loc_atx["country"] == "US"

    def test_standalone_states(self) -> None:
        """Standalone state names or codes return state with city=None."""
        loc_ca = normalize_location("California")
        assert loc_ca["city"] is None
        assert loc_ca["state"] == "CA"
        assert loc_ca["country"] == "US"

        loc_tx = normalize_location("TX")
        assert loc_tx["city"] is None
        assert loc_tx["state"] == "TX"
        assert loc_tx["country"] == "US"

    def test_washington_dc_variations(self) -> None:
        """District of Columbia and Washington DC variations map to DC."""
        for dc_str in [
            "Washington, DC",
            "Washington, District of Columbia",
            "Washington D.C.",
            "Washington DC",
            "District of Columbia",
        ]:
            loc = normalize_location(dc_str)
            assert loc["state"] == "DC"
            assert loc["country"] == "US"

    @pytest.mark.parametrize(
        ("state_name", "postal_code"),
        [
            ("Alabama", "AL"),
            ("Alaska", "AK"),
            ("Arizona", "AZ"),
            ("Arkansas", "AR"),
            ("California", "CA"),
            ("Colorado", "CO"),
            ("Connecticut", "CT"),
            ("Delaware", "DE"),
            ("Florida", "FL"),
            ("Georgia", "GA"),
            ("Hawaii", "HI"),
            ("Idaho", "ID"),
            ("Illinois", "IL"),
            ("Indiana", "IN"),
            ("Iowa", "IA"),
            ("Kansas", "KS"),
            ("Kentucky", "KY"),
            ("Louisiana", "LA"),
            ("Maine", "ME"),
            ("Maryland", "MD"),
            ("Massachusetts", "MA"),
            ("Michigan", "MI"),
            ("Minnesota", "MN"),
            ("Mississippi", "MS"),
            ("Missouri", "MO"),
            ("Montana", "MT"),
            ("Nebraska", "NE"),
            ("Nevada", "NV"),
            ("New Hampshire", "NH"),
            ("New Jersey", "NJ"),
            ("New Mexico", "NM"),
            ("New York", "NY"),
            ("North Carolina", "NC"),
            ("North Dakota", "ND"),
            ("Ohio", "OH"),
            ("Oklahoma", "OK"),
            ("Oregon", "OR"),
            ("Pennsylvania", "PA"),
            ("Rhode Island", "RI"),
            ("South Carolina", "SC"),
            ("South Dakota", "SD"),
            ("Tennessee", "TN"),
            ("Texas", "TX"),
            ("Utah", "UT"),
            ("Vermont", "VT"),
            ("Virginia", "VA"),
            ("Washington", "WA"),
            ("West Virginia", "WV"),
            ("Wisconsin", "WI"),
            ("Wyoming", "WY"),
        ],
    )
    def test_all_50_us_states_lookup(self, state_name: str, postal_code: str) -> None:
        """Every US state full name must map to its 2-letter postal abbreviation."""
        loc = normalize_location(f"Capital, {state_name}")
        assert loc["state"] == postal_code
        assert loc["country"] == "US"


# ==============================================================================
# 6. Remote Flag Detection Across Diverse Phrasing
# ==============================================================================


class TestLocationRemoteDetection:
    """Verifies that remote keywords are detected and extracted cleanly."""

    @pytest.mark.parametrize(
        "remote_kw",
        [
            "remote",
            "us-remote",
            "remote - us",
            "work from anywhere",
            "anywhere in us",
            "hybrid",
        ],
    )
    def test_mandated_remote_keywords_detected(self, remote_kw: str) -> None:
        """Mandated remote keywords must set is_remote=True."""
        loc = normalize_location(remote_kw)
        assert loc["is_remote"] is True
        assert loc["city"] is None
        assert loc["state"] is None

    def test_remote_us_variations_set_country_us(self) -> None:
        """Remote with US specifications must set country='US'."""
        for s in ["US-Remote", "remote - us", "Anywhere in US", "Remote, United States"]:
            loc = normalize_location(s)
            assert loc["is_remote"] is True
            assert loc["country"] == "US"
            assert loc["city"] is None
            assert loc["state"] is None

    def test_remote_combined_with_city_and_state(self) -> None:
        """Remote combined with geographic location must extract both location and remote flag."""
        # "New York City / Remote" (Explicitly mandated in objectives)
        loc_nyc = normalize_location("New York City / Remote")
        assert loc_nyc["city"] == "New York"
        assert loc_nyc["state"] == "NY"
        assert loc_nyc["country"] == "US"
        assert loc_nyc["is_remote"] is True

        # "San Francisco, CA (Hybrid)"
        loc_sf = normalize_location("San Francisco, CA (Hybrid)")
        assert loc_sf["city"] == "San Francisco"
        assert loc_sf["state"] == "CA"
        assert loc_sf["country"] == "US"
        assert loc_sf["is_remote"] is True

        # "San Francisco, CA (Remote)"
        loc_sf_rem = normalize_location("San Francisco, CA (Remote)")
        assert loc_sf_rem["city"] == "San Francisco"
        assert loc_sf_rem["state"] == "CA"
        assert loc_sf_rem["country"] == "US"
        assert loc_sf_rem["is_remote"] is True

        # "Austin, TX - Remote"
        loc_atx = normalize_location("Austin, TX - Remote")
        assert loc_atx["city"] == "Austin"
        assert loc_atx["state"] == "TX"
        assert loc_atx["country"] == "US"
        assert loc_atx["is_remote"] is True

        # "Remote - Seattle, WA"
        loc_sea = normalize_location("Remote - Seattle, WA")
        assert loc_sea["city"] == "Seattle"
        assert loc_sea["state"] == "WA"
        assert loc_sea["country"] == "US"
        assert loc_sea["is_remote"] is True

        # "Hybrid - Chicago, IL"
        loc_chi = normalize_location("Hybrid - Chicago, IL")
        assert loc_chi["city"] == "Chicago"
        assert loc_chi["state"] == "IL"
        assert loc_chi["country"] == "US"
        assert loc_chi["is_remote"] is True

    def test_onsite_location_is_not_remote(self) -> None:
        """Explicitly onsite locations must have is_remote=False."""
        loc = normalize_location("San Francisco, CA (Onsite)")
        assert loc["city"] == "San Francisco"
        assert loc["state"] == "CA"
        assert loc["country"] == "US"
        assert loc["is_remote"] is False


# ==============================================================================
# 7. International Locations
# ==============================================================================


class TestLocationInternational:
    """Verifies parsing of international tech hubs and countries."""

    @pytest.mark.parametrize(
        ("location_str", "expected_city", "expected_country"),
        [
            ("London, United Kingdom", "London", "UK"),
            ("London, UK", "London", "UK"),
            ("Berlin, Germany", "Berlin", "DE"),
            ("Paris, France", "Paris", "FR"),
            ("Toronto, Canada", "Toronto", "CA"),
            ("Tokyo, Japan", "Tokyo", "JP"),
            ("Sydney, Australia", "Sydney", "AU"),
            ("Dublin, Ireland", "Dublin", "IE"),
            ("Amsterdam, Netherlands", "Amsterdam", "NL"),
            ("Singapore", "Singapore", "SG"),
        ],
    )
    def test_international_locations(
        self, location_str: str, expected_city: str, expected_country: str
    ) -> None:
        """International cities map to standardized country codes with state=None."""
        loc = normalize_location(location_str)
        assert loc["city"] == expected_city
        assert loc["state"] is None
        assert loc["country"] == expected_country
        assert loc["is_remote"] is False


# ==============================================================================
# 8. Location Normalizer Edge Cases
# ==============================================================================


class TestLocationEdgeCases:
    """Verifies edge cases including None, empty strings, ATS artifacts, and zip codes."""

    def test_none_input(self) -> None:
        loc = normalize_location(None)
        assert loc == {
            "city": None,
            "state": None,
            "country": "",
            "is_remote": False,
            "raw": "",
        }

    def test_empty_and_whitespace_input(self) -> None:
        loc = normalize_location("   \n\t  ")
        assert loc["city"] is None
        assert loc["state"] is None
        assert loc["country"] == ""
        assert loc["is_remote"] is False
        assert loc["raw"] == "   \n\t  "

    def test_greenhouse_us_prefix_artifact(self) -> None:
        """Greenhouse ATS prefixes (e.g. 'US-San Francisco') parse cleanly."""
        loc = normalize_location("US-San Francisco")
        assert loc["city"] == "San Francisco"
        assert loc["state"] == "CA"
        assert loc["country"] == "US"
        assert loc["is_remote"] is False

        loc_nyc = normalize_location("US-New York")
        assert loc_nyc["city"] == "New York"
        assert loc_nyc["state"] == "NY"
        assert loc_nyc["country"] == "US"

    def test_strips_zip_codes_cleanly(self) -> None:
        """Zip codes appended to state or as separate segments must be stripped."""
        loc = normalize_location("San Francisco, CA 94105")
        assert loc["city"] == "San Francisco"
        assert loc["state"] == "CA"
        assert loc["country"] == "US"

        loc_nyc = normalize_location("New York, NY 10001-1234")
        assert loc_nyc["city"] == "New York"
        assert loc_nyc["state"] == "NY"
        assert loc_nyc["country"] == "US"

        loc_part = normalize_location("Austin, TX, 78701")
        assert loc_part["city"] == "Austin"
        assert loc_part["state"] == "TX"
        assert loc_part["country"] == "US"

    def test_multi_location_semicolon_delimited(self) -> None:
        """When multi-location listings appear, primary location is extracted cleanly."""
        loc = normalize_location("San Francisco, CA; Chicago, IL & New York, NY")
        assert loc["city"] == "San Francisco"
        assert loc["state"] == "CA"
        assert loc["country"] == "US"

    def test_weird_spacing_and_commas(self) -> None:
        """Irregular whitespace and commas do not break parsing."""
        loc = normalize_location("  San Francisco   ,   CA  ")
        assert loc["city"] == "San Francisco"
        assert loc["state"] == "CA"
        assert loc["country"] == "US"


# ==============================================================================
# 9. Class, Classmethod, and Standalone Helper APIs
# ==============================================================================


class TestNormalizerAPIs:
    """Verifies that all classes, classmethods, and functions operate equivalently."""

    def test_company_normalizer_calling_styles(self) -> None:
        raw = "Stripe, Inc."
        # 1. Instance method
        assert CompanyNormalizer().normalize_company(raw) == "Stripe"
        # 2. Class method
        assert CompanyNormalizer.normalize(raw) == "Stripe"
        # 3. Standalone helper
        assert normalize_company(raw) == "Stripe"

    def test_location_normalizer_calling_styles(self) -> None:
        raw = "San Francisco, CA"
        # 1. Instance method
        loc1 = LocationNormalizer().normalize_location(raw)
        # 2. Class method
        loc2 = LocationNormalizer.normalize(raw)
        # 3. Standalone helper
        loc3 = normalize_location(raw)

        assert loc1 == loc2 == loc3
        assert loc1["city"] == "San Francisco"
        assert loc1["state"] == "CA"
        assert loc1["country"] == "US"

    def test_custom_company_normalizer_configuration(self) -> None:
        """CompanyNormalizer supports custom alias additions."""
        custom_cn = CompanyNormalizer(aliases={"twig data": "Twig"})
        assert custom_cn.normalize_company("twig data inc.") == "Twig"
