"""Batch Extraction Runner for the Twig Data Ingestion Pipeline.

Orchestrates multi-threaded batch LLM extraction across candidate job postings
with controlled concurrency, fail-soft resilience, SQLite persistence for Phase 3
deduplication, and comprehensive data quality metrics reporting.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from pipeline.config import DB_PATH
from pipeline.models.schema import (
    EmploymentTypeEnum,
    JobPostingRecord,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
    generate_deterministic_id,
)

logger = logging.getLogger(__name__)


class FallbackJobExtractor:
    """Zero-dependency fallback extractor for offline testing or when JobExtractor is pending.

    Integrates directly with LLMClient in mock or provider mode to extract structured
    job details, sanitize fields, and produce compliant JobPostingRecord objects.
    """

    def __init__(self, mock: bool = True, llm_client: Optional[Any] = None) -> None:
        """Initialize fallback extractor."""
        self.mock = mock
        if llm_client is not None:
            self.llm_client = llm_client
        else:
            from pipeline.extractor.llm_client import LLMClient

            self.llm_client = LLMClient(mock=mock)

    def extract_posting(self, raw_posting: Dict[str, Any]) -> JobPostingRecord:
        """Extract a structured JobPostingRecord from a raw candidate posting."""
        company = raw_posting.get("company") or raw_posting.get("company_name") or "Unknown"
        title = raw_posting.get("title") or raw_posting.get("job_title") or "Untitled Role"
        url = raw_posting.get("url") or raw_posting.get("absolute_url") or ""

        # Extract text payload
        content = (
            raw_posting.get("content")
            or raw_posting.get("description_raw")
            or raw_posting.get("raw_payload")
            or raw_posting.get("snippet")
            or ""
        )
        if isinstance(content, dict):
            content = json.dumps(content)

        clean_text = str(content).strip() or f"{title} at {company}"

        source_meta = {
            "company": company,
            "title": title,
            "url": url,
            "source": raw_posting.get("source", "raw"),
        }

        # Invoke LLMClient
        extracted_dict, call_metrics = self.llm_client.extract_job_data(
            clean_job_text=clean_text,
            source_metadata=source_meta,
        )

        # Build posting ID
        loc_str = extracted_dict.get("city") or extracted_dict.get("country") or url
        posting_id = generate_deterministic_id(company, title, loc_str)

        # Enforce source URL
        source_urls = [url] if url else []
        if "source_urls" in extracted_dict and isinstance(extracted_dict["source_urls"], list):
            for u in extracted_dict["source_urls"]:
                if u and u not in source_urls:
                    source_urls.append(u)

        now = datetime.now(timezone.utc)
        record = JobPostingRecord(
            posting_id=posting_id,
            company=extracted_dict.get("company") or company,
            title=extracted_dict.get("title") or title,
            seniority=extracted_dict.get("seniority"),
            city=extracted_dict.get("city"),
            country=extracted_dict.get("country"),
            remote_policy=extracted_dict.get("remote_policy"),
            salary_min=extracted_dict.get("salary_min"),
            salary_max=extracted_dict.get("salary_max"),
            currency=extracted_dict.get("currency"),
            employment_type=extracted_dict.get("employment_type"),
            skills=extracted_dict.get("skills", []),
            visa_sponsorship=extracted_dict.get("visa_sponsorship"),
            posted_date=extracted_dict.get("posted_date"),
            status=StatusEnum.open,
            source_urls=source_urls,
            evidence=extracted_dict.get("evidence", {}),
            first_seen=now,
            last_seen=now,
        )
        return record

    def get_cost_summary(self) -> Dict[str, Any]:
        """Return token and cost statistics from underlying LLM client."""
        if hasattr(self.llm_client, "get_session_stats"):
            return self.llm_client.get_session_stats()
        return {}


class BatchExtractor:
    """Thread-safe batch runner for LLM-based job posting extraction.

    Provides high-throughput parallel extractions with ThreadPoolExecutor,
    fail-soft resilience per posting, SQLite persistence for Phase 3 deduplication,
    and comprehensive data quality metrics reporting.
    """

    def __init__(
        self,
        extractor: Optional[Any] = None,
        mock: bool = False,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> None:
        """Initialize the BatchExtractor.

        Args:
            extractor: Optional custom JobExtractor instance. If omitted, attempts
                       to load `JobExtractor` from `pipeline.extractor.extractor`
                       with fallback to `FallbackJobExtractor`.
            mock: If True and extractor is omitted, initializes in offline mock mode.
            provider: Optional LLM provider ('gemini', 'openai', 'mock').
            model: Optional LLM model name (e.g. 'gemini-1.5-flash', 'gemini-4-argon').
            db_path: Path to the SQLite database. Defaults to pipeline.config.DB_PATH.
        """
        self.mock = mock
        self.provider = provider
        self.model = model
        self.db_path = str(db_path or DB_PATH)
        self._lock = threading.Lock()

        # Batch state tracking
        self.successful_extractions: List[JobPostingRecord] = []
        self.failed_extractions: List[Dict[str, Any]] = []
        self.total_processed: int = 0
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None

        # Resolve or initialize JobExtractor
        if extractor is not None:
            self.extractor = extractor
        else:
            self.extractor = self._init_default_extractor(mock=mock, provider=provider, model=model)

    def _init_default_extractor(
        self, mock: bool = False, provider: Optional[str] = None, model: Optional[str] = None
    ) -> Any:
        """Initialize the core JobExtractor with graceful fallback."""
        try:
            from pipeline.extractor.extractor import JobExtractor
            from pipeline.extractor.llm_client import LLMClient

            logger.info(
                "Initializing standard JobExtractor with LLMClient (mock=%s, provider=%s, model=%s)",
                mock,
                provider,
                model,
            )
            client = LLMClient(mock=mock, provider=provider, model=model)
            return JobExtractor(llm_client=client)
        except Exception as exc:
            logger.warning(
                "Could not load JobExtractor from pipeline.extractor.extractor (%s). "
                "Using FallbackJobExtractor with LLMClient.",
                exc,
            )
            from pipeline.extractor.llm_client import LLMClient
            client = LLMClient(mock=mock, provider=provider, model=model)
            return FallbackJobExtractor(mock=mock, llm_client=client)

    def reset_metrics(self) -> None:
        """Reset internal metrics counters and extraction buffers."""
        with self._lock:
            self.successful_extractions.clear()
            self.failed_extractions.clear()
            self.total_processed = 0
            self.start_time = None
            self.end_time = None

    def _process_single(
        self,
        idx: int,
        posting: Dict[str, Any],
        total: int,
    ) -> Optional[JobPostingRecord]:
        """Extract a single posting with fail-soft isolation.

        Guarantees that an exception on a single posting never crashes the batch.

        Args:
            idx: Zero-based index of posting in batch.
            posting: Raw posting dictionary.
            total: Total number of postings in batch.

        Returns:
            Extracted JobPostingRecord on success, None on error.
        """
        company = posting.get("company") or posting.get("company_name") or "Unknown"
        title = posting.get("title") or posting.get("job_title") or "Untitled"

        logger.debug(
            "[BatchRunner] Processing posting %d/%d: %s - %s",
            idx + 1,
            total,
            company,
            title,
        )

        try:
            record: Any = self.extractor.extract_posting(posting)

            # Ensure strongly-typed JobPostingRecord model
            if not isinstance(record, JobPostingRecord):
                if isinstance(record, dict):
                    record = JobPostingRecord.model_validate(record)
                else:
                    raise TypeError(f"Extractor returned unsupported type: {type(record)}")

            with self._lock:
                self.successful_extractions.append(record)
                self.total_processed += 1

            logger.info(
                "[BatchRunner] [%d/%d] SUCCESS: %s - %s (ID: %s)",
                idx + 1,
                total,
                record.company,
                record.title,
                record.posting_id,
            )
            return record

        except Exception as exc:
            with self._lock:
                self.failed_extractions.append({
                    "index": idx,
                    "company": company,
                    "title": title,
                    "error": str(exc),
                    "exception_type": type(exc).__name__,
                    "posting_snippet": str(posting)[:200],
                })
                self.total_processed += 1

            logger.error(
                "[BatchRunner] [%d/%d] FAILED: %s - %s: %s",
                idx + 1,
                total,
                company,
                title,
                exc,
                exc_info=True,
            )
            return None

    def run_batch(
        self,
        postings: List[Dict[str, Any]],
        max_concurrency: int = 5,
    ) -> List[JobPostingRecord]:
        """Execute concurrent extraction across candidate postings.

        Thread-safe execution using concurrent.futures.ThreadPoolExecutor with
        fail-soft resilience and guaranteed progress tracking.

        Args:
            postings: List of candidate job posting dictionaries.
            max_concurrency: Maximum worker threads (default 5).

        Returns:
            List of successfully extracted JobPostingRecord instances.
        """
        if not postings:
            logger.warning("[BatchRunner] Empty postings list supplied to run_batch.")
            return []

        total = len(postings)
        concurrency = max(1, int(max_concurrency))
        logger.info(
            "[BatchRunner] Starting batch extraction for %d postings with concurrency=%d",
            total,
            concurrency,
        )

        self.start_time = time.time()
        ordered_results: List[Optional[JobPostingRecord]] = [None] * total

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            future_to_idx = {
                executor.submit(self._process_single, idx, posting, total): idx
                for idx, posting in enumerate(postings)
            }

            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    res = future.result()
                    ordered_results[idx] = res
                except Exception as exc:
                    logger.error("[BatchRunner] Uncaught future exception on item %d: %s", idx, exc)

        self.end_time = time.time()
        elapsed = self.end_time - self.start_time

        successful_records = [r for r in ordered_results if r is not None]
        success_rate = (len(successful_records) / total * 100.0) if total > 0 else 0.0

        logger.info(
            "[BatchRunner] Completed batch: %d/%d succeeded (%.1f%%) in %.2f seconds (%.1f jobs/sec)",
            len(successful_records),
            total,
            success_rate,
            elapsed,
            (total / elapsed) if elapsed > 0 else 0.0,
        )

        return successful_records

    def save_extracted_records(
        self,
        records: List[Union[JobPostingRecord, Dict[str, Any]]],
        db_path: Optional[str] = None,
        table_name: str = "extracted_postings",
    ) -> int:
        """Persist extracted records into SQLite table for Phase 3 deduplication.

        Idempotent batch insertion using SQLite INSERT OR REPLACE within a transaction.

        Args:
            records: List of JobPostingRecord objects or model dictionaries.
            db_path: Optional SQLite database path override.
            table_name: Target SQLite table name (default 'extracted_postings').

        Returns:
            Number of records successfully saved.
        """
        if not records:
            return 0

        target_db = db_path or self.db_path
        is_memory = target_db == ":memory:"

        if not is_memory:
            Path(target_db).parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(target_db, timeout=30.0)
        try:
            with conn:
                # 1. Initialize schema and indexes
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table_name} (
                        posting_id TEXT PRIMARY KEY,
                        company TEXT NOT NULL,
                        title TEXT NOT NULL,
                        seniority TEXT,
                        city TEXT,
                        country TEXT,
                        remote_policy TEXT,
                        salary_min REAL,
                        salary_max REAL,
                        currency TEXT,
                        employment_type TEXT,
                        skills TEXT NOT NULL,
                        visa_sponsorship INTEGER,
                        posted_date TEXT,
                        status TEXT DEFAULT 'open',
                        source_urls TEXT NOT NULL,
                        evidence TEXT NOT NULL,
                        first_seen TIMESTAMP NOT NULL,
                        last_seen TIMESTAMP NOT NULL,
                        extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_company ON {table_name} (company)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_title ON {table_name} (title)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_remote ON {table_name} (remote_policy)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_status ON {table_name} (status)"
                )

                # 2. Prepare parameter tuples
                rows: List[Tuple[Any, ...]] = []
                for rec in records:
                    if isinstance(rec, JobPostingRecord):
                        data = rec.model_dump()
                    elif isinstance(rec, dict):
                        data = rec
                    else:
                        data = getattr(rec, "__dict__", {})

                    # Format Enum values to string
                    seniority_val = data.get("seniority")
                    if hasattr(seniority_val, "value"):
                        seniority_val = seniority_val.value

                    remote_val = data.get("remote_policy")
                    if hasattr(remote_val, "value"):
                        remote_val = remote_val.value

                    emp_val = data.get("employment_type")
                    if hasattr(emp_val, "value"):
                        emp_val = emp_val.value

                    status_val = data.get("status")
                    if hasattr(status_val, "value"):
                        status_val = status_val.value
                    elif status_val is None:
                        status_val = "open"

                    # Format boolean visa_sponsorship
                    visa_val = data.get("visa_sponsorship")
                    if visa_val is True:
                        visa_int = 1
                    elif visa_val is False:
                        visa_int = 0
                    else:
                        visa_int = None

                    # Format date fields
                    posted_date_val = data.get("posted_date")
                    if hasattr(posted_date_val, "isoformat"):
                        posted_date_str = posted_date_val.isoformat()
                    elif posted_date_val:
                        posted_date_str = str(posted_date_val)
                    else:
                        posted_date_str = None

                    first_seen_val = data.get("first_seen")
                    if hasattr(first_seen_val, "isoformat"):
                        first_seen_str = first_seen_val.isoformat()
                    else:
                        first_seen_str = str(first_seen_val or datetime.now(timezone.utc).isoformat())

                    last_seen_val = data.get("last_seen")
                    if hasattr(last_seen_val, "isoformat"):
                        last_seen_str = last_seen_val.isoformat()
                    else:
                        last_seen_str = str(last_seen_val or datetime.now(timezone.utc).isoformat())

                    # Serialize collections to JSON
                    skills_json = json.dumps(data.get("skills") or [], ensure_ascii=False)
                    sources_json = json.dumps(data.get("source_urls") or [], ensure_ascii=False)
                    evidence_json = json.dumps(data.get("evidence") or {}, ensure_ascii=False)

                    rows.append((
                        data.get("posting_id"),
                        data.get("company"),
                        data.get("title"),
                        seniority_val,
                        data.get("city"),
                        data.get("country"),
                        remote_val,
                        data.get("salary_min"),
                        data.get("salary_max"),
                        data.get("currency"),
                        emp_val,
                        skills_json,
                        visa_int,
                        posted_date_str,
                        status_val,
                        sources_json,
                        evidence_json,
                        first_seen_str,
                        last_seen_str,
                    ))

                # 3. High-throughput batch insertion
                conn.executemany(
                    f"""
                    INSERT OR REPLACE INTO {table_name} (
                        posting_id, company, title, seniority, city, country,
                        remote_policy, salary_min, salary_max, currency,
                        employment_type, skills, visa_sponsorship, posted_date,
                        status, source_urls, evidence, first_seen, last_seen
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            logger.info("Saved %d extracted postings into table '%s'", len(rows), table_name)
            return len(rows)
        finally:
            conn.close()

    def generate_metrics_report(self) -> Dict[str, Any]:
        """Compute end-to-end data quality and performance metrics.

        Computes:
        - total processed, total successful, total failed
        - success rate (%)
        - average cost per posting ($)
        - percentage of postings with non-null salary
        - percentage of postings with non-null remote policy
        - percentage of postings with verified evidence
        - token accounting summary
        - throughput (jobs/sec)

        Returns:
            Dictionary containing computed metric keys.
        """
        with self._lock:
            successful = list(self.successful_extractions)
            failed = list(self.failed_extractions)

        total_succ = len(successful)
        total_fail = len(failed)
        total_proc = total_succ + total_fail

        success_rate = (total_succ / total_proc * 100.0) if total_proc > 0 else 0.0

        # Field coverage calculations over successful extractions
        with_salary = sum(
            1 for r in successful
            if r.salary_min is not None or r.salary_max is not None
        )
        pct_salary = (with_salary / total_succ * 100.0) if total_succ > 0 else 0.0

        with_remote = sum(
            1 for r in successful
            if r.remote_policy is not None
        )
        pct_remote = (with_remote / total_succ * 100.0) if total_succ > 0 else 0.0

        with_evidence = sum(
            1 for r in successful
            if r.evidence and any(bool(v and str(v).strip()) for v in r.evidence.values())
        )
        pct_evidence = (with_evidence / total_succ * 100.0) if total_succ > 0 else 0.0

        # Cost and token metrics from underlying extractor / LLM client
        total_cost_usd = 0.0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_tokens = 0
        avg_cost = 0.0

        if hasattr(self.extractor, "get_cost_summary"):
            try:
                stats = self.extractor.get_cost_summary()
                total_cost_usd = float(stats.get("total_cost_usd", 0.0))
                total_prompt_tokens = int(stats.get("total_prompt_tokens", 0))
                total_completion_tokens = int(stats.get("total_completion_tokens", 0))
                total_tokens = int(stats.get("total_tokens", total_prompt_tokens + total_completion_tokens))
                if total_succ > 0 and total_cost_usd > 0:
                    avg_cost = round(total_cost_usd / total_succ, 6)
                else:
                    avg_cost = float(stats.get("avg_cost_per_posting", 0.0))
            except Exception as exc:
                logger.debug("Failed querying cost summary from extractor: %s", exc)

        # Throughput calculations
        elapsed = 0.0
        if self.start_time and self.end_time:
            elapsed = max(0.001, self.end_time - self.start_time)
        postings_per_sec = (total_proc / elapsed) if elapsed > 0 else 0.0

        return {
            "total_processed": total_proc,
            "total_successful": total_succ,
            "total_failed": total_fail,
            "success_rate_pct": round(success_rate, 2),
            "average_cost_per_posting": round(avg_cost, 6),
            "percentage_with_salary": round(pct_salary, 2),
            "percentage_with_remote_policy": round(pct_remote, 2),
            "percentage_with_verified_evidence": round(pct_evidence, 2),
            "postings_with_salary": with_salary,
            "postings_with_remote_policy": with_remote,
            "postings_with_verified_evidence": with_evidence,
            "total_cost_usd": round(total_cost_usd, 6),
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "total_tokens": total_tokens,
            "elapsed_seconds": round(elapsed, 2),
            "postings_per_second": round(postings_per_sec, 2),
        }

    def format_ascii_metrics_table(self, report: Optional[Dict[str, Any]] = None) -> str:
        """Format an informative ASCII summary table for console output."""
        rep = report or self.generate_metrics_report()

        lines = [
            "",
            "=" * 74,
            "                 Twig Phase 2 LLM Extraction Metrics Report",
            "=" * 74,
            f"  {'Metric Category':<35} | {'Measured Value':>32}",
            "-" * 36 + "+" + "-" * 35,
            f"  {'Total Postings Processed':<35} | {rep['total_processed']:>32,}",
            f"  {'Successful Extractions':<35} | {rep['total_successful']:>32,}",
            f"  {'Failed Extractions (Fail-Soft)':<35} | {rep['total_failed']:>32,}",
            f"  {'Extraction Success Rate':<35} | {rep['success_rate_pct']:>31.2f}%",
            "-" * 36 + "+" + "-" * 35,
            f"  {'Postings with Non-Null Salary':<35} | {rep['percentage_with_salary']:>31.2f}% ({rep['postings_with_salary']}/{rep['total_successful']})",
            f"  {'Postings with Remote Policy':<35} | {rep['percentage_with_remote_policy']:>31.2f}% ({rep['postings_with_remote_policy']}/{rep['total_successful']})",
            f"  {'Postings with Verified Evidence':<35} | {rep['percentage_with_verified_evidence']:>31.2f}% ({rep['postings_with_verified_evidence']}/{rep['total_successful']})",
            "-" * 36 + "+" + "-" * 35,
            f"  {'Total Prompt Tokens Consumed':<35} | {rep['total_prompt_tokens']:>32,}",
            f"  {'Total Completion Tokens':<35} | {rep['total_completion_tokens']:>32,}",
            f"  {'Total Cost (USD)':<35} | ${rep['total_cost_usd']:>31.6f}",
            f"  {'Average Cost per Posting':<35} | ${rep['average_cost_per_posting']:>31.6f}",
            "-" * 36 + "+" + "-" * 35,
            f"  {'Batch Execution Time':<35} | {rep['elapsed_seconds']:>30.2f} sec",
            f"  {'Extraction Throughput':<35} | {rep['postings_per_second']:>26.2f} postings/s",
            "=" * 74,
            "",
        ]
        return "\n".join(lines)

    @staticmethod
    def load_postings_from_file(file_path: Union[str, Path]) -> List[Dict[str, Any]]:
        """Load candidate job postings from a JSON file.

        Args:
            file_path: Path to JSON file containing list of postings.

        Returns:
            List of parsed posting dictionaries.
        """
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Input file not found: {file_path}")

        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        elif isinstance(data, dict):
            # If wrapped in a dictionary container
            for key in ("postings", "candidates", "jobs", "data"):
                if key in data and isinstance(data[key], list):
                    return [d for d in data[key] if isinstance(d, dict)]
            return [data]
        else:
            raise ValueError(f"JSON input must be a list of objects, got {type(data)}")

    @staticmethod
    def load_postings_from_db(
        db_path: Optional[str] = None,
        source: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Load candidate postings directly from the SQLite RawCache table.

        Args:
            db_path: Optional custom SQLite database path. Defaults to DB_PATH.
            source: Optional source filter ('greenhouse', 'lever', 'hn').
            limit: Maximum records to load.

        Returns:
            List of unwrapped posting dictionaries ready for extraction.
        """
        target_db = db_path or str(DB_PATH)
        if not Path(target_db).is_file():
            logger.warning("Database file not found: %s", target_db)
            return []

        conn = sqlite3.connect(target_db, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            query = "SELECT cache_id, source, url, payload FROM raw_cache"
            params: List[Any] = []

            if source:
                query += " WHERE source = ?"
                params.append(source.lower())

            query += " ORDER BY fetched_at DESC"

            if limit and limit > 0:
                query += " LIMIT ?"
                params.append(int(limit))

            cursor.execute(query, params)
            rows = cursor.fetchall()

            postings: List[Dict[str, Any]] = []
            for r in rows:
                p_source = r["source"]
                p_url = r["url"]
                payload_str = r["payload"]
                item: Dict[str, Any] = {
                    "cache_id": r["cache_id"],
                    "source": p_source,
                    "url": p_url,
                }

                # Attempt parsing JSON payload
                try:
                    parsed = json.loads(payload_str)
                    if isinstance(parsed, dict):
                        # Merge parsed fields into item
                        for k, v in parsed.items():
                            if k not in item:
                                item[k] = v
                    elif isinstance(parsed, list):
                        item["raw_payload"] = payload_str
                except Exception:
                    item["content"] = payload_str

                postings.append(item)

            return postings
        finally:
            conn.close()
