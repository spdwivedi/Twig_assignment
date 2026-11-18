"""Canonical Record Merger for Cross-Source Deduplication.

Merges clusters of duplicate JobPostingRecords into a single, high-fidelity
CanonicalJobPosting using source authority weights, non-null field fallback,
case-insensitive skills union, and verbatim evidence preservation.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import urlparse

from pipeline.models.canonical import (
    CanonicalJobPosting,
    format_skill,
    generate_canonical_id,
)
from pipeline.models.schema import (
    EmploymentTypeEnum,
    JobPostingRecord,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
)

logger = logging.getLogger(__name__)


# Standard source authority weight hierarchy
DEFAULT_SOURCE_AUTHORITY: Dict[str, int] = {
    "greenhouse": 10,
    "lever": 10,
    "careers_page": 8,
    "careers": 8,
    "hackernews": 5,
    "hn": 5,
    "default": 1,
}


def extract_source_name(record: Union[JobPostingRecord, Dict[str, Any]]) -> str:
    """Infers the source origin name from a job record or its URLs.

    Args:
        record: JobPostingRecord instance or dictionary.

    Returns:
        Canonical source identifier string ('greenhouse', 'lever', 'careers_page', 'hackernews', or 'default').
    """
    # 1. Direct attribute or evidence hint
    if hasattr(record, "source") and getattr(record, "source"):
        src = str(getattr(record, "source")).strip().lower()
        if src in ("hn", "hackernews", "whoishiring"):
            return "hackernews"
        return src

    if isinstance(record, dict) and record.get("source"):
        src = str(record["source"]).strip().lower()
        if src in ("hn", "hackernews", "whoishiring"):
            return "hackernews"
        return src

    # 2. Evidence dictionary source inspection
    evidence = getattr(record, "evidence", None)
    if isinstance(evidence, dict) and evidence.get("source"):
        src = str(evidence["source"]).strip().lower()
        if src in ("hn", "hackernews", "whoishiring"):
            return "hackernews"
        return src

    # 3. Inspect source_urls
    source_urls = getattr(record, "source_urls", None)
    if isinstance(record, dict):
        source_urls = record.get("source_urls")

    if source_urls and isinstance(source_urls, list):
        for u in source_urls:
            u_str = str(u).lower()
            if "greenhouse.io" in u_str:
                return "greenhouse"
            if "lever.co" in u_str:
                return "lever"
            if "news.ycombinator.com" in u_str or "ycombinator" in u_str:
                return "hackernews"
            if "careers." in u_str or "/careers" in u_str or "/jobs" in u_str:
                return "careers_page"

    return "default"


class RecordMerger:
    """Merges a cluster of duplicate JobPostingRecords into a single CanonicalJobPosting.

    Resolves field conflicts using deterministic source authority prioritization:
    - Company & Title: Taken from the highest-authority source.
    - City, Country, Remote Policy: Taken from the highest-authority source with non-null values.
    - Salary: Non-null takes priority over null. If multiple sources have salary, highest-authority source takes precedence.
    - Seniority & Employment Type: Non-null from highest-authority source.
    - Skills: Case-insensitive set union of all skills across all records, formatted/title-cased.
    - Evidence: Combined evidence dictionary retaining highest-authority snippet per field.
    - Sources & URLs: Union of all distinct sources and original posting URLs.
    - Timestamps: first_seen is min across records; last_seen is max across records.
    - Duplicate Count: Total number of raw records merged (cluster size >= 1).
    """

    def __init__(self, authority_weights: Optional[Dict[str, int]] = None) -> None:
        """Initialize RecordMerger with custom or default source authority weights.

        Args:
            authority_weights: Optional mapping of source name to integer weight.
        """
        self.authority_weights: Dict[str, int] = dict(DEFAULT_SOURCE_AUTHORITY)
        if authority_weights:
            self.authority_weights.update(authority_weights)

    def get_source(self, record: Union[JobPostingRecord, Dict[str, Any]]) -> str:
        """Determines the source identifier for a record."""
        return extract_source_name(record)

    def get_authority(self, record: Union[JobPostingRecord, Dict[str, Any]]) -> int:
        """Returns the authority score for a given job posting record."""
        src = self.get_source(record)
        return self.authority_weights.get(src, self.authority_weights.get("default", 1))

    @staticmethod
    def _to_utc_datetime(dt_val: Union[datetime, date, str, None]) -> Optional[datetime]:
        """Converts dates, strings, or datetimes to timezone-aware UTC datetime."""
        if dt_val is None:
            return None
        if isinstance(dt_val, datetime):
            if dt_val.tzinfo is None:
                return dt_val.replace(tzinfo=timezone.utc)
            return dt_val.astimezone(timezone.utc)
        if isinstance(dt_val, date):
            return datetime(dt_val.year, dt_val.month, dt_val.day, tzinfo=timezone.utc)
        if isinstance(dt_val, str):
            try:
                # ISO parsing
                cleaned = dt_val.replace("Z", "+00:00")
                parsed = datetime.fromisoformat(cleaned)
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except Exception:
                return None
        return None

    def merge(self, cluster: List[JobPostingRecord]) -> CanonicalJobPosting:
        """Merges a cluster of duplicate JobPostingRecords into a single CanonicalJobPosting.

        Args:
            cluster: Non-empty list of duplicate JobPostingRecord instances.

        Returns:
            Unified, strongly-typed CanonicalJobPosting golden entity.

        Raises:
            ValueError: If cluster is empty.
        """
        if not cluster:
            raise ValueError("Cannot merge an empty cluster of job postings")

        # 1. Sort records by authority descending (secondary key: most recent last_seen)
        def sort_key(r: JobPostingRecord) -> Tuple[int, datetime]:
            auth = self.get_authority(r)
            ts = self._to_utc_datetime(r.last_seen) or datetime.min.replace(tzinfo=timezone.utc)
            return (auth, ts)

        sorted_records = sorted(cluster, key=sort_key, reverse=True)
        top_record = sorted_records[0]

        # 2. Company & Title: Highest authority source
        # In case top record has blank values (rare), find first non-blank
        canonical_company = top_record.company
        for r in sorted_records:
            if r.company and r.company.strip():
                canonical_company = r.company.strip()
                break

        canonical_title = top_record.title
        for r in sorted_records:
            if r.title and r.title.strip():
                canonical_title = r.title.strip()
                break

        # 3. Location fields: highest-authority non-null value
        canonical_city: Optional[str] = None
        for r in sorted_records:
            if r.city and str(r.city).strip():
                canonical_city = str(r.city).strip()
                break

        canonical_country: Optional[str] = None
        for r in sorted_records:
            if r.country and str(r.country).strip():
                canonical_country = str(r.country).strip()
                break

        canonical_remote: Optional[RemotePolicyEnum] = None
        for r in sorted_records:
            if r.remote_policy is not None:
                canonical_remote = r.remote_policy
                break

        # 4. Seniority & Employment Type & Visa: highest-authority non-null value
        canonical_seniority: Optional[SeniorityEnum] = None
        for r in sorted_records:
            if r.seniority is not None:
                canonical_seniority = r.seniority
                break

        canonical_employment_type: Optional[EmploymentTypeEnum] = None
        for r in sorted_records:
            if r.employment_type is not None:
                canonical_employment_type = r.employment_type
                break

        canonical_visa: Optional[bool] = None
        for r in sorted_records:
            if r.visa_sponsorship is not None:
                canonical_visa = r.visa_sponsorship
                break

        # 5. Salary: Non-null takes priority over null. Highest-authority with salary wins.
        canonical_salary_min: Optional[float] = None
        canonical_salary_max: Optional[float] = None
        canonical_currency: Optional[str] = None

        salary_records = [
            r for r in sorted_records
            if r.salary_min is not None or r.salary_max is not None
        ]
        if salary_records:
            primary_salary_rec = salary_records[0]
            canonical_salary_min = primary_salary_rec.salary_min
            canonical_salary_max = primary_salary_rec.salary_max
            canonical_currency = primary_salary_rec.currency or "USD"

        # 6. Skills: Case-insensitive set union across all records, Title-cased/standardized
        seen_skills: Dict[str, str] = {}
        for r in sorted_records:
            if r.skills:
                for skill_raw in r.skills:
                    formatted = format_skill(skill_raw)
                    if formatted:
                        k = formatted.lower()
                        if k not in seen_skills:
                            seen_skills[k] = formatted
        canonical_skills = sorted(list(seen_skills.values()), key=str.lower)

        # 7. Evidence: Combined evidence dictionary retaining highest-authority snippet per field
        combined_evidence: Dict[str, str] = {}
        for r in sorted_records:
            if r.evidence and isinstance(r.evidence, dict):
                for field_name, snippet in r.evidence.items():
                    if snippet and str(snippet).strip() and field_name not in combined_evidence:
                        combined_evidence[field_name] = str(snippet).strip()

        # 8. Sources & URLs & Posting IDs union
        sources_list: List[str] = []
        source_urls_list: List[str] = []
        source_posting_ids: List[str] = []

        seen_sources: Set[str] = set()
        seen_urls: Set[str] = set()
        seen_pids: Set[str] = set()

        for r in sorted_records:
            # Source origin
            src_name = self.get_source(r)
            if src_name and src_name not in seen_sources:
                seen_sources.add(src_name)
                sources_list.append(src_name)

            # Posting ID
            pid = str(r.posting_id).strip()
            if pid and pid not in seen_pids:
                seen_pids.add(pid)
                source_posting_ids.append(pid)

            # Source URLs
            if r.source_urls:
                for u in r.source_urls:
                    u_str = str(u).strip()
                    if u_str and u_str not in seen_urls:
                        seen_urls.add(u_str)
                        source_urls_list.append(u_str)

        # 9. Timestamps: min first_seen, max last_seen
        first_seen_candidates: List[datetime] = []
        last_seen_candidates: List[datetime] = []

        for r in cluster:
            fs = self._to_utc_datetime(r.first_seen)
            if fs:
                first_seen_candidates.append(fs)

            # Also consider posted_date if provided
            if r.posted_date:
                pd_dt = self._to_utc_datetime(r.posted_date)
                if pd_dt:
                    first_seen_candidates.append(pd_dt)

            ls = self._to_utc_datetime(r.last_seen)
            if ls:
                last_seen_candidates.append(ls)

        now_utc = datetime.now(timezone.utc)
        first_seen = min(first_seen_candidates) if first_seen_candidates else now_utc
        last_seen = max(last_seen_candidates) if last_seen_candidates else now_utc

        # 10. Status: If any record is open, canonical status is open
        has_open = any(r.status == StatusEnum.open for r in cluster)
        canonical_status = StatusEnum.open if has_open else StatusEnum.closed

        # Original posted_date from top record if available
        canonical_posted_date: Optional[date] = None
        for r in sorted_records:
            if r.posted_date is not None:
                canonical_posted_date = r.posted_date
                break

        # 11. Deterministic Canonical ID: 64-char SHA-256 hash
        # Location string composition: city, country or remote policy
        loc_components: List[str] = []
        if canonical_city:
            loc_components.append(canonical_city)
        if canonical_country:
            loc_components.append(canonical_country)

        if loc_components:
            primary_location = ", ".join(loc_components)
        elif canonical_remote:
            primary_location = canonical_remote.value if hasattr(canonical_remote, "value") else str(canonical_remote)
        else:
            primary_location = ""

        canonical_id = generate_canonical_id(
            company=canonical_company,
            title=canonical_title,
            location=primary_location,
        )

        # Construct and return model
        return CanonicalJobPosting(
            canonical_id=canonical_id,
            company=canonical_company,
            title=canonical_title,
            seniority=canonical_seniority,
            city=canonical_city,
            country=canonical_country,
            remote_policy=canonical_remote,
            salary_min=canonical_salary_min,
            salary_max=canonical_salary_max,
            currency=canonical_currency,
            employment_type=canonical_employment_type,
            skills=canonical_skills,
            visa_sponsorship=canonical_visa,
            sources=sources_list,
            source_urls=source_urls_list,
            source_posting_ids=source_posting_ids,
            evidence=combined_evidence,
            first_seen=first_seen,
            last_seen=last_seen,
            duplicate_count=len(cluster),
            status=canonical_status,
            posted_date=canonical_posted_date,
        )
