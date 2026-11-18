"""Job Title Standardization, Seniority Disambiguation, and Specialization Parser.

This module provides high-precision parsing of job titles for deduplication,
clustering, and record linkage across multi-source ingestion feeds.

Key Capabilities:
1. Noise & Job Requisition Stripping:
   Removes requisition IDs, parentheticals, bracketed locations, remote tags,
   gender markers (e.g. f/m/d), and employment types (Contract, Full-Time).
2. Seniority Classification & Disambiguation:
   Categorizes roles into strict seniority tiers (junior, mid, senior, staff_plus)
   across Roman numerals (I, II, III, IV), prefixes, and suffixes.
3. Core Role Family Classification:
   Classifies job roles into canonical engineering families:
   - data_engineer
   - ml_engineer
   - analytics_engineer
   - platform_engineer
   - software_engineer
   - other
4. Team / Domain Specialization Extraction:
   Extracts team/domain specializations from hyphens, commas, or parentheticals
   (e.g., 'Core Infrastructure', 'Search & Ranking', 'Product Analytics').
5. Canonical Clean Title Generation:
   Standardizes titles into clean representations (e.g., 'Senior Data Engineer').
6. Seniority Compatibility Matrix:
   Evaluates whether two roles could be the exact same job opening, enforcing
   strict mismatch guards (Staff vs Senior, Junior vs Senior).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field


# ==============================================================================
# ParsedTitle Model
# ==============================================================================


class ParsedTitle(BaseModel):
    """Structured title decomposition model."""

    raw_title: str = Field(
        ...,
        description="Original unedited job title string as scraped or ingested",
    )
    clean_title: str = Field(
        ...,
        description="Canonical normalized role title (e.g. 'Senior Data Engineer')",
    )
    seniority: Optional[str] = Field(
        default=None,
        description="Seniority level: 'junior', 'mid', 'senior', 'staff_plus', or None",
    )
    core_role: str = Field(
        default="other",
        description=(
            "Core role family: 'data_engineer', 'ml_engineer', 'analytics_engineer', "
            "'platform_engineer', 'software_engineer', or 'other'"
        ),
    )
    team_specialization: Optional[str] = Field(
        default=None,
        description="Domain or team specialization (e.g. 'Core Infrastructure', 'Product Analytics')",
    )

    model_config = ConfigDict(
        use_enum_values=True,
        validate_assignment=True,
        extra="ignore",
    )

    def __getitem__(self, item: str) -> Any:
        """Dictionary-like subscript access for backwards-compatibility."""
        if item == "title":
            return self.clean_title
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)

    def get(self, item: str, default: Any = None) -> Any:
        """Dictionary-like get method for backwards-compatibility."""
        if item == "title":
            return self.clean_title
        if hasattr(self, item):
            val = getattr(self, item)
            return val if val is not None else default
        return default



# ==============================================================================
# TitleParser
# ==============================================================================


class TitleParser:
    """Parser and disambiguator for job titles in deduplication pipelines."""

    # --------------------------------------------------------------------------
    # Noise Regex Patterns
    # --------------------------------------------------------------------------

    # Job requisition IDs: e.g. (Req #1234), [REQ-5678], Req: 999, Job ID: 12345, (JR-1029)
    _REQ_ID_PATTERN = re.compile(
        r"""
        (?:[\(\[\{]\s*)?
        \b(?:req(?:uisition)?(?:\s*id)?|job\s*id|pos(?:ition)?\s*id|ref(?:\s*id)?|jr)[\s:\-#\-_]*(?:\#\s*)?[0-9][a-z0-9\-]*
        (?:\s*[\)\]\}])?
        """,
        re.IGNORECASE | re.VERBOSE,
    )



    # Diversity / gender tags: e.g. (f/m/d), (m/f/d), (d/f/m), (m/w/d), (all genders)
    _DIVERSITY_PATTERN = re.compile(
        r"""
        [\(\[\{]\s*
        (?:
            (?:[fmdw]\s*[/|\\]\s*){1,3}[fmdw]
            |
            all\s+genders?
            |
            he/him|she/her|they/them
        )
        \s*[\)\]\}]
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Remote policy and workplace tags: e.g. [Remote], (US Remote), 100% Remote, Remote - US
    _REMOTE_LOCATION_TAG_PATTERN = re.compile(
        r"""
        (?:
            [\(\[\{]\s*
            (?:
                remote(?:\s*[-–—,/|:]\s*[\w\s]+)?
                |
                (?:us|usa|uk|emea|apac|americas|latam|canada)\s*remote
                |
                wfh|work\s*from\s*home|hybrid|onsite|in[\s\-]office
                |
                us|usa|united\s*states|uk|emea|apac|canada
                |
                nyc|sf|nyc/sf|sf/nyc|bay\s*area|seattle|austin|chicago|london|berlin|boston|la|dc
                |
                [a-z\s]+,\s*[a-z]{2}  # City, ST (e.g. New York, NY)
            )
            \s*[\)\]\}]
            |
            \b(?:100%\s*)?(?:remote|wfh)(?:\s*[-–—]\s*(?:us|usa|anywhere|worldwide|americas))?\b
        )
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Employment commitment noise: e.g. (Full-Time), (Contract), Contract - 6 Months
    _EMPLOYMENT_TYPE_PATTERN = re.compile(
        r"""
        (?:[\(\[\{]\s*)?
        \b(?:
            full[\s\-]time|part[\s\-]time|contract(?:or)?|temp(?:orary)?
            |
            permanent|co[\s\-]op|direct[\s\-]hire|c2c|w2
        )\b
        (?:\s*[\)\]\}])?
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Security clearance / visa noise
    _CLEARANCE_VISA_PATTERN = re.compile(
        r"""
        (?:[\(\[\{]\s*)?
        \b(?:
            ts/sci|top\s*secret|secret\s*clearance|security\s*clearance
            |
            us\s*citizen(?:ship)?|visa\s*sponsorship(?:\s*available)?
        )\b
        (?:\s*[\)\]\}])?
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Standalone noise tokens that don't add semantic title value
    _GENERIC_NOISE_TOKENS = {
        "remote", "hybrid", "onsite", "us", "usa", "uk", "emea", "apac",
        "full-time", "fulltime", "part-time", "parttime", "contract", "contractor",
        "intern", "internship", "temporary", "permanent",
        "hiring", "urgent", "immediate", "opening", "opportunity",
    }

    # --------------------------------------------------------------------------
    # Seniority Regex Patterns
    # --------------------------------------------------------------------------

    # Staff+ tier: Staff, Principal, Distinguished, Fellow, Lead, Director, Head of, Architect
    _STAFF_PLUS_PATTERN = re.compile(
        r"""
        \b(
            distinguished
            |
            fellow
            |
            principal
            |
            staff
            |
            director(?:\s+of)?
            |
            head\s+of
            |
            vp(?:\s+of)?|vice\s+president
            |
            chief(?:\s+data|\s+software|\s+technology)?
            |
            lead(?:\s+data|\s+ml|\s+analytics|\s+platform|\s+software|\s+systems|\s+pipeline)?
            |
            architect
        )\b
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Senior tier: Senior, Sr., Sr, III, IV, Level 3/4/5
    _SENIOR_PATTERN = re.compile(
        r"""
        \b(
            senior
            |
            sr\.?
            |
            iii|iv
            |
            level\s*(?:3|4|5|iii|iv)
            |
            l[567]
        )\b
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Mid tier: II, Intermediate, Mid-level, Mid, Level 2
    _MID_PATTERN = re.compile(
        r"""
        \b(
            intermediate
            |
            mid[\s\-]level
            |
            mid
            |
            ii
            |
            level\s*(?:2|ii)
            |
            l4
        )\b
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Junior tier: Junior, Jr., Jr, Associate, Entry Level, Entry-Level, I, Intern, Internship
    _JUNIOR_PATTERN = re.compile(
        r"""
        \b(
            junior
            |
            jr\b\.?
            |
            associate
            |
            entry[\s\-]level
            |
            entry
            |
            intern(?:ship)?
            |
            co[\s\-]op
            |
            trainee
            |
            graduate
            |
            level\s*(?:1|i)
            |
            l3
        )\b
        |
        (?<=\bengineer\s)i\b
        |
        (?<=\bdeveloper\s)i\b
        |
        (?<=\bscientist\s)i\b
        |
        (?<=\banalyst\s)i\b
        |
        \bi(?=\s*$)
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # --------------------------------------------------------------------------
    # Core Role Family Patterns
    # --------------------------------------------------------------------------

    # ML Engineer Family
    _ROLE_ML_PATTERN = re.compile(
        r"""
        \b(
            machine\s+learning\s+engineer(?:ing)?
            |
            ml\s+engineer(?:ing)?
            |
            ai\s+engineer(?:ing)?
            |
            ai/ml\s+engineer(?:ing)?|ai\s*and\s*ml\s+engineer(?:ing)?
            |
            mlops\s+engineer(?:ing)?
            |
            deep\s+learning\s+engineer(?:ing)?
            |
            machine\s+learning\s+scientist(?:s)?|ml\s+scientist(?:s)?
            |
            computer\s+vision\s+engineer(?:ing)?
            |
            nlp\s+engineer(?:ing)?
            |
            ml\s+infra(?:structure)?\s+engineer(?:ing)?
            |
            ml\s+platform\s+engineer(?:ing)?
            |
            generative\s+ai\s+engineer(?:ing)?|genai\s+engineer(?:ing)?
        )\b
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Analytics Engineer Family
    _ROLE_ANALYTICS_PATTERN = re.compile(
        r"""
        \b(
            analytics\s+engineer(?:ing)?
            |
            bi\s+engineer(?:ing)?
            |
            business\s+intelligence\s+engineer(?:ing)?
            |
            bi\s+developer(?:s)?
            |
            business\s+intelligence\s+developer(?:s)?
            |
            data\s+analytics\s+engineer(?:ing)?
        )\b
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Platform Engineer Family
    _ROLE_PLATFORM_PATTERN = re.compile(
        r"""
        \b(
            data\s+platform\s+engineer(?:ing)?
            |
            platform\s+engineer(?:ing)?
            |
            infrastructure\s+engineer(?:ing)?|infra\s+engineer(?:ing)?
            |
            data\s+infra(?:structure)?\s+engineer(?:ing)?
            |
            cloud\s+platform\s+engineer(?:ing)?
            |
            platform\s+systems\s+engineer(?:ing)?
        )\b
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Data Engineer Family
    _ROLE_DATA_PATTERN = re.compile(
        r"""
        \b(
            big\s+data\s+engineer(?:ing)?
            |
            data\s+pipeline\s+engineer(?:ing)?
            |
            data\s+warehous(?:e|ing)\s+engineer(?:ing)?
            |
            data\s+integration\s+engineer(?:ing)?
            |
            data\s+engineer(?:ing)?
            |
            etl\s+developer(?:s)?|etl\s+engineer(?:ing)?
            |
            database\s+engineer(?:ing)?
        )\b
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Software Engineer Family
    _ROLE_SWE_PATTERN = re.compile(
        r"""
        \b(
            software\s+(?:development\s+)?engineer(?:ing)?
            |
            software\s+developer(?:s)?
            |
            swe
            |
            backend\s+engineer(?:ing)?|backend\s+developer(?:s)?
            |
            systems\s+engineer(?:ing)?
            |
            full\s*stack\s+engineer(?:ing)?|full\s*stack\s+developer(?:s)?
        )\b
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Specialization extraction separators (hyphen, en-dash, em-dash, pipe, comma, slash)
    _SPECIALIZATION_SPLIT_PATTERN = re.compile(r"\s*[-–—|/,]\s*")

    # Parenthetical extractor
    _PARENTHETICAL_PATTERN = re.compile(r"[\(\[\{]([^\(\)\[\]\{\}]+)[\)\]\}]")

    def __init__(self, default_unadorned_as_mid: bool = False):
        """Initializes the TitleParser.

        Args:
            default_unadorned_as_mid: If True, sets seniority to 'mid' for standard
                unadorned titles without explicit seniority keywords. If False (default),
                unadorned titles retain seniority=None to reflect unspecified seniority.
        """
        self.default_unadorned_as_mid = default_unadorned_as_mid

    # ==========================================================================
    # Public API
    # ==========================================================================

    def parse_title(
        self,
        title: Optional[str],
        unadorned_as_mid: Optional[bool] = None,
    ) -> ParsedTitle:
        """Parses, disambiguates, and standardizes a job title.

        Args:
            title: Raw input job title (e.g. 'Senior Data Engineer - Infrastructure [Remote]').
            unadorned_as_mid: Override for default_unadorned_as_mid. If None, uses
                instance default.

        Returns:
            ParsedTitle containing raw_title, clean_title, seniority, core_role,
            and team_specialization.
        """
        use_mid_default = (
            unadorned_as_mid
            if unadorned_as_mid is not None
            else self.default_unadorned_as_mid
        )

        if not title or not isinstance(title, str) or not title.strip():
            return ParsedTitle(
                raw_title=title or "",
                clean_title="",
                seniority=None,
                core_role="other",
                team_specialization=None,
            )

        raw = title.strip()

        # Step 1: Extract candidate team specialization from parentheticals or separators
        cleaned_text, extracted_spec = self._extract_specialization_and_clean_noise(raw)

        # Step 2: Extract Seniority level
        seniority, raw_seniority_keyword = self._extract_seniority(cleaned_text)

        # Step 3: Classify Core Role Family
        core_role = self._classify_core_role(cleaned_text, raw, extracted_spec)

        # Step 4: If unadorned and use_mid_default is True, assign 'mid'
        if seniority is None and use_mid_default and core_role != "other":
            seniority = "mid"

        # Step 5: Generate canonical clean_title
        clean_title = self._generate_clean_title(
            cleaned_text=cleaned_text,
            core_role=core_role,
            seniority=seniority,
            raw_seniority_keyword=raw_seniority_keyword,
        )

        return ParsedTitle(
            raw_title=raw,
            clean_title=clean_title,
            seniority=seniority,
            core_role=core_role,
            team_specialization=extracted_spec,
        )

    def parse(
        self,
        title: Optional[str],
        unadorned_as_mid: Optional[bool] = None,
    ) -> ParsedTitle:
        """Alias for parse_title for polymorphic interoperability."""
        return self.parse_title(title, unadorned_as_mid=unadorned_as_mid)


    @classmethod
    def are_seniorities_compatible(
        cls,
        s1: Optional[Any],
        s2: Optional[Any],
    ) -> bool:
        """Evaluates whether two roles could be the exact same job opening.

        Enforces strict disambiguation rules to avoid false merges across
        distinct requisition levels.

        Rules:
        - Identical seniorities -> True (e.g., senior == senior, None == None).
        - Explicit mismatch guards (NEVER merge):
          - junior vs senior -> False
          - junior vs staff_plus -> False
          - mid vs staff_plus -> False
          - senior vs staff_plus -> False (Staff and Senior are distinct requisition levels)
          - junior vs mid -> False
          - mid vs senior -> False (Mid/L4 and Senior/L5 are distinct levels)
        - Unadorned / None tolerance:
          - One is None (unadorned) and the other is 'mid' or 'senior' -> True
            (tolerant of scraper omissions where 'Senior' or 'Mid' was dropped).
          - One is None and the other is 'junior' or 'staff_plus' -> False
            (Staff and Junior are high-stakes and never merged from unadorned titles).

        Args:
            s1: Seniority string, enum, or None from role 1.
            s2: Seniority string, enum, or None from role 2.

        Returns:
            True if seniorities are compatible under fuzzy deduplication tolerance.
        """
        s1_norm = cls._normalize_seniority_token(s1)
        s2_norm = cls._normalize_seniority_token(s2)

        # 1. Identical seniorities (including None == None, mid == mid, etc.)
        if s1_norm == s2_norm:
            return True

        # 2. One is None (unadorned)
        if s1_norm is None or s2_norm is None:
            non_none = s1_norm if s2_norm is None else s2_norm
            # Compatible under fuzzy tolerance with 'mid' or 'senior'
            if non_none in ("mid", "senior"):
                return True
            # Incompatible with 'junior', 'staff_plus', or unknown
            return False

        # 3. Explicit mismatch guards when both are non-None and different
        pair = {s1_norm, s2_norm}

        if pair == {"junior", "senior"}:
            return False
        if pair == {"junior", "staff_plus"}:
            return False
        if pair == {"mid", "staff_plus"}:
            return False
        if pair == {"senior", "staff_plus"}:
            return False
        if pair == {"junior", "mid"}:
            return False
        if pair == {"mid", "senior"}:
            return False

        return False

    # ==========================================================================
    # Internal Extraction Helpers
    # ==========================================================================

    @classmethod
    def _normalize_seniority_token(cls, val: Any) -> Optional[str]:
        """Normalizes a seniority token to canonical tier or None."""
        if val is None:
            return None
        if hasattr(val, "value"):
            val = val.value
        if not isinstance(val, str):
            return None

        token = val.strip().lower()
        if not token or token == "none" or token == "null":
            return None

        # Check Staff+
        if token in ("staff_plus", "staff", "principal", "distinguished", "fellow", "lead", "director", "head of", "architect"):
            return "staff_plus"
        # Check Senior
        if token in ("senior", "sr", "sr.", "iii", "iv", "level 3", "level 4", "level 5"):
            return "senior"
        # Check Mid
        if token in ("mid", "intermediate", "ii", "level 2"):
            return "mid"
        # Check Junior
        if token in ("junior", "jr", "jr.", "associate", "entry level", "entry-level", "entry", "i", "intern", "internship"):
            return "junior"

        return token

    def _strip_noise(self, text: str) -> str:
        """Strips job requisition IDs, diversity markers, remote tags, and noise."""
        s = text
        s = self._REQ_ID_PATTERN.sub(" ", s)
        s = self._DIVERSITY_PATTERN.sub(" ", s)
        s = self._REMOTE_LOCATION_TAG_PATTERN.sub(" ", s)
        s = self._CLEARANCE_VISA_PATTERN.sub(" ", s)

        # Strip standalone noise parentheticals like (Contract), (Full-Time)
        s = self._EMPLOYMENT_TYPE_PATTERN.sub(" ", s)

        # Normalize multiple spaces and dangling punctuation
        s = re.sub(r"\s+", " ", s)
        s = re.sub(r"^[\s\-–—|/,\.\:\;]+|[\s\-–—|/,\.\:\;]+$", "", s)
        return s.strip()

    def _is_noise_string(self, text: str) -> bool:
        """Checks if a string fragment is purely noise rather than specialization."""
        t = text.strip()
        if not t:
            return True
        lower = t.lower()

        # Check wrapped with parentheses
        wrapped = f"({t})"
        if self._REQ_ID_PATTERN.search(t) or self._REQ_ID_PATTERN.search(wrapped):
            return True
        if self._DIVERSITY_PATTERN.search(t) or self._DIVERSITY_PATTERN.search(wrapped):
            return True
        if self._REMOTE_LOCATION_TAG_PATTERN.search(t) or self._REMOTE_LOCATION_TAG_PATTERN.search(wrapped):
            return True
        if self._EMPLOYMENT_TYPE_PATTERN.search(t) or self._EMPLOYMENT_TYPE_PATTERN.search(wrapped):
            return True
        if self._CLEARANCE_VISA_PATTERN.search(t) or self._CLEARANCE_VISA_PATTERN.search(wrapped):
            return True

        # Check diversity regex directly
        if re.search(r"^(?:[fmdw]\s*[/|\\]\s*){1,3}[fmdw]$", lower):
            return True
        if re.search(r"^(?:all\s+genders?|he/him|she/her|they/them)$", lower):
            return True

        # Check location / remote / country / commitment tokens directly
        if lower in (
            "remote", "wfh", "hybrid", "onsite", "in-office",
            "us", "usa", "uk", "emea", "apac", "americas", "latam", "canada",
            "full-time", "full time", "part-time", "part time", "contract", "contractor",
            "c2c", "w2", "permanent", "temporary", "temp", "intern", "internship",
            "nyc", "sf", "nyc/sf", "sf/nyc", "bay area", "seattle", "austin", "chicago",
            "london", "berlin", "boston", "la", "dc", "us remote", "remote us",
            "100% remote", "work from home", "united states",
        ):
            return True

        # Check generic tokens
        clean_words = set(re.findall(r"\b[a-z0-9\-]+\b", lower))
        if clean_words and clean_words.issubset(self._GENERIC_NOISE_TOKENS):
            return True

        # Pure numbers or years (e.g. 2024, 12345)
        if re.match(r"^\d+$", t):
            return True

        return False

    def _is_seniority_string(self, text: str) -> bool:
        """Checks if a string fragment is purely a seniority indicator."""
        t = text.strip().lower()
        if self._STAFF_PLUS_PATTERN.fullmatch(t):
            return True
        if self._SENIOR_PATTERN.fullmatch(t):
            return True
        if self._MID_PATTERN.fullmatch(t):
            return True
        if self._JUNIOR_PATTERN.fullmatch(t):
            return True
        return False

    def _extract_specialization_and_clean_noise(
        self,
        raw: str,
    ) -> Tuple[str, Optional[str]]:
        """Extracts candidate team specialization and returns cleaned base text."""
        extracted_spec: Optional[str] = None
        working = raw

        # Step 1: Specifically strip known noise patterns from raw text
        working = self._REQ_ID_PATTERN.sub(" ", working)
        working = self._DIVERSITY_PATTERN.sub(" ", working)
        working = self._REMOTE_LOCATION_TAG_PATTERN.sub(" ", working)
        working = self._CLEARANCE_VISA_PATTERN.sub(" ", working)

        # Step 2: Look for parenthetical / bracketed specialization e.g. (Search & Ranking)
        matches = list(self._PARENTHETICAL_PATTERN.finditer(working))
        for m in reversed(matches):
            content = m.group(1).strip()
            # If it's noise, strip it
            if self._is_noise_string(content):
                working = working[:m.start()] + " " + working[m.end():]
                continue
            # If it's seniority keyword (e.g. (Senior), (Lead)), leave for seniority parser
            if self._is_seniority_string(content):
                continue

            # Otherwise, it's a team / domain specialization!
            if not extracted_spec:
                extracted_spec = content
            working = working[:m.start()] + " " + working[m.end():]

        # Step 3: Strip remaining noise patterns from the working text
        cleaned = self._strip_noise(working)

        # Step 4: If no parenthetical specialization was found, look for suffix separators
        if not extracted_spec:
            # Check for delimiters: - , | /
            parts = [p.strip() for p in self._SPECIALIZATION_SPLIT_PATTERN.split(cleaned) if p.strip()]
            if len(parts) >= 2:
                # The first part is usually the title, subsequent parts may be specialization or seniority
                first_part = parts[0]
                potential_spec_parts = parts[1:]

                spec_candidates = []
                seniority_tokens = []
                for p in potential_spec_parts:
                    p_clean = re.sub(r"^[^a-zA-Z0-9\(\)]+|[^a-zA-Z0-9\(\)]+$", "", p).strip()
                    if not p_clean or not re.search(r"[a-zA-Z0-9]", p_clean):
                        continue
                    if self._is_noise_string(p_clean):
                        continue
                    if self._is_seniority_string(p_clean):
                        seniority_tokens.append(p_clean)
                        continue
                    spec_candidates.append(p_clean)

                if spec_candidates:
                    extracted_spec = " - ".join(spec_candidates)

                # Keep any seniority suffix with the cleaned text for seniority extraction
                if seniority_tokens:
                    cleaned = f"{first_part} {' '.join(seniority_tokens)}"
                else:
                    cleaned = first_part

        # Clean trailing and leading punctuation from extracted specialization
        if extracted_spec:
            extracted_spec = re.sub(r"^[^a-zA-Z0-9]+|[^a-zA-Z0-9]+$", "", extracted_spec).strip()
            if not extracted_spec or self._is_noise_string(extracted_spec) or self._is_seniority_string(extracted_spec):
                extracted_spec = None

        cleaned = self._strip_noise(cleaned)
        return cleaned, extracted_spec

    def _extract_seniority(
        self,
        cleaned_text: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Extracts seniority level ('junior', 'mid', 'senior', 'staff_plus', or None).

        Returns:
            Tuple of (seniority_tier, raw_keyword).
        """
        combined = cleaned_text.lower()

        # Priority 1: Staff+ tier
        m_staff = self._STAFF_PLUS_PATTERN.search(combined)
        if m_staff:
            return "staff_plus", m_staff.group(1).lower()

        # Priority 2: Senior tier
        m_senior = self._SENIOR_PATTERN.search(combined)
        if m_senior:
            return "senior", m_senior.group(1).lower()

        # Priority 3: Junior tier
        # (Evaluated before mid to ensure 'associate' or 'intern' or 'i' wins)
        m_junior = self._JUNIOR_PATTERN.search(combined)
        if m_junior:
            matched_str = m_junior.group(1) or m_junior.group(0)
            return "junior", matched_str.lower()

        # Priority 4: Mid tier (explicit: II, Intermediate, Mid)
        m_mid = self._MID_PATTERN.search(combined)
        if m_mid:
            return "mid", m_mid.group(1).lower()

        return None, None

    def _classify_core_role(
        self,
        cleaned_text: str,
        raw_text: str,
        specialization: Optional[str],
    ) -> str:
        """Classifies the job role into an engineering family."""
        spec_text = f" {specialization} " if specialization else ""
        text = f"{cleaned_text} {raw_text}{spec_text}".lower()

        # 1. ML Engineer Family
        if self._ROLE_ML_PATTERN.search(text):
            return "ml_engineer"

        # 2. Analytics Engineer Family
        if self._ROLE_ANALYTICS_PATTERN.search(text):
            return "analytics_engineer"

        # 3. Platform Engineer Family
        if self._ROLE_PLATFORM_PATTERN.search(text):
            return "platform_engineer"

        # 4. Data Engineer Family
        if self._ROLE_DATA_PATTERN.search(text):
            return "data_engineer"

        # 5. Reverse formulations: Software Engineer (Data / ML / Platform)
        if self._ROLE_SWE_PATTERN.search(text):
            if any(w in text for w in ("machine learning", " ml ", "ai/ml", "deep learning", "genai")):
                return "ml_engineer"
            if any(w in text for w in ("analytics", "bi ", "business intelligence")):
                return "analytics_engineer"
            if any(w in text for w in ("platform", "infra", "infrastructure")):
                return "platform_engineer"
            if any(w in text for w in ("data", "big data", "etl", "pipeline", "warehouse")):
                return "data_engineer"
            return "software_engineer"

        return "other"

    def _generate_clean_title(
        self,
        cleaned_text: str,
        core_role: str,
        seniority: Optional[str],
        raw_seniority_keyword: Optional[str],
    ) -> str:
        """Generates the canonical clean title representation."""
        # Mapping canonical base titles per role
        role_base_map = {
            "data_engineer": "Data Engineer",
            "ml_engineer": "Machine Learning Engineer",
            "analytics_engineer": "Analytics Engineer",
            "platform_engineer": "Platform Engineer",
            "software_engineer": "Software Engineer",
        }

        # If role is 'other', retain the cleaned string title-cased
        if core_role == "other":
            cleaned = re.sub(r"\s+", " ", cleaned_text).strip()
            return self._title_case(cleaned) if cleaned else ""

        base_role_title = role_base_map.get(core_role, "Data Engineer")

        # Specific role variations preservation
        cleaned_lower = cleaned_text.lower()
        if core_role == "platform_engineer" and "data platform" in cleaned_lower:
            base_role_title = "Data Platform Engineer"
        elif core_role == "data_engineer" and "etl" in cleaned_lower:
            base_role_title = "ETL Developer"
        elif core_role == "analytics_engineer" and ("business intelligence" in cleaned_lower or "bi " in cleaned_lower):
            base_role_title = "Business Intelligence Engineer"

        # Format with Seniority
        if seniority == "staff_plus":
            kw = (raw_seniority_keyword or "").lower()
            if "principal" in kw:
                return f"Principal {base_role_title}"
            elif "distinguished" in kw:
                return f"Distinguished {base_role_title}"
            elif "fellow" in kw:
                return f"Fellow {base_role_title}"
            elif "lead" in kw:
                return f"Lead {base_role_title}"
            elif "architect" in kw:
                if core_role == "data_engineer":
                    return "Data Architect"
                elif core_role == "platform_engineer":
                    return "Platform Architect"
                return f"Principal {base_role_title}"
            elif "director" in kw:
                return f"Director of {base_role_title}ing" if not base_role_title.endswith("ing") else f"Director of {base_role_title}"
            elif "head of" in kw:
                return f"Head of {base_role_title}ing" if not base_role_title.endswith("ing") else f"Head of {base_role_title}"
            return f"Staff {base_role_title}"

        elif seniority == "senior":
            kw = (raw_seniority_keyword or "").lower()
            if kw in ("iii", "level 3", "level iii"):
                return f"{base_role_title} III"
            elif kw in ("iv", "level 4", "level iv"):
                return f"{base_role_title} IV"
            return f"Senior {base_role_title}"

        elif seniority == "mid":
            kw = (raw_seniority_keyword or "").lower()
            if kw in ("ii", "level 2", "level ii"):
                return f"{base_role_title} II"
            return base_role_title

        elif seniority == "junior":
            kw = (raw_seniority_keyword or "").lower()
            if "associate" in kw:
                return f"Associate {base_role_title}"
            elif "intern" in kw:
                return f"{base_role_title} Intern"
            elif kw in ("i", "level 1", "level i"):
                return f"{base_role_title} I"
            return f"Junior {base_role_title}"

        # Unadorned / None seniority
        return base_role_title

    @staticmethod
    def _title_case(text: str) -> str:
        """Utility to title-case text while preserving standard acronyms."""
        acronyms = {"AI", "ML", "BI", "ETL", "NLP", "SWE", "US", "NYC", "SF", "AWS", "GCP"}
        words = text.split()
        capitalized = []
        for w in words:
            upper = w.upper()
            if upper in acronyms:
                capitalized.append(upper)
            else:
                capitalized.append(w.capitalize())
        return " ".join(capitalized)
