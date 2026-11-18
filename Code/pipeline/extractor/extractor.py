"""Core Job Extractor Pipeline for the Twig Ingestion and Extraction System.

Orchestrates the Phase 2 LLM extraction pipeline:
1. Content Preprocessing (HTML boilerplate stripping, whitespace normalization).
2. Extraction Prompt Construction (schema, few-shots, and source metadata).
3. LLM Invocation & Structured JSON Extraction.
4. Anti-Hallucination Guardrails (verbatim evidence verification & ungrounded field nulling).
5. Deterministic ID Generation (SHA-256 hash of company, title, and location).
6. Schema Validation into strongly-typed JobPostingRecord instances.
7. Token and Cost Accounting.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from bs4 import BeautifulSoup

from pipeline.extractor.llm_client import LLMClient
from pipeline.extractor.prompts import build_extraction_prompt
from pipeline.models.schema import (
    EmploymentTypeEnum,
    JobPostingRecord,
    RawExtractionOutput,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
)
from pipeline.models.validators import (
    FIELD_EVIDENCE_ALIASES,
    normalize_annual_salary,
    validate_evidence_in_text,
)
from pipeline.storage.raw_cache import RawCache

logger = logging.getLogger(__name__)


# ==============================================================================
# Job Content Preprocessor
# ==============================================================================


class JobContentPreprocessor:
    """Preprocesses and sanitizes raw job posting descriptions.

    Strips boilerplate HTML tags, navigation, scripts, and styles, and normalizes
    excessive whitespace to reduce token count and LLM processing costs.
    """

    # HTML tags that never contain useful job description text
    STRIP_TAGS: Tuple[str, ...] = (
        "script",
        "style",
        "nav",
        "header",
        "footer",
        "aside",
        "noscript",
        "svg",
        "iframe",
        "button",
        "form",
    )

    def clean(self, raw_content: Optional[str]) -> str:
        """Sanitize raw HTML or messy free-form text.

        Args:
            raw_content: Raw HTML, markdown, or free-form text.

        Returns:
            Sanitized, compact plaintext suitable for LLM extraction prompts.
        """
        if not raw_content:
            return ""

        text = str(raw_content)

        # Detect and clean HTML tags if present
        if "<" in text and ">" in text:
            try:
                soup = BeautifulSoup(text, "html.parser")

                # Remove non-content elements
                for tag in soup.find_all(self.STRIP_TAGS):
                    tag.decompose()

                # Extract readable text with newline separators
                text = soup.get_text(separator="\n")
            except Exception as err:
                logger.debug("BeautifulSoup parser fallback: %s", err)
                # Regex fallback to remove basic HTML tags
                text = re.sub(r"<[^>]+>", " ", text)

        # Normalize unicode spaces and horizontal whitespace
        text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+", " ", text)

        # Collapse excessive consecutive newlines (max 2)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)

        return text.strip()

    def preprocess(self, raw_content: Optional[str]) -> str:
        """Alias for clean() to adhere to diverse pipeline naming conventions."""
        return self.clean(raw_content)

    def clean_html(self, raw_content: Optional[str]) -> str:
        """Alias for clean() specifically targeting HTML content."""
        return self.clean(raw_content)

    def __call__(self, raw_content: Optional[str]) -> str:
        """Callable interface for functional pipeline stages."""
        return self.clean(raw_content)


# ==============================================================================
# Core Job Extractor Pipeline
# ==============================================================================


class JobExtractor:
    """Core job posting extractor pipeline.

    Coordinates preprocessing, LLM invocation, verbatim evidence verification,
    anti-hallucination sanitization, deterministic ID generation, and Pydantic
    schema validation.
    """

    # Fields requiring explicit verbatim evidence justification when non-null
    AUDITABLE_FIELDS: Tuple[str, ...] = (
        "seniority",
        "remote_policy",
        "salary_min",
        "salary_max",
        "currency",
        "city",
        "country",
        "employment_type",
        "visa_sponsorship",
        "skills",
        "posted_date",
    )

    def __init__(
        self,
        preprocessor: Optional[JobContentPreprocessor] = None,
        llm_client: Optional[LLMClient] = None,
        cache: Optional[RawCache] = None,
    ) -> None:
        """Initialize the JobExtractor pipeline.

        Args:
            preprocessor: Preprocessor instance (defaults to JobContentPreprocessor).
            llm_client: LLMClient instance (defaults to LLMClient in auto-detect/mock mode).
            cache: Optional persistent RawCache instance for raw payload access.
        """
        self.preprocessor = preprocessor or JobContentPreprocessor()
        self.llm_client = llm_client or LLMClient()
        self.cache = cache

        # Performance, token, and cost accounting metrics
        self.total_extractions: int = 0
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_cost_usd: float = 0.0
        self.hallucination_mitigations: int = 0

    # --------------------------------------------------------------------------
    # Deterministic ID Generation
    # --------------------------------------------------------------------------

    @staticmethod
    def generate_posting_id(company: str, title: str, location: str = "") -> str:
        """Generates a stable, deterministic SHA-256 posting ID across pipeline runs.

        Normalizes casing and whitespace so identical job postings from repeated
        scrapes produce identical IDs.

        Args:
            company: Company name.
            title: Job title.
            location: Optional location or remote policy string for disambiguation.

        Returns:
            64-character SHA-256 hexadecimal hash string.
        """
        c = (company or "").strip().lower()
        t = (title or "").strip().lower()
        loc = (location or "").strip().lower()
        composite = f"{c}::{t}::{loc}".encode("utf-8")
        return hashlib.sha256(composite).hexdigest()

    # --------------------------------------------------------------------------
    # Verbatim Evidence Verification
    # --------------------------------------------------------------------------

    @staticmethod
    def verify_evidence(source_text: str, evidence_map: Dict[str, str]) -> Dict[str, bool]:
        """Programmatically verifies whether each text snippet in evidence_map exists verbatim

        (case-insensitive substring or normalized whitespace) in the source text.

        Args:
            source_text: Raw or sanitized job posting text.
            evidence_map: Dictionary mapping field names to literal text snippets.

        Returns:
            Dictionary mapping field names to boolean verification outcomes.
        """
        if not evidence_map:
            return {}

        results: Dict[str, bool] = {}
        if not source_text:
            for field in evidence_map:
                results[field] = False
            return results

        raw_lower = source_text.lower()
        norm_source = " ".join(raw_lower.split())

        for field, snippet in evidence_map.items():
            if snippet is None:
                results[field] = False
                continue

            snip_str = str(snippet).strip()
            if not snip_str:
                results[field] = False
                continue

            snip_lower = snip_str.lower()
            norm_snip = " ".join(snip_lower.split())

            # 1. Direct case-insensitive substring check
            # 2. Whitespace-normalized substring check (newlines/spaces collapsed)
            # 3. Model validator robust token overlap check
            is_verified = (
                snip_lower in raw_lower
                or norm_snip in norm_source
                or validate_evidence_in_text(snip_str, source_text, case_sensitive=False)
            )
            results[field] = bool(is_verified)

        return results

    # --------------------------------------------------------------------------
    # Anti-Hallucination Guardrails
    # --------------------------------------------------------------------------

    def sanitize_hallucinations(
        self,
        extracted_data: Dict[str, Any],
        source_text: str,
    ) -> Dict[str, Any]:
        """Anti-Hallucination Guard: Validates that non-null extracted fields are backed

        by verbatim evidence snippets in the source text.

        If a field was extracted as non-null by the LLM, BUT its evidence snippet is
        missing or fails verbatim verification against the source text:
        - Overwrites the field to None (or empty list for skills)!
        - Removes or invalidates the ungrounded snippet in evidence.
        - Logs a structured warning.
        - Increments the hallucination mitigation counter.

        Args:
            extracted_data: Raw dictionary extracted from LLM.
            source_text: Full raw or sanitized source text of the posting.

        Returns:
            Sanitized dictionary guaranteed to have no hallucinated fields.
        """
        data = dict(extracted_data)
        evidence = dict(data.get("evidence") or {})
        data["evidence"] = evidence

        # Pre-verify all present evidence snippets against source text
        verified_evidence = self.verify_evidence(source_text, evidence)

        def _is_snippet_verified(field_name: str) -> bool:
            """Check if field or any of its approved aliases has a verified snippet."""
            # 1. Exact field match in evidence
            if field_name in evidence:
                if verified_evidence.get(field_name, False):
                    return True
                return False

            # 2. Check approved aliases (e.g. 'salary' for 'salary_min' / 'salary_max')
            aliases = FIELD_EVIDENCE_ALIASES.get(field_name, [])
            for alias in aliases:
                if alias in evidence:
                    if verified_evidence.get(alias, False):
                        return True
                    return False

            return False

        # Identify all candidate fields to audit
        # Exclude internal/metadata fields that do not come from job description text
        skip_fields = {
            "evidence",
            "posting_id",
            "source_urls",
            "first_seen",
            "last_seen",
            "status",
            "raw_html",
            "raw_payload",
            "metadata",
            "salary_period",
            "url",
            "source",
        }

        # Check standard auditable fields as well as any other fields in extracted_data
        fields_to_check = set(self.AUDITABLE_FIELDS).union(
            k for k in data.keys() if k not in skip_fields and not k.startswith("_")
        )

        for field in sorted(fields_to_check):
            val = data.get(field)

            # Null, None, or empty values do not constitute hallucinations
            if val is None:
                continue
            if isinstance(val, (list, dict, set)) and len(val) == 0:
                continue

            # Company and Title are often supplied via source metadata (e.g. ATS API endpoint)
            # Only audit company/title if an explicit evidence snippet was provided for them
            if field in ("company", "title") and field not in evidence:
                continue

            # Verify whether a valid, verified evidence snippet justifies this field
            if not _is_snippet_verified(field):
                # Hallucination detected! Overwrite field to None (or empty list for collections)
                if isinstance(val, list):
                    data[field] = []
                else:
                    data[field] = None

                # Remove unverified snippet from evidence
                evidence.pop(field, None)
                for alias in FIELD_EVIDENCE_ALIASES.get(field, []):
                    if alias in evidence and not verified_evidence.get(alias, False):
                        evidence.pop(alias, None)

                # Log structured warning conforming to specification
                logger.warning(
                    "Hallucination detected on field '%s': snippet not in source text. Set to None.",
                    field,
                )
                self.hallucination_mitigations += 1

        # Strict Null Rule for Currency:
        # If both salary_min and salary_max are None, currency must also be None
        if data.get("salary_min") is None and data.get("salary_max") is None and data.get("currency") is not None:
            logger.warning(
                "Hallucination detected on field 'currency': compensation numbers not present. Set to None."
            )
            data["currency"] = None
            evidence.pop("currency", None)
            self.hallucination_mitigations += 1

        # Remove any lingering unverified snippets from evidence dictionary
        for snip_key in list(evidence.keys()):
            if not verified_evidence.get(snip_key, False):
                evidence.pop(snip_key, None)

        return data

    # --------------------------------------------------------------------------
    # End-to-End Extraction Pipeline
    # --------------------------------------------------------------------------

    def extract_posting(self, raw_posting: Dict[str, Any]) -> JobPostingRecord:
        """Extract a structured, validated JobPostingRecord from a raw posting payload.

        Pipeline Stages:
        - Step 1: Preprocess raw HTML / free text with JobContentPreprocessor.
        - Step 2: Build extraction prompt with source metadata (company, title, url).
        - Step 3: Invoke LLMClient.extract(...) to obtain structured JSON.
        - Step 4: Run sanitize_hallucinations(...) with verbatim evidence verification.
        - Step 5: Generate stable deterministic posting_id (SHA-256 of company + title + location).
        - Step 6: Validate and return strongly-typed JobPostingRecord model instance.

        Args:
            raw_posting: Raw posting dictionary containing HTML or text description
                         and optional metadata (e.g. company, title, url).

        Returns:
            Validated JobPostingRecord instance conforming to target schema.
        """
        # Step 1: Preprocesses raw HTML / free text with JobContentPreprocessor
        raw_content = (
            raw_posting.get("content")
            or raw_posting.get("payload")
            or raw_posting.get("description")
            or raw_posting.get("text")
            or raw_posting.get("raw_html")
            or raw_posting.get("body")
            or ""
        )
        cleaned_text = self.preprocessor.clean(raw_content)

        # Step 2: Builds extraction prompt with source metadata (company, title, url)
        metadata = {
            "company": raw_posting.get("company"),
            "title": raw_posting.get("title"),
            "url": raw_posting.get("url") or raw_posting.get("source_url"),
            "location": raw_posting.get("location"),
            "source": raw_posting.get("source"),
        }
        filtered_metadata = {k: v for k, v in metadata.items() if v is not None}

        prompt = build_extraction_prompt(
            clean_job_text=cleaned_text,
            source_metadata=filtered_metadata,
        )

        # Step 3: Invokes LLMClient.extract(...) to obtain structured JSON
        extracted_data, call_metrics = self._invoke_llm(
            prompt=prompt,
            cleaned_text=cleaned_text,
            metadata=filtered_metadata,
        )

        # Update session token and cost accounting
        self._record_metrics(call_metrics, prompt, extracted_data)

        # Step 4: Runs sanitize_hallucinations(...)
        # Verify evidence against both raw content and cleaned text
        source_verification_corpus = f"{raw_content}\n{cleaned_text}"
        sanitized_data = self.sanitize_hallucinations(
            extracted_data=extracted_data,
            source_text=source_verification_corpus,
        )

        # Resolve final company, title, and location (prioritizing metadata fallback)
        final_company = sanitized_data.get("company") or metadata.get("company") or "Unknown"
        final_title = sanitized_data.get("title") or metadata.get("title") or "Unknown"

        # Determine location string for deterministic hashing
        loc_parts: List[str] = []
        if sanitized_data.get("city"):
            loc_parts.append(str(sanitized_data["city"]))
        if sanitized_data.get("country"):
            loc_parts.append(str(sanitized_data["country"]))
        if not loc_parts and metadata.get("location"):
            loc_parts.append(str(metadata["location"]))
        if not loc_parts and sanitized_data.get("remote_policy"):
            loc_parts.append(str(sanitized_data["remote_policy"]))
        location_str = ", ".join(loc_parts)

        # Step 5: Generates stable deterministic posting_id (SHA-256 hash of company + title + location)
        posting_id = self.generate_posting_id(
            company=final_company,
            title=final_title,
            location=location_str,
        )

        # Step 6: Validates and returns a strongly-typed JobPostingRecord model instance
        now = datetime.now(timezone.utc)
        first_seen = raw_posting.get("first_seen") or sanitized_data.get("first_seen") or now
        last_seen = raw_posting.get("last_seen") or sanitized_data.get("last_seen") or now

        # Source URLs list
        source_url = metadata.get("url")
        source_urls = sanitized_data.get("source_urls") or []
        if not source_urls:
            if source_url:
                source_urls = [source_url]
            elif raw_posting.get("source_urls"):
                source_urls = list(raw_posting["source_urls"])

        # Handle salary annualization if salary_period was extracted
        sal_min = sanitized_data.get("salary_min")
        sal_max = sanitized_data.get("salary_max")
        sal_period = sanitized_data.get("salary_period")
        if sal_period:
            if sal_min is not None:
                sal_min = normalize_annual_salary(sal_min, sal_period)
            if sal_max is not None:
                sal_max = normalize_annual_salary(sal_max, sal_period)

        record = JobPostingRecord(
            posting_id=posting_id,
            company=final_company,
            title=final_title,
            seniority=sanitized_data.get("seniority"),
            city=sanitized_data.get("city"),
            country=sanitized_data.get("country"),
            remote_policy=sanitized_data.get("remote_policy"),
            salary_min=sal_min,
            salary_max=sal_max,
            currency=sanitized_data.get("currency"),
            employment_type=sanitized_data.get("employment_type"),
            skills=sanitized_data.get("skills") or [],
            visa_sponsorship=sanitized_data.get("visa_sponsorship"),
            posted_date=sanitized_data.get("posted_date"),
            status=sanitized_data.get("status") or StatusEnum.open,
            source_urls=source_urls,
            evidence=sanitized_data.get("evidence") or {},
            first_seen=first_seen,
            last_seen=last_seen,
        )

        # Optional caching of raw posting if cache is configured and not yet present
        if self.cache is not None and source_url and raw_content:
            try:
                if not self.cache.has(posting_id):
                    self.cache.save(
                        source=metadata.get("source") or "extractor",
                        url=source_url,
                        payload=raw_content,
                    )
            except Exception as err:
                logger.debug("Optional cache write omitted: %s", err)

        return record

    # --------------------------------------------------------------------------
    # LLM Invocation & Parsing Helper
    # --------------------------------------------------------------------------

    def _invoke_llm(
        self,
        prompt: str,
        cleaned_text: str,
        metadata: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """Invoke the configured LLMClient, supporting extract() or extract_job_data()."""
        call_metrics: Optional[Dict[str, Any]] = None

        if hasattr(self.llm_client, "extract"):
            try:
                raw_result = self.llm_client.extract(prompt)
            except TypeError:
                try:
                    raw_result = self.llm_client.extract(
                        clean_job_text=cleaned_text,
                        source_metadata=metadata,
                    )
                except TypeError:
                    raw_result = self.llm_client.extract(cleaned_text)
        elif hasattr(self.llm_client, "extract_job_data"):
            raw_result, call_metrics = self.llm_client.extract_job_data(
                clean_job_text=cleaned_text,
                source_metadata=metadata,
            )
        else:
            raise AttributeError("LLMClient has neither 'extract' nor 'extract_job_data' method")

        # Normalize return value into a dictionary
        if isinstance(raw_result, tuple):
            extracted = raw_result[0]
            if len(raw_result) > 1 and isinstance(raw_result[1], dict):
                call_metrics = raw_result[1]
        elif isinstance(raw_result, str):
            extracted = LLMClient.extract_json_from_response(raw_result)
        elif hasattr(raw_result, "model_dump"):
            extracted = raw_result.model_dump()
        elif isinstance(raw_result, dict):
            extracted = raw_result
        else:
            extracted = dict(raw_result)

        return extracted, call_metrics

    def _record_metrics(
        self,
        call_metrics: Optional[Dict[str, Any]],
        prompt: str,
        extracted_data: Dict[str, Any],
    ) -> None:
        """Update cumulative session token counters and cost accounting."""
        self.total_extractions += 1

        session_calls = getattr(self.llm_client, "session_call_count", None)
        if call_metrics and isinstance(call_metrics, dict):
            self.total_prompt_tokens += int(call_metrics.get("prompt_tokens", 0))
            self.total_completion_tokens += int(call_metrics.get("completion_tokens", 0))
            self.total_cost_usd += float(call_metrics.get("cost_usd", 0.0))
        elif isinstance(session_calls, int) and session_calls > 0:
            # Sync with LLMClient session stats
            stats = self.llm_client.get_session_stats()
            if isinstance(stats, dict):
                self.total_prompt_tokens = int(stats.get("total_prompt_tokens", self.total_prompt_tokens))
                self.total_completion_tokens = int(stats.get("total_completion_tokens", self.total_completion_tokens))
                self.total_cost_usd = float(stats.get("total_cost_usd", self.total_cost_usd))
        else:
            # Fallback character-based token estimation (1 token ~ 4 characters)
            est_prompt = max(1, len(prompt) // 4)
            est_completion = max(1, len(json.dumps(extracted_data, default=str)) // 4)
            est_cost = (est_prompt * 0.075 + est_completion * 0.30) / 1_000_000.0
            self.total_prompt_tokens += est_prompt
            self.total_completion_tokens += est_completion
            self.total_cost_usd += est_cost

    # --------------------------------------------------------------------------
    # Cost & Accounting Summary
    # --------------------------------------------------------------------------

    def get_cost_summary(self) -> Dict[str, Any]:
        """Returns total extractions, total prompt tokens, total completion tokens,

        average cost per posting ($), and hallucination mitigation count.

        Returns:
            Dictionary matching the required cost summary specification.
        """
        avg_cost = (
            self.total_cost_usd / self.total_extractions
            if self.total_extractions > 0
            else 0.0
        )
        return {
            "total_extractions": self.total_extractions,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "average_cost_per_posting": round(avg_cost, 6),
            "avg_cost_per_posting": round(avg_cost, 6),
            "hallucination_mitigations": self.hallucination_mitigations,
            "hallucination_mitigation_count": self.hallucination_mitigations,
        }

    def reset_cost_summary(self) -> None:
        """Reset extraction counters and cost metrics to zero."""
        self.total_extractions = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cost_usd = 0.0
        self.hallucination_mitigations = 0
        if hasattr(self.llm_client, "reset_session_stats"):
            self.llm_client.reset_session_stats()
