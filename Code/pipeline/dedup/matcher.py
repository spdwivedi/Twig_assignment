"""Fuzzy matching and similarity engine for job posting deduplication.

Provides:
- MatchResult: Pydantic model capturing match status, confidence score, match category,
  rejection reason, and step-by-step diagnostic details.
- JobPostingMatcher: High-precision similarity engine using RapidFuzz, domain-specific
  seniority guards, team specialization conflict detectors, location compatibility checks,
  and skills overlap boosting.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)


# ==============================================================================
# Match Result Model
# ==============================================================================


class MatchResult(BaseModel):
    """Result of comparing two job postings for entity resolution / deduplication."""

    model_config = ConfigDict(
        validate_assignment=True,
        extra="ignore",
    )

    is_match: bool = Field(..., description="Whether postings represent the same job opening")
    score: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        description="Confidence similarity score between 0.0 and 100.0",
    )
    match_type: str = Field(
        ...,
        description="'exact', 'fuzzy_high_confidence', 'url_match', or 'no_match'",
    )
    reason: str = Field(
        default="",
        description="Human-readable justification or rejection rationale",
    )
    details: Dict[str, Any] = Field(
        default_factory=dict,
        description="Diagnostic metadata and intermediate score calculations",
    )


# ==============================================================================
# Domain Dictionaries & Regular Expressions
# ==============================================================================

# Common corporate legal entity suffixes to strip during normalization
COMPANY_LEGAL_SUFFIXES: re.Pattern = re.compile(
    r"\b(?:inc|incorporated|llc|l\.l\.c\.|ltd|limited|corp|corporation|co|company|"
    r"technologies|tech|technology|labs|laboratory|group|holdings|services|solutions|"
    r"software|systems|enterprises|international)\b\.?",
    re.IGNORECASE,
)

# Acronyms and abbreviations expansion in job titles
TITLE_ABBREVIATIONS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bsr\.?\b", re.IGNORECASE), "senior"),
    (re.compile(r"\bjr\.?\b", re.IGNORECASE), "junior"),
    (re.compile(r"\bswe\b", re.IGNORECASE), "software engineer"),
    (re.compile(r"\bde\b", re.IGNORECASE), "data engineer"),
    (re.compile(r"\bds\b", re.IGNORECASE), "data scientist"),
    (re.compile(r"\bmle\b", re.IGNORECASE), "machine learning engineer"),
    (re.compile(r"\bml\b", re.IGNORECASE), "machine learning"),
    (re.compile(r"\bai\b", re.IGNORECASE), "artificial intelligence"),
    (re.compile(r"\bbi\b", re.IGNORECASE), "business intelligence"),
    (re.compile(r"\betl\b", re.IGNORECASE), "etl"),
    (re.compile(r"\bpe\b", re.IGNORECASE), "platform engineer"),
]

# Patterns to strip parenthetical location or policy metadata from titles
TITLE_LOCATION_PARENS: re.Pattern = re.compile(
    r"[\(\[\{]\s*(?:remote|onsite|on-site|hybrid|in-office|wfh|nyc|sf|us|usa|"
    r"new\s+york|san\s+francisco|austin|seattle|boston|chicago|london|united\s+states|"
    r"[a-z]{2}\s+only|[a-z]{2}\s*,\s*[a-z]{2})[^\)\]\}]*[\)\]\}]",
    re.IGNORECASE,
)

# Standardized seniority levels
SENIORITY_RANKS: Dict[str, int] = {
    "junior": 1,
    "mid": 2,
    "senior": 3,
    "staff_plus": 4,
}

# Seniority detection patterns from job titles
SENIORITY_PATTERNS: List[Tuple[str, re.Pattern]] = [
    (
        "staff_plus",
        re.compile(
            r"\b(staff|principal|distinguished|fellow|director|vp|vice\s+president|head\s+of)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "senior",
        re.compile(
            r"\b(senior|sr\.?|lead|iii|iv|level\s*3|level\s*4|l5|l6|expert)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "junior",
        re.compile(
            r"\b(junior|jr\.?|associate|entry|intern|internship|apprentice|i|level\s*1)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "mid",
        re.compile(
            r"\b(mid|intermediate|ii|level\s*2|l4)\b",
            re.IGNORECASE,
        ),
    ),
]

# Opposing team specialization clusters
SPECIALIZATION_CLUSTERS: Dict[str, Set[str]] = {
    "platform": {
        "platform",
        "infrastructure",
        "infra",
        "foundation",
        "foundations",
        "core",
        "systems",
        "frameworks",
        "developer productivity",
        "dev productivity",
        "storage",
        "compute",
    },
    "growth": {
        "growth",
        "marketing",
        "acquisition",
        "retention",
        "monetization",
        "commercial",
        "sales analytics",
        "gtm",
        "go-to-market",
    },
    "product": {
        "product",
        "consumer",
        "mobile",
        "web",
        "engagement",
        "user experience",
    },
    "analytics": {
        "analytics",
        "business intelligence",
        "bi",
        "reporting",
        "insights",
        "dashboarding",
    },
    "mlops": {
        "mlops",
        "ai platform",
        "model serving",
        "machine learning operations",
    },
    "security": {
        "security",
        "privacy",
        "governance",
        "compliance",
        "trust",
        "infosec",
    },
    "finance": {
        "finance",
        "fintech",
        "payments",
        "billing",
        "revenue",
        "accounting",
        "payroll",
    },
    "search": {
        "search",
        "discovery",
        "recommendation",
        "recsys",
        "ranking",
    },
    "risk": {
        "risk",
        "fraud",
        "trust and safety",
        "abuse",
    },
}

# Known city / metropolitan aliases for location compatibility
CITY_ALIASES: Dict[str, Set[str]] = {
    "new york": {"new york", "nyc", "new york city", "ny", "manhattan", "brooklyn"},
    "san francisco": {
        "san francisco",
        "sf",
        "bay area",
        "san francisco bay area",
        "palo alto",
        "menlo park",
        "mountain view",
        "sunnyvale",
        "san jose",
        "oakland",
    },
    "seattle": {"seattle", "bellevue", "redmond", "wa"},
    "austin": {"austin", "tx"},
    "chicago": {"chicago", "il"},
    "boston": {"boston", "cambridge", "ma"},
    "los angeles": {"los angeles", "la", "santa monica", "culver city", "ca"},
    "london": {"london", "uk", "greater london"},
}

# Tracking query parameters to strip when validating source URLs
TRACKING_QUERY_PARAMS: Set[str] = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gh_src",
    "ref",
    "source",
    "s",
    "lever-source",
    "lever-origin",
}


# ==============================================================================
# Helper Functions
# ==============================================================================


def _extract_field(posting: Any, *field_names: str, default: Any = None) -> Any:
    """Safely extracts a field from a Pydantic model, dictionary, or object."""
    if posting is None:
        return default

    for name in field_names:
        if isinstance(posting, dict):
            if name in posting and posting[name] is not None:
                return posting[name]
        else:
            val = getattr(posting, name, None)
            if val is not None:
                return val

    return default


def _extract_urls(posting: Any) -> List[str]:
    """Safely extracts all source URLs from a posting object or dict."""
    if posting is None:
        return []

    urls: List[str] = []
    candidates = []

    if isinstance(posting, dict):
        for key in ("source_url", "source_urls", "url", "urls", "link"):
            val = posting.get(key)
            if val is not None:
                candidates.append(val)
    else:
        for key in ("source_url", "source_urls", "url", "urls", "link"):
            val = getattr(posting, key, None)
            if val is not None:
                candidates.append(val)

    for item in candidates:
        if isinstance(item, str):
            s = item.strip()
            if s:
                urls.append(s)
        elif isinstance(item, (list, tuple, set)):
            for sub in item:
                if isinstance(sub, str) and sub.strip():
                    urls.append(sub.strip())

    return urls


def _clean_tracking_url(url: str) -> str:
    """Strips advertising / tracking query parameters while preserving unique job IDs."""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if not parsed.scheme or not parsed.netloc:
            return url.strip().rstrip("/").lower()

        query_params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        filtered_params = [
            (k, v) for k, v in query_params if k.lower() not in TRACKING_QUERY_PARAMS
        ]
        new_query = urllib.parse.urlencode(filtered_params)
        clean = urllib.parse.urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                parsed.params,
                new_query,
                "",  # strip fragment
            )
        )
        return clean
    except Exception:
        return url.strip().rstrip("/").lower()


def _is_valid_job_url(url: str) -> bool:
    """Checks whether a URL points to a specific job posting rather than a generic root."""
    if not url or not isinstance(url, str):
        return False
    u = url.strip().lower()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    try:
        parsed = urllib.parse.urlparse(u)
        path = parsed.path.strip("/")
        # If there's no path and no query, it's just a root homepage (e.g. https://boards.greenhouse.io)
        if not path and not parsed.query:
            return False
        # Very short path like '/' is not a valid posting
        if len(path) < 2 and not parsed.query:
            return False
        return True
    except Exception:
        return False


def _extract_requisition_id(posting: Any) -> Optional[str]:
    """Extracts explicit requisition or job ID from a posting or its URLs."""
    explicit_id = _extract_field(
        posting, "requisition_id", "job_id", "req_id", "posting_id"
    )
    if explicit_id:
        s = str(explicit_id).strip()
        if s and s.lower() not in ("none", "null", ""):
            return s

    # Attempt regex extraction from Greenhouse or Lever URLs
    urls = _extract_urls(posting)
    for u in urls:
        # Greenhouse pattern: /jobs/(\d+)
        gh_match = re.search(r"/jobs/(\d+)", u)
        if gh_match:
            return f"gh_{gh_match.group(1)}"
        # Lever pattern: /jobs.lever.co/[^/]+/([a-f0-9\-]+)
        lever_match = re.search(
            r"/jobs\.lever\.co/[^/]+/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
            u,
            re.IGNORECASE,
        )
        if lever_match:
            return f"lever_{lever_match.group(1).lower()}"
        # Hacker news pattern: item?id=(\d+)
        hn_match = re.search(r"item\?id=(\d+)", u)
        if hn_match:
            return f"hn_{hn_match.group(1)}"

    return None


# ==============================================================================
# JobPostingMatcher Class
# ==============================================================================


class JobPostingMatcher:
    """Production-grade similarity engine for job posting deduplication.

    Executes a disciplined 7-step comparison pipeline:
    1. Direct URL or Requisition ID Match (instant high-confidence match)
    2. Company Match Validation (instant no-match on company mismatch)
    3. Seniority Compatibility Guard (rejects e.g. Staff vs Senior)
    4. Team Specialization Conflict Check (rejects e.g. Platform vs Growth)
    5. Location / Remote Compatibility Check (rejects conflicting onsite locations)
    6. Title Fuzzy Scoring via RapidFuzz token_set_ratio & token_sort_ratio
    7. Skills Overlap Confidence Bonus (Jaccard similarity on skills sets)
    """

    def __init__(
        self,
        title_threshold: float = 88.0,
        relaxed_title_threshold: float = 85.0,
        skills_jaccard_threshold: float = 0.3,
        skills_bonus_weight: float = 5.0,
    ) -> None:
        """Initialize matcher with configurable thresholds.

        Args:
            title_threshold: Primary token_set_ratio threshold for match (default 88.0).
            relaxed_title_threshold: Secondary threshold when location/skills align (default 85.0).
            skills_jaccard_threshold: Minimum skills Jaccard index for heavy alignment (default 0.3).
            skills_bonus_weight: Maximum score bonus added for complete skills overlap (default 5.0).
        """
        self.title_threshold = title_threshold
        self.relaxed_title_threshold = relaxed_title_threshold
        self.skills_jaccard_threshold = skills_jaccard_threshold
        self.skills_bonus_weight = skills_bonus_weight

    # --------------------------------------------------------------------------
    # Normalization Utilities
    # --------------------------------------------------------------------------

    @classmethod
    def normalize_company(cls, company: Optional[str]) -> str:
        """Normalizes company name by stripping legal suffixes, punctuation, and whitespace."""
        if not company or not isinstance(company, str):
            return ""

        text = company.strip()
        # Remove corporate suffixes like Inc, LLC, Corp
        text = COMPANY_LEGAL_SUFFIXES.sub("", text)
        # Strip non-alphanumeric chars
        text = re.sub(r"[^a-zA-Z0-9\s]", " ", text)
        # Collapse whitespace
        return re.sub(r"\s+", " ", text).strip().lower()

    @classmethod
    def clean_title(cls, title: Optional[str]) -> str:
        """Sanitizes and normalizes job title for fuzzy comparison.

        Expands abbreviations (Sr. -> Senior), removes parenthetical location tags,
        and eliminates special punctuation.
        """
        if not title or not isinstance(title, str):
            return ""

        text = title.strip()
        # Strip parenthetical location/policy tags, e.g. (NYC Onsite), (Remote)
        text = TITLE_LOCATION_PARENS.sub(" ", text)

        # Expand common abbreviations
        for pattern, replacement in TITLE_ABBREVIATIONS:
            text = pattern.sub(replacement, text)

        # Remove special characters
        text = re.sub(r"[^a-zA-Z0-9\s]", " ", text)
        # Collapse whitespace
        return re.sub(r"\s+", " ", text).strip().lower()

    @classmethod
    def extract_seniority(cls, posting: Any, title: str = "") -> Optional[str]:
        """Extracts standardized seniority level (junior, mid, senior, staff_plus)."""
        explicit = _extract_field(posting, "seniority", "seniority_level", "level")
        if explicit is not None:
            if hasattr(explicit, "value"):
                explicit = explicit.value
            s_str = str(explicit).strip().lower()
            if s_str in SENIORITY_RANKS:
                return s_str

        # Fallback to inspecting job title
        check_text = f"{title} {_extract_field(posting, 'title', default='')}".lower()
        for level, pattern in SENIORITY_PATTERNS:
            if pattern.search(check_text):
                return level

        return None

    @classmethod
    def extract_specialization(cls, posting: Any, title: str = "") -> Optional[str]:
        """Extracts team specialization cluster (platform, growth, analytics, etc.)."""
        explicit_team = _extract_field(posting, "team", "specialization", "department")
        check_text = (
            f"{explicit_team or ''} {title} {_extract_field(posting, 'title', default='')}"
        ).lower()

        if not check_text.strip():
            return None

        # Look for cluster keywords
        for cluster, keywords in SPECIALIZATION_CLUSTERS.items():
            for kw in keywords:
                if re.search(r"\b" + re.escape(kw) + r"\b", check_text):
                    return cluster

        return None

    @classmethod
    def normalize_city(cls, city_str: Optional[str]) -> Optional[str]:
        """Maps city variations or metropolitan aliases to canonical city names."""
        if not city_str or not isinstance(city_str, str):
            return None

        c_lower = city_str.lower().strip()
        for canonical, aliases in CITY_ALIASES.items():
            for alias in aliases:
                if re.search(r"\b" + re.escape(alias) + r"\b", c_lower):
                    return canonical

        # Clean punctuation and return
        cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", c_lower)
        return re.sub(r"\s+", " ", cleaned).strip() or None

    @classmethod
    def extract_location_and_policy(
        cls, posting: Any, title: str = ""
    ) -> Tuple[Optional[str], Optional[str]]:
        """Extracts canonical city and remote policy ('onsite', 'hybrid', 'remote')."""
        city = _extract_field(posting, "city", "location", "workplace_city")
        remote_pol = _extract_field(
            posting, "remote_policy", "workplace_type", "remote"
        )
        full_text = (
            f"{title} {_extract_field(posting, 'title', default='')} {city or ''}"
        ).lower()

        # Resolve remote policy
        pol_str: Optional[str] = None
        if remote_pol is not None:
            if hasattr(remote_pol, "value"):
                remote_pol = remote_pol.value
            s_pol = str(remote_pol).lower().strip()
            if "remote" in s_pol:
                pol_str = "remote"
            elif "hybrid" in s_pol:
                pol_str = "hybrid"
            elif "onsite" in s_pol or "in-office" in s_pol or "office" in s_pol:
                pol_str = "onsite"

        if not pol_str:
            if re.search(r"\b(remote|wfh|work\s+from\s+home)\b", full_text):
                pol_str = "remote"
            elif re.search(r"\b(hybrid)\b", full_text):
                pol_str = "hybrid"
            elif re.search(r"\b(onsite|on-site|in-office|in\s+office)\b", full_text):
                pol_str = "onsite"

        # Resolve canonical city
        norm_city = cls.normalize_city(city) if city else None
        if not norm_city:
            # Check title for city keywords
            for canonical, aliases in CITY_ALIASES.items():
                for alias in aliases:
                    if re.search(r"\b" + re.escape(alias) + r"\b", full_text):
                        norm_city = canonical
                        break
                if norm_city:
                    break

        return norm_city, pol_str

    @classmethod
    def extract_skills_set(cls, posting: Any) -> Set[str]:
        """Extracts set of normalized skill strings."""
        skills = _extract_field(
            posting, "skills", "technologies", "tech_stack", default=[]
        )
        if isinstance(skills, str):
            skills = [skills]
        elif not isinstance(skills, (list, tuple, set)):
            skills = []

        result = set()
        for s in skills:
            if isinstance(s, str) and s.strip():
                clean_s = re.sub(r"[^a-zA-Z0-9\s\+\#]", "", s.strip().lower())
                if clean_s:
                    result.add(clean_s)
        return result

    @classmethod
    def calculate_skills_jaccard(cls, skills_1: Set[str], skills_2: Set[str]) -> float:
        """Calculates Jaccard similarity index between two skill sets."""
        if not skills_1 or not skills_2:
            return 0.0
        intersection = skills_1.intersection(skills_2)
        union = skills_1.union(skills_2)
        if not union:
            return 0.0
        return len(intersection) / len(union)

    # --------------------------------------------------------------------------
    # Main Comparison Pipeline
    # --------------------------------------------------------------------------

    def compare(self, p1: Any, p2: Any) -> MatchResult:
        """Compares two job postings and returns a structured MatchResult.

        Follows the strict 7-step comparison flow:
        1. Direct URL or requisition ID match
        2. Company match validation
        3. Seniority compatibility guard
        4. Team specialization conflict check
        5. Location / Remote compatibility check
        6. Title fuzzy scoring (RapidFuzz token_set_ratio)
        7. Skills overlap bonus (Jaccard similarity)
        """
        # Guard against None / invalid postings
        if p1 is None or p2 is None:
            return MatchResult(
                is_match=False,
                score=0.0,
                match_type="no_match",
                reason="One or both postings are None",
                details={"p1_present": p1 is not None, "p2_present": p2 is not None},
            )

        # ----------------------------------------------------------------------
        # Step 1: Direct URL or requisition ID match
        # ----------------------------------------------------------------------
        urls_1 = [_clean_tracking_url(u) for u in _extract_urls(p1) if _is_valid_job_url(u)]
        urls_2 = [_clean_tracking_url(u) for u in _extract_urls(p2) if _is_valid_job_url(u)]

        matched_url: Optional[str] = None
        for u1 in urls_1:
            for u2 in urls_2:
                if u1 == u2:
                    matched_url = u1
                    break
            if matched_url:
                break

        if matched_url:
            return MatchResult(
                is_match=True,
                score=100.0,
                match_type="url_match",
                reason=f"Direct source URL match: {matched_url}",
                details={"matched_url": matched_url, "urls_1": urls_1, "urls_2": urls_2},
            )

        req_id_1 = _extract_requisition_id(p1)
        req_id_2 = _extract_requisition_id(p2)
        if req_id_1 and req_id_2 and req_id_1.lower() == req_id_2.lower():
            return MatchResult(
                is_match=True,
                score=100.0,
                match_type="url_match",
                reason=f"Direct requisition ID match: {req_id_1}",
                details={"requisition_id": req_id_1},
            )

        # ----------------------------------------------------------------------
        # Step 2: Company match validation
        # ----------------------------------------------------------------------
        raw_comp_1 = _extract_field(p1, "company", "company_name")
        raw_comp_2 = _extract_field(p2, "company", "company_name")

        comp_1 = self.normalize_company(raw_comp_1)
        comp_2 = self.normalize_company(raw_comp_2)

        if not comp_1 or not comp_2:
            return MatchResult(
                is_match=False,
                score=0.0,
                match_type="no_match",
                reason="Missing company name on one or both postings",
                details={"company_1": raw_comp_1, "company_2": raw_comp_2},
            )

        company_match = False
        if comp_1 == comp_2:
            company_match = True
        else:
            comp_sort = fuzz.token_sort_ratio(comp_1, comp_2)
            comp_set = fuzz.token_set_ratio(comp_1, comp_2)
            if comp_sort >= 90.0 or (
                len(comp_1) >= 4 and len(comp_2) >= 4 and comp_set >= 95.0
            ):
                company_match = True

        if not company_match:
            return MatchResult(
                is_match=False,
                score=0.0,
                match_type="no_match",
                reason=f"Company mismatch: '{comp_1}' vs '{comp_2}'",
                details={
                    "company_1": comp_1,
                    "company_2": comp_2,
                    "raw_company_1": raw_comp_1,
                    "raw_company_2": raw_comp_2,
                },
            )

        # ----------------------------------------------------------------------
        # Step 3: Seniority compatibility guard
        # ----------------------------------------------------------------------
        raw_title_1 = str(_extract_field(p1, "title", "job_title", default="") or "")
        raw_title_2 = str(_extract_field(p2, "title", "job_title", default="") or "")

        s1 = self.extract_seniority(p1, raw_title_1)
        s2 = self.extract_seniority(p2, raw_title_2)

        # Check for staff_plus vs junior / mid / senior incompatibility
        if (s1 == "staff_plus" and s2 in ("senior", "junior", "mid")) or (
            s2 == "staff_plus" and s1 in ("senior", "junior", "mid")
        ):
            return MatchResult(
                is_match=False,
                score=0.0,
                match_type="no_match",
                reason="Incompatible seniority levels (e.g. Staff vs Senior)",
                details={"seniority_1": s1, "seniority_2": s2},
            )

        # Check for senior vs junior incompatibility
        if (s1 == "senior" and s2 == "junior") or (s2 == "senior" and s1 == "junior"):
            return MatchResult(
                is_match=False,
                score=0.0,
                match_type="no_match",
                reason="Incompatible seniority levels (Senior vs Junior)",
                details={"seniority_1": s1, "seniority_2": s2},
            )

        # ----------------------------------------------------------------------
        # Step 4: Team specialization conflict check
        # ----------------------------------------------------------------------
        spec1 = self.extract_specialization(p1, raw_title_1)
        spec2 = self.extract_specialization(p2, raw_title_2)

        # Both postings must have an explicit, differing specialization to trigger conflict
        if spec1 is not None and spec2 is not None and spec1 != spec2:
            return MatchResult(
                is_match=False,
                score=0.0,
                match_type="no_match",
                reason=f"Opposing team specializations: '{spec1}' vs '{spec2}'",
                details={
                    "specialization_1": spec1,
                    "specialization_2": spec2,
                    "title_1": raw_title_1,
                    "title_2": raw_title_2,
                },
            )

        # ----------------------------------------------------------------------
        # Step 5: Location / Remote compatibility
        # ----------------------------------------------------------------------
        loc1, pol1 = self.extract_location_and_policy(p1, raw_title_1)
        loc2, pol2 = self.extract_location_and_policy(p2, raw_title_2)

        is_remote_1 = pol1 == "remote"
        is_remote_2 = pol2 == "remote"
        is_onsite_1 = pol1 == "onsite"
        is_onsite_2 = pol2 == "onsite"

        # If both are strictly onsite in different cities, reject match
        if loc1 and loc2 and loc1 != loc2:
            if (is_onsite_1 and is_onsite_2) or (
                not is_remote_1 and not is_remote_2 and (is_onsite_1 or is_onsite_2)
            ):
                return MatchResult(
                    is_match=False,
                    score=0.0,
                    match_type="no_match",
                    reason=f"Incompatible onsite locations: '{loc1}' vs '{loc2}'",
                    details={
                        "location_1": loc1,
                        "location_2": loc2,
                        "policy_1": pol1,
                        "policy_2": pol2,
                    },
                )
            elif pol1 == "hybrid" and pol2 == "hybrid":
                return MatchResult(
                    is_match=False,
                    score=0.0,
                    match_type="no_match",
                    reason=f"Incompatible hybrid locations: '{loc1}' vs '{loc2}'",
                    details={
                        "location_1": loc1,
                        "location_2": loc2,
                        "policy_1": pol1,
                        "policy_2": pol2,
                    },
                )

        # ----------------------------------------------------------------------
        # Step 6: Title Fuzzy Scoring
        # ----------------------------------------------------------------------
        if not raw_title_1.strip() or not raw_title_2.strip():
            return MatchResult(
                is_match=False,
                score=0.0,
                match_type="no_match",
                reason="Missing job title on one or both postings",
                details={"title_1": raw_title_1, "title_2": raw_title_2},
            )

        clean_1 = self.clean_title(raw_title_1)
        clean_2 = self.clean_title(raw_title_2)

        if not clean_1 or not clean_2:
            return MatchResult(
                is_match=False,
                score=0.0,
                match_type="no_match",
                reason="Job title was empty after sanitization",
                details={"clean_title_1": clean_1, "clean_title_2": clean_2},
            )

        # RapidFuzz similarity ratios
        token_set = float(fuzz.token_set_ratio(clean_1, clean_2))
        token_sort = float(fuzz.token_sort_ratio(clean_1, clean_2))
        raw_ratio = float(fuzz.ratio(clean_1, clean_2))

        # ----------------------------------------------------------------------
        # Step 7: Skills overlap bonus
        # ----------------------------------------------------------------------
        skills_1 = self.extract_skills_set(p1)
        skills_2 = self.extract_skills_set(p2)
        jaccard = self.calculate_skills_jaccard(skills_1, skills_2)
        skills_bonus = round(jaccard * self.skills_bonus_weight, 2)

        # Evaluate location & skills alignment
        location_aligns = (
            (is_remote_1 and is_remote_2)
            or (loc1 and loc2 and loc1 == loc2)
            or (loc1 is None and loc2 is None)
        )
        skills_align = jaccard >= self.skills_jaccard_threshold

        diagnostic_details = {
            "clean_title_1": clean_1,
            "clean_title_2": clean_2,
            "token_set_ratio": token_set,
            "token_sort_ratio": token_sort,
            "ratio": raw_ratio,
            "skills_jaccard": round(jaccard, 4),
            "skills_bonus": skills_bonus,
            "seniority_1": s1,
            "seniority_2": s2,
            "specialization_1": spec1,
            "specialization_2": spec2,
            "location_1": loc1,
            "location_2": loc2,
            "policy_1": pol1,
            "policy_2": pol2,
            "location_aligns": location_aligns,
            "skills_align": skills_align,
        }

        # Case A: Identical canonical titles -> exact match
        if (
            raw_title_1.strip().lower() == raw_title_2.strip().lower()
            or clean_1 == clean_2
        ):
            is_exact = raw_title_1.strip().lower() == raw_title_2.strip().lower()
            return MatchResult(
                is_match=True,
                score=100.0,
                match_type="exact" if is_exact else "fuzzy_high_confidence",
                reason="Exact company and job title match"
                if is_exact
                else "Canonical normalized title match",
                details=diagnostic_details,
            )

        # Case B: High-confidence fuzzy title match (token_set_ratio >= title_threshold)
        if token_set >= self.title_threshold:
            final_score = min(100.0, round(token_set + skills_bonus, 2))
            return MatchResult(
                is_match=True,
                score=final_score,
                match_type="fuzzy_high_confidence",
                reason=(
                    f"High confidence fuzzy title match (score: {token_set:.1f}, "
                    f"skills overlap: {jaccard:.2f})"
                ),
                details=diagnostic_details,
            )

        # Case C: Relaxed fuzzy match when location and skills heavily align
        if (
            token_set >= self.relaxed_title_threshold
            and location_aligns
            and skills_align
        ):
            final_score = min(100.0, round(token_set + skills_bonus, 2))
            return MatchResult(
                is_match=True,
                score=final_score,
                match_type="fuzzy_high_confidence",
                reason=(
                    f"Fuzzy title match with aligned location and skills (score: {token_set:.1f}, "
                    f"skills overlap: {jaccard:.2f})"
                ),
                details=diagnostic_details,
            )

        # Case D: Similarity below threshold -> no match
        return MatchResult(
            is_match=False,
            score=round(token_set, 2),
            match_type="no_match",
            reason=f"Title similarity score ({token_set:.1f}) below threshold ({self.title_threshold})",
            details=diagnostic_details,
        )
