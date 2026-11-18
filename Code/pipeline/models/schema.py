"""Schema definitions and Pydantic models for the job posting data pipeline.

Defines:
- Strict Enums: SeniorityEnum, RemotePolicyEnum, EmploymentTypeEnum, StatusEnum
- JobPostingRecord: The canonical target record model matching the assignment schema
- RawExtractionOutput: The LLM structured JSON output model prior to canonical enrichment
- generate_deterministic_id: Helper for stable, idempotent posting IDs
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pipeline.models.validators import normalize_annual_salary, validate_salary


# ==============================================================================
# Strict Enums
# ==============================================================================


class SeniorityEnum(str, Enum):
    """Standardized seniority levels."""

    junior = "junior"
    mid = "mid"
    senior = "senior"
    staff_plus = "staff_plus"


class RemotePolicyEnum(str, Enum):
    """Standardized workplace remote policies."""

    onsite = "onsite"
    hybrid = "hybrid"
    remote = "remote"


class EmploymentTypeEnum(str, Enum):
    """Standardized employment commitment types."""

    full_time = "full_time"
    contract = "contract"
    intern = "intern"


class StatusEnum(str, Enum):
    """Lifecycle status of a job posting."""

    open = "open"
    closed = "closed"


# ==============================================================================
# Deterministic ID Generator
# ==============================================================================


def generate_deterministic_id(company: str, title: str, location_or_url: str = "") -> str:
    """Generates a stable, deterministic SHA-256 based posting ID.

    Normalizes whitespace and casing to guarantee identical IDs across pipeline runs.

    Args:
        company: Normalized company name.
        title: Canonical job title.
        location_or_url: City/country or source URL for disambiguation.

    Returns:
        Deterministic 16-character hexadecimal hash string.
    """
    c = (company or "").strip().lower()
    t = (title or "").strip().lower()
    loc = (location_or_url or "").strip().lower()
    composite = f"{c}::{t}::{loc}".encode("utf-8")
    return hashlib.sha256(composite).hexdigest()[:16]


# ==============================================================================
# Canonical Job Posting Record
# ==============================================================================


class JobPostingRecord(BaseModel):
    """Canonical unified job posting record adhering to the assignment target schema.

    Represents an entity-resolved, enriched job posting with source audit history.
    """

    model_config = ConfigDict(
        use_enum_values=False,
        validate_assignment=True,
        extra="forbid",
    )

    posting_id: str = Field(..., description="Stable deterministic ID across runs")
    company: str = Field(..., description="Normalized company name")
    title: str = Field(..., description="Canonical title")
    seniority: Optional[SeniorityEnum] = Field(
        default=None,
        description="Seniority level: junior, mid, senior, staff_plus",
    )
    city: Optional[str] = Field(default=None, description="City if stated")
    country: Optional[str] = Field(default=None, description="Country if stated (e.g. US)")
    remote_policy: Optional[RemotePolicyEnum] = Field(
        default=None,
        description="Remote work policy: onsite, hybrid, remote",
    )
    salary_min: Optional[float] = Field(
        default=None,
        description="Annualized minimum salary in specified currency",
    )
    salary_max: Optional[float] = Field(
        default=None,
        description="Annualized maximum salary in specified currency",
    )
    currency: Optional[str] = Field(
        default=None,
        description="Currency code, e.g. USD",
    )
    employment_type: Optional[EmploymentTypeEnum] = Field(
        default=None,
        description="Employment type: full_time, contract, intern",
    )
    skills: List[str] = Field(
        default_factory=list,
        description="List of extracted skills, e.g. ['Python', 'Spark', 'dbt']",
    )
    visa_sponsorship: Optional[bool] = Field(
        default=None,
        description="True/False if explicitly stated, else null",
    )
    posted_date: Optional[date] = Field(
        default=None,
        description="Date posted if stated",
    )
    status: StatusEnum = Field(
        default=StatusEnum.open,
        description="Lifecycle status: open or closed",
    )
    source_urls: List[str] = Field(
        default_factory=list,
        description="Source links merged into this canonical record",
    )
    evidence: Dict[str, str] = Field(
        default_factory=dict,
        description="Field name -> source snippet justifying the extraction",
    )
    first_seen: datetime = Field(..., description="Timestamp when posting was first observed")
    last_seen: datetime = Field(..., description="Timestamp when posting was last observed")

    @field_validator("skills", mode="before")
    @classmethod
    def _coerce_skills(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return list(v)

    @field_validator("source_urls", mode="before")
    @classmethod
    def _coerce_source_urls(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return list(v)

    @field_validator("evidence", mode="before")
    @classmethod
    def _coerce_evidence(cls, v: Any) -> Dict[str, str]:
        if v is None:
            return {}
        return dict(v)

    @model_validator(mode="after")
    def _validate_salary_integrity(self) -> "JobPostingRecord":
        """Enforces realistic annualized salary bounds and min <= max ordering."""
        if self.salary_min is not None or self.salary_max is not None:
            validate_salary(
                salary_min=self.salary_min,
                salary_max=self.salary_max,
                currency=self.currency,
                raise_on_error=True,
            )
        return self

    def to_dict(self, mode: str = "python") -> Dict[str, Any]:
        """Serializes model to a dictionary."""
        return self.model_dump(mode=mode)

    def to_json(self, indent: Optional[int] = None) -> str:
        """Serializes model to a JSON string."""
        return self.model_dump_json(indent=indent)


# ==============================================================================
# Raw Extraction Output (Direct LLM Output)
# ==============================================================================


class RawExtractionOutput(BaseModel):
    """Structured JSON model returned directly by the LLM before canonical enrichment.

    All fields are optional, defaulting to None or empty collections, ensuring strict
    'null over guessing' compliance.
    """

    model_config = ConfigDict(
        extra="ignore",
        use_enum_values=False,
        validate_assignment=True,
    )

    company: Optional[str] = Field(
        default=None,
        description="Raw company name stated in posting",
    )
    title: Optional[str] = Field(
        default=None,
        description="Raw job title stated in posting",
    )
    seniority: Optional[SeniorityEnum] = Field(
        default=None,
        description="junior, mid, senior, staff_plus if stated",
    )
    city: Optional[str] = Field(
        default=None,
        description="City location if stated",
    )
    country: Optional[str] = Field(
        default=None,
        description="Country location if stated",
    )
    remote_policy: Optional[RemotePolicyEnum] = Field(
        default=None,
        description="onsite, hybrid, remote if stated",
    )
    salary_min: Optional[float] = Field(
        default=None,
        description="Minimum salary extracted",
    )
    salary_max: Optional[float] = Field(
        default=None,
        description="Maximum salary extracted",
    )
    salary_period: Optional[str] = Field(
        default=None,
        description="Frequency: 'hour', 'month', 'year', 'week', etc.",
    )
    currency: Optional[str] = Field(
        default=None,
        description="Currency code, e.g. USD, EUR",
    )
    employment_type: Optional[EmploymentTypeEnum] = Field(
        default=None,
        description="full_time, contract, intern if stated",
    )
    skills: List[str] = Field(
        default_factory=list,
        description="List of technical/domain skills mentioned",
    )
    visa_sponsorship: Optional[bool] = Field(
        default=None,
        description="True if sponsored, False if explicitly not sponsored, None if unstated",
    )
    posted_date: Optional[date] = Field(
        default=None,
        description="Posting date if stated in description",
    )
    evidence: Dict[str, str] = Field(
        default_factory=dict,
        description="Exact literal source snippets justifying each extracted field",
    )

    @field_validator("salary_min", "salary_max", mode="before")
    @classmethod
    def _clean_salary_input(cls, v: Any) -> Optional[float]:
        """Cleans numeric strings with currency symbols or commas (e.g. '$150,000' -> 150000.0)."""
        if v is None or v == "":
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            cleaned = re.sub(r"[^\d.]", "", v)
            if cleaned:
                try:
                    return float(cleaned)
                except ValueError:
                    return None
        return v

    @field_validator("visa_sponsorship", mode="before")
    @classmethod
    def _clean_visa_input(cls, v: Any) -> Optional[bool]:
        """Parses common LLM string outputs for visa sponsorship into boolean or None."""
        if v is None or v == "":
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            clean = v.strip().lower()
            if clean in ("true", "yes", "1", "available", "sponsored", "will sponsor"):
                return True
            if clean in ("false", "no", "0", "not sponsored", "unable to sponsor", "no sponsorship", "unavailable"):
                return False
            if clean in ("none", "null", "unknown", "unclear", "n/a", "not mentioned", "not stated"):
                return None
        return v

    @field_validator("skills", mode="before")
    @classmethod
    def _coerce_skills(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return list(v)

    @field_validator("evidence", mode="before")
    @classmethod
    def _coerce_evidence(cls, v: Any) -> Dict[str, str]:
        if v is None:
            return {}
        return dict(v)

    def to_job_posting_record(
        self,
        posting_id: str,
        first_seen: datetime,
        last_seen: datetime,
        source_urls: Optional[List[str]] = None,
        company: Optional[str] = None,
        title: Optional[str] = None,
        status: StatusEnum = StatusEnum.open,
        annualize_salary: bool = True,
    ) -> JobPostingRecord:
        """Converts raw LLM extraction output into a validated canonical JobPostingRecord.

        Handles salary annualization if salary_period is provided and annualize_salary=True.

        Args:
            posting_id: Deterministic posting identifier.
            first_seen: First observation timestamp.
            last_seen: Last observation timestamp.
            source_urls: Source URLs to attach.
            company: Override/normalized company name (falls back to self.company or 'Unknown').
            title: Override/canonical title (falls back to self.title or 'Unknown').
            status: Lifecycle status (default open).
            annualize_salary: Whether to normalize hourly/monthly salaries to annual figures.

        Returns:
            Validated JobPostingRecord instance.
        """
        sal_min = self.salary_min
        sal_max = self.salary_max

        if annualize_salary and self.salary_period:
            if sal_min is not None:
                sal_min = normalize_annual_salary(sal_min, self.salary_period)
            if sal_max is not None:
                sal_max = normalize_annual_salary(sal_max, self.salary_period)

        return JobPostingRecord(
            posting_id=posting_id,
            company=company or self.company or "Unknown",
            title=title or self.title or "Unknown",
            seniority=self.seniority,
            city=self.city,
            country=self.country,
            remote_policy=self.remote_policy,
            salary_min=sal_min,
            salary_max=sal_max,
            currency=self.currency,
            employment_type=self.employment_type,
            skills=self.skills,
            visa_sponsorship=self.visa_sponsorship,
            posted_date=self.posted_date,
            status=status,
            source_urls=source_urls or [],
            evidence=self.evidence,
            first_seen=first_seen,
            last_seen=last_seen,
        )

    def to_dict(self, mode: str = "python") -> Dict[str, Any]:
        """Serializes model to a dictionary."""
        return self.model_dump(mode=mode)

    def to_json(self, indent: Optional[int] = None) -> str:
        """Serializes model to a JSON string."""
        return self.model_dump_json(indent=indent)
