"""Canonical Job Posting Model for cross-source entity resolution and deduplication.

Defines:
- CanonicalJobPosting: The unified canonical job entity resulting from merging duplicate postings
- generate_canonical_id: Deterministic 64-character SHA-256 hash generator
- format_skill: Standardized skill casing and acronym preservation helper
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pipeline.models.schema import (
    EmploymentTypeEnum,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
)
from pipeline.models.validators import validate_salary


# ==============================================================================
# Helper Utilities
# ==============================================================================


KNOWN_SKILL_ACRONYMS: Dict[str, str] = {
    "sql": "SQL",
    "aws": "AWS",
    "gcp": "GCP",
    "azure": "Azure",
    "etl": "ETL",
    "elt": "ELT",
    "dbt": "dbt",
    "api": "API",
    "ml": "ML",
    "ai": "AI",
    "k8s": "K8s",
    "ci/cd": "CI/CD",
    "nosql": "NoSQL",
    "llm": "LLM",
    "spark": "Spark",
    "kafka": "Kafka",
    "flink": "Flink",
    "airflow": "Airflow",
    "snowflake": "Snowflake",
    "bigquery": "BigQuery",
    "redshift": "Redshift",
    "python": "Python",
    "scala": "Scala",
    "java": "Java",
    "rust": "Rust",
    "golang": "Go",
    "go": "Go",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "mongodb": "MongoDB",
    "redis": "Redis",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "terraform": "Terraform",
}


def format_skill(skill: str) -> str:
    """Standardizes skill casing and preserves canonical acronyms.

    Args:
        skill: Raw skill string (e.g. 'python', 'sql', 'dbt').

    Returns:
        Title-cased or canonical acronym formatted string.
    """
    s = str(skill).strip()
    if not s:
        return ""
    s_lower = s.lower()
    if s_lower in KNOWN_SKILL_ACRONYMS:
        return KNOWN_SKILL_ACRONYMS[s_lower]
    # For acronyms with 2-4 uppercase characters already provided
    if s.isupper() and 2 <= len(s) <= 4:
        return s
    return s.title()


def generate_canonical_id(company: str, title: str, location: str = "") -> str:
    """Generates a stable, deterministic 64-character SHA-256 based canonical ID.

    Normalizes whitespace and casing across company, title, and primary location
    to guarantee identical canonical IDs across pipeline runs and deduplication passes.

    Args:
        company: Normalized company name.
        title: Canonical job title.
        location: City/country or primary location string for disambiguation.

    Returns:
        Deterministic 64-character hexadecimal SHA-256 hash string.
    """
    c = (company or "").strip().lower()
    t = (title or "").strip().lower()
    loc = (location or "").strip().lower()
    composite = f"{c}::{t}::{loc}".encode("utf-8")
    return hashlib.sha256(composite).hexdigest()


# ==============================================================================
# Canonical Job Posting Model
# ==============================================================================


class CanonicalJobPosting(BaseModel):
    """Canonical unified job posting entity resulting from deduplication.

    Represents a merged golden record combining multiple duplicate job postings
    from disparate sources (e.g. Greenhouse, Lever, Hacker News) with complete
    audit history and provenance tracking.
    """

    model_config = ConfigDict(
        use_enum_values=False,
        validate_assignment=True,
        extra="forbid",
    )

    canonical_id: str = Field(
        ...,
        description="Deterministic 64-char SHA-256 hash of company + title + primary location",
    )
    company: str = Field(..., description="Canonical normalized company name")
    title: str = Field(..., description="Canonical normalized job title")
    seniority: Optional[SeniorityEnum] = Field(
        default=None,
        description="Standardized seniority level: junior, mid, senior, staff_plus",
    )
    city: Optional[str] = Field(default=None, description="City if stated")
    country: Optional[str] = Field(default=None, description="Country if stated (e.g. US)")
    remote_policy: Optional[RemotePolicyEnum] = Field(
        default=None,
        description="Standardized remote policy: onsite, hybrid, remote",
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
        description="ISO currency code, e.g. USD",
    )
    employment_type: Optional[EmploymentTypeEnum] = Field(
        default=None,
        description="Standardized employment type: full_time, contract, intern",
    )
    skills: List[str] = Field(
        default_factory=list,
        description="Deduplicated union of skills across sources, title-cased",
    )
    visa_sponsorship: Optional[bool] = Field(
        default=None,
        description="True/False if explicitly stated, else null",
    )
    sources: List[str] = Field(
        default_factory=list,
        description="List of distinct scraper sources merged (e.g. ['greenhouse', 'hackernews'])",
    )
    source_urls: List[str] = Field(
        default_factory=list,
        description="Deduplicated list of all original job posting URLs",
    )
    source_posting_ids: List[str] = Field(
        default_factory=list,
        description="List of all raw/extracted posting IDs merged into this canonical entity",
    )
    evidence: Dict[str, str] = Field(
        default_factory=dict,
        description="Combined verbatim evidence snippets justified by highest-authority sources",
    )
    first_seen: datetime = Field(
        ...,
        description="Timestamp when job posting was first observed across any source",
    )
    last_seen: datetime = Field(
        ...,
        description="Timestamp when job posting was most recently observed across any source",
    )
    duplicate_count: int = Field(
        default=1,
        ge=1,
        description="Total raw postings merged into this canonical entity (>= 1)",
    )
    status: StatusEnum = Field(
        default=StatusEnum.open,
        description="Lifecycle status of job posting (open or closed)",
    )
    posted_date: Optional[date] = Field(
        default=None,
        description="Original posted date if available from highest-authority source",
    )

    @field_validator("canonical_id", mode="before")
    @classmethod
    def _coerce_canonical_id(cls, v: Any) -> str:
        if not v or not isinstance(v, str):
            raise ValueError("canonical_id must be a non-empty string")
        v_clean = v.strip().lower()
        if len(v_clean) != 64:
            # If not 64-char hex, validate if hex or hash
            if not all(c in "0123456789abcdefABCDEF" for c in v_clean):
                raise ValueError(f"canonical_id must be a 64-char hexadecimal string, got {len(v_clean)} chars")
        return v_clean

    @field_validator("skills", mode="before")
    @classmethod
    def _normalize_skills(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        seen: Dict[str, str] = {}
        for item in v:
            s = str(item).strip()
            if not s:
                continue
            formatted = format_skill(s)
            k = formatted.lower()
            if k not in seen:
                seen[k] = formatted
        return sorted(list(seen.values()), key=str.lower)

    @field_validator("sources", mode="before")
    @classmethod
    def _coerce_sources(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        seen = set()
        result: List[str] = []
        for item in v:
            s = str(item).strip().lower()
            if s and s not in seen:
                seen.add(s)
                result.append(s)
        return result

    @field_validator("source_urls", mode="before")
    @classmethod
    def _coerce_source_urls(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        seen = set()
        result: List[str] = []
        for item in v:
            s = str(item).strip()
            if s and s not in seen:
                seen.add(s)
                result.append(s)
        return result

    @field_validator("source_posting_ids", mode="before")
    @classmethod
    def _coerce_posting_ids(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        seen = set()
        result: List[str] = []
        for item in v:
            s = str(item).strip()
            if s and s not in seen:
                seen.add(s)
                result.append(s)
        return result

    @field_validator("evidence", mode="before")
    @classmethod
    def _coerce_evidence(cls, v: Any) -> Dict[str, str]:
        if v is None:
            return {}
        return dict(v)

    @model_validator(mode="after")
    def _validate_salary_integrity(self) -> "CanonicalJobPosting":
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
        """Serializes model to a standard dictionary."""
        return self.model_dump(mode=mode)

    def to_json(self, indent: Optional[int] = None) -> str:
        """Serializes model to a formatted JSON string."""
        return self.model_dump_json(indent=indent)
