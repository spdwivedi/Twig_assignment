"""Anti-hallucination and normalization validation engine for job posting extraction.

Provides strict validators for:
- Realistic salary bounds checking and min <= max verification
- Annualized compensation normalization (hourly x2080, monthly x12)
- Verbatim evidence presence and source text verification
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple, Union

# Realistic annualized salary bounds by currency (min_annual, max_annual)
CURRENCY_ANNUAL_BOUNDS: Dict[str, Tuple[float, float]] = {
    "USD": (10_000.0, 2_000_000.0),
    "EUR": (10_000.0, 2_000_000.0),
    "GBP": (8_000.0, 2_000_000.0),
    "CAD": (10_000.0, 2_500_000.0),
    "AUD": (15_000.0, 2_500_000.0),
    "CHF": (15_000.0, 2_500_000.0),
    "SGD": (15_000.0, 2_500_000.0),
    "NZD": (15_000.0, 2_500_000.0),
    "INR": (100_000.0, 100_000_000.0),
    "JPY": (1_000_000.0, 500_000_000.0),
    "KRW": (10_000_000.0, 1_000_000_000.0),
}
DEFAULT_ANNUAL_BOUNDS: Tuple[float, float] = (10_000.0, 5_000_000.0)

# Field aliases for mapping extracted field names to evidence dictionary keys
FIELD_EVIDENCE_ALIASES: Dict[str, List[str]] = {
    "salary_min": ["salary", "compensation", "comp", "pay", "pay_range", "base_salary"],
    "salary_max": ["salary", "compensation", "comp", "pay", "pay_range", "base_salary"],
    "currency": ["salary", "compensation", "comp", "pay", "pay_range", "base_salary"],
    "remote_policy": ["remote", "location", "workplace", "work_policy", "work_arrangement"],
    "city": ["location", "office", "address"],
    "country": ["location", "office", "address"],
    "visa_sponsorship": ["visa", "sponsorship", "work_authorization", "authorization"],
    "seniority": ["level", "seniority_level", "experience", "experience_level"],
    "employment_type": ["job_type", "employment", "commitment"],
    "skills": ["requirements", "qualifications", "tech_stack", "stack", "technologies"],
    "posted_date": ["date", "posted", "timestamp"],
}


def normalize_annual_salary(amount: float, period: str) -> float:
    """Standardizes hourly (x2080), monthly (x12), weekly (x52), daily (x260),

    and annual compensation into an annualized figure.

    Args:
        amount: Raw compensation amount (must be positive).
        period: Pay frequency (e.g. 'hour', 'month', 'year', 'week', 'day').

    Returns:
        Annualized compensation amount as a float rounded to 2 decimal places.

    Raises:
        ValueError: If amount is non-positive or period is unsupported.
    """
    if amount is None or amount <= 0:
        raise ValueError(f"Salary amount must be a positive number, got: {amount}")

    if not period or not isinstance(period, str) or not period.strip():
        raise ValueError("Salary period must be a non-empty string (e.g. 'hour', 'month', 'year')")

    p = period.strip().lower()

    # Hourly rates: standard full-time year = 40 hours/week * 52 weeks = 2,080 hours
    if p in ("hour", "hourly", "hr", "h", "/hour", "/hr", "per hour"):
        return round(float(amount) * 2080.0, 2)

    # Monthly rates: 12 months/year
    if p in ("month", "monthly", "mo", "m", "/month", "/mo", "per month"):
        return round(float(amount) * 12.0, 2)

    # Annual / yearly rates: 1x
    if p in ("year", "yearly", "annual", "annually", "yr", "y", "/year", "/yr", "per year", "annum", "p.a."):
        return round(float(amount), 2)

    # Weekly rates: 52 weeks/year
    if p in ("week", "weekly", "wk", "w", "/week", "/wk", "per week"):
        return round(float(amount) * 52.0, 2)

    # Daily rates: 260 working days/year (5 days * 52 weeks)
    if p in ("day", "daily", "d", "/day", "/d", "per day"):
        return round(float(amount) * 260.0, 2)

    raise ValueError(
        f"Unsupported salary period: '{period}'. "
        "Expected one of: 'hour', 'month', 'year', 'week', 'day'."
    )


def validate_salary(
    salary_min: Optional[float],
    salary_max: Optional[float],
    currency: Optional[str] = "USD",
    raise_on_error: bool = True,
) -> bool:
    """Ensures min <= max, checks positivity, and validates realistic annualized bounds.

    Args:
        salary_min: Annualized minimum salary.
        salary_max: Annualized maximum salary.
        currency: 3-letter currency code (e.g. 'USD', 'EUR').
        raise_on_error: If True, raises ValueError on violation. If False, returns False.

    Returns:
        True if valid.

    Raises:
        ValueError: If salary bounds or ordering are violated (when raise_on_error=True).
    """
    # If neither is set, no salary validation needed
    if salary_min is None and salary_max is None:
        return True

    # 1. Non-positivity check
    if salary_min is not None and salary_min <= 0:
        if raise_on_error:
            raise ValueError(f"salary_min must be positive, got {salary_min}")
        return False

    if salary_max is not None and salary_max <= 0:
        if raise_on_error:
            raise ValueError(f"salary_max must be positive, got {salary_max}")
        return False

    # 2. Ordering check (min <= max)
    if salary_min is not None and salary_max is not None:
        if salary_min > salary_max:
            if raise_on_error:
                raise ValueError(
                    f"salary_min ({salary_min}) cannot be greater than salary_max ({salary_max})"
                )
            return False

    # 3. Realistic annualized bounds check
    curr_code = (currency or "USD").strip().upper()
    min_bound, max_bound = CURRENCY_ANNUAL_BOUNDS.get(curr_code, DEFAULT_ANNUAL_BOUNDS)

    if salary_min is not None:
        if salary_min < min_bound or salary_min > max_bound:
            if raise_on_error:
                raise ValueError(
                    f"salary_min ({salary_min}) is outside realistic annualized bounds "
                    f"[{min_bound:,.0f}, {max_bound:,.0f}] for currency '{curr_code}'. "
                    "Ensure amounts are annualized."
                )
            return False

    if salary_max is not None:
        if salary_max < min_bound or salary_max > max_bound:
            if raise_on_error:
                raise ValueError(
                    f"salary_max ({salary_max}) is outside realistic annualized bounds "
                    f"[{min_bound:,.0f}, {max_bound:,.0f}] for currency '{curr_code}'. "
                    "Ensure amounts are annualized."
                )
            return False

    return True


def validate_evidence_presence(
    field_name: str,
    field_val: Any,
    evidence_dict: Dict[str, str],
) -> bool:
    """Verifies that non-null extracted fields have non-empty justification snippets in evidence.

    Unstated fields (None, empty collections, or empty strings) do not require evidence.
    Non-null fields must have either an exact key match or an approved alias in evidence_dict
    with a non-empty string snippet.

    Args:
        field_name: Name of the extracted field (e.g. 'salary_min', 'remote_policy').
        field_val: Extracted value (if None/empty, returns True).
        evidence_dict: Mapping of field name or alias to source snippet string.

    Returns:
        True if valid evidence is present or field is unstated; False otherwise.
    """
    # Null or empty fields do not require justification
    if field_val is None:
        return True
    if isinstance(field_val, (str, list, dict, set)) and len(field_val) == 0:
        return True

    if not isinstance(evidence_dict, dict):
        return False

    # 1. Exact match in evidence
    if field_name in evidence_dict:
        snippet = evidence_dict[field_name]
        if isinstance(snippet, str) and snippet.strip():
            return True

    # 2. Check known aliases (e.g. 'salary' for 'salary_min' / 'salary_max')
    aliases = FIELD_EVIDENCE_ALIASES.get(field_name, [])
    for alias in aliases:
        if alias in evidence_dict:
            snippet = evidence_dict[alias]
            if isinstance(snippet, str) and snippet.strip():
                return True

    return False


def validate_evidence_in_text(
    evidence_snippet: str,
    source_text: str,
    case_sensitive: bool = False,
    min_overlap_ratio: float = 0.8,
) -> bool:
    """Verifies that an extracted evidence snippet actually exists within the source raw text.

    Protects against LLM hallucinations where justification text is fabricated.

    Args:
        evidence_snippet: Snippet returned in evidence dict.
        source_text: Raw job posting HTML or text.
        case_sensitive: Whether match should be case-sensitive.
        min_overlap_ratio: Minimum ratio of normalized snippet tokens found in source text.

    Returns:
        True if snippet is verified in source text; False otherwise.
    """
    if not evidence_snippet or not source_text:
        return False

    snip = evidence_snippet.strip()
    src = source_text

    if not case_sensitive:
        snip = snip.lower()
        src = src.lower()

    # 1. Direct substring check
    if snip in src:
        return True

    # 2. Whitespace-normalized substring check
    norm_snip = re.sub(r"\s+", " ", snip).strip()
    norm_src = re.sub(r"\s+", " ", src).strip()
    if norm_snip in norm_src:
        return True

    # 3. Token overlap check for long snippets (>= 5 words)
    tokens = [t for t in re.findall(r"\w+", norm_snip) if len(t) > 2]
    if tokens:
        matches = sum(1 for t in tokens if t in norm_src)
        if (matches / len(tokens)) >= min_overlap_ratio:
            return True

    return False


def validate_record_evidence(
    record: Any,
    fields_to_check: Optional[List[str]] = None,
) -> Dict[str, bool]:
    """Validates evidence presence across all non-null fields of a record.

    Args:
        record: JobPostingRecord, RawExtractionOutput, or dict with extracted fields & evidence.
        fields_to_check: Specific fields to check. If None, checks standard auditable fields.

    Returns:
        Dictionary mapping field names to boolean validation results.
    """
    if hasattr(record, "model_dump"):
        data = record.model_dump()
    elif isinstance(record, dict):
        data = record
    else:
        data = getattr(record, "__dict__", {})

    evidence_dict = data.get("evidence", {}) or {}

    default_fields = [
        "seniority",
        "remote_policy",
        "salary_min",
        "salary_max",
        "employment_type",
        "visa_sponsorship",
        "skills",
    ]
    target_fields = fields_to_check or default_fields

    results: Dict[str, bool] = {}
    for f in target_fields:
        val = data.get(f)
        results[f] = validate_evidence_presence(f, val, evidence_dict)

    return results
