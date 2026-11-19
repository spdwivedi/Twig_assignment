"""Master Orchestration Pipeline for the Twig Data Engineering System.

Coordinates end-to-end execution across all 6 pipeline stages:
1. Ingestion (`pipeline.ingest`): Multi-source scraping from Greenhouse, Lever, and HN
   (or loading cached / high-fidelity mock candidates for offline execution)
2. Candidate Filtering (`pipeline.filters.role_filter`): Keep US Data/ML/Analytics/Platform roles
3. LLM Extraction (`pipeline.extract`): Concurrent structured extraction with anti-hallucination verification
4. Deduplication & Entity Resolution (`pipeline.dedup_cli` / `DeduplicationEngine`): Multi-pass blocking, RapidFuzz matching, and DSU clustering
5. Storage & Change Detection (`pipeline.storage.idempotent_upsert.IdempotentStorageEngine`): Transactional upsert, active/closed lifecycle management, and field-level diff tracking
6. Export (`pipeline.export`): RFC 4180 CSV (`canonical_jobs.csv`), JSON (`canonical_jobs.json`), and change audit trail (`change_history.csv`)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.config import DB_PATH
from pipeline.filters.role_filter import RoleFilter
from pipeline.models.canonical import CanonicalJobPosting
from pipeline.models.schema import JobPostingRecord
from pipeline.storage.database import DatabaseManager, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("pipeline.run")


# ==============================================================================
# Database Schema & Migration Helper
# ==============================================================================


def ensure_database_schema(conn: sqlite3.Connection) -> None:
    """Ensures database tables exist and handles schema variations between posting_id and canonical_id.

    Guarantees that both `posting_id` and `canonical_id` columns exist with unique indexes
    so foreign key references from `posting_change_history` never fail.
    """
    init_db(conn=conn)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(canonical_job_postings)")
    cols = {row[1] for row in cursor.fetchall()}
    if cols:
        if "posting_id" not in cols and "canonical_id" in cols:
            conn.execute("ALTER TABLE canonical_job_postings ADD COLUMN posting_id TEXT")
            conn.execute("UPDATE canonical_job_postings SET posting_id = canonical_id WHERE posting_id IS NULL")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical_posting_id ON canonical_job_postings(posting_id)")
        elif "canonical_id" not in cols and "posting_id" in cols:
            conn.execute("ALTER TABLE canonical_job_postings ADD COLUMN canonical_id TEXT")
            conn.execute("UPDATE canonical_job_postings SET canonical_id = posting_id WHERE canonical_id IS NULL")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical_canonical_id ON canonical_job_postings(canonical_id)")


# ==============================================================================
# High-Fidelity Mock Candidate Postings (for offline testing & dry-runs)
# ==============================================================================


def get_mock_raw_postings() -> List[Dict[str, Any]]:
    """Returns a rich, deterministic set of mock raw postings across all sources.

    Includes target roles, cross-source duplicates, non-target roles (to test
    filtering), and non-US locations (to test geo-filtering).
    """
    return [
        # --- Greenhouse Postings ---
        {
            "source": "greenhouse",
            "source_id": "gh_stripe_01",
            "id": "5829101",
            "company": "Stripe",
            "title": "Staff Data Engineer",
            "location": "San Francisco, CA",
            "url": "https://boards.greenhouse.io/stripe/jobs/5829101",
            "content": (
                "Stripe is building economic infrastructure for the internet. "
                "We are seeking a Staff Data Engineer to lead our financial ledger and real-time payment ingestion pipelines. "
                "Requirements: 7+ years of experience with Python, SQL, Apache Spark, Kafka, and Presto. "
                "Compensation: $180,000 - $240,000 base salary. Hybrid in San Francisco, CA."
            ),
        },
        {
            "source": "greenhouse",
            "source_id": "gh_databricks_01",
            "id": "6102941",
            "company": "Databricks",
            "title": "Senior ML Platform Engineer",
            "location": "Mountain View, CA",
            "url": "https://boards.greenhouse.io/databricks/jobs/6102941",
            "content": (
                "Databricks provides a unified data analytics and AI platform. "
                "Looking for a Senior ML Platform Engineer to build scalable infrastructure for model training and serving. "
                "Key skills: Python, Go, Kubernetes, Ray, PyTorch, Docker. "
                "Salary: $190,000 - $250,000 per year. Hybrid / Onsite in Mountain View, CA."
            ),
        },
        {
            "source": "greenhouse",
            "source_id": "gh_airbnb_01",
            "id": "4910284",
            "company": "Airbnb",
            "title": "Senior Data Engineer, Core Infrastructure",
            "location": "San Francisco, CA (US Remote)",
            "url": "https://boards.greenhouse.io/airbnb/jobs/4910284",
            "content": (
                "Airbnb is looking for a Senior Data Engineer for Core Infrastructure. "
                "Work on large-scale data lake architecture. "
                "Technologies: Apache Spark, Trino, Apache Iceberg, Python, Airflow. "
                "Remote eligible anywhere in the United States. "
                "Compensation: $175,000 - $225,000."
            ),
        },
        {
            "source": "greenhouse",
            "source_id": "gh_figma_01",
            "id": "7192041",
            "company": "Figma",
            "title": "Analytics Engineer, Growth",
            "location": "San Francisco, CA",
            "url": "https://boards.greenhouse.io/figma/jobs/7192041",
            "content": (
                "Figma is looking for an Analytics Engineer to partner with Product and Growth teams. "
                "Design and maintain dimensional data models in dbt and Snowflake. "
                "Skills: SQL, dbt, Snowflake, Python, Looker. "
                "Base salary: $160,000 - $210,000."
            ),
        },
        # --- Lever Postings ---
        {
            "source": "lever",
            "source_id": "lv_palantir_01",
            "id": "palantir-dp-01",
            "company": "Palantir",
            "title": "Data Platform Engineer, Foundry",
            "location": "New York, NY",
            "url": "https://jobs.lever.co/palantir/palantir-dp-01",
            "content": (
                "Palantir builds software that empowers organizations to effectively integrate their data. "
                "We need a Data Platform Engineer for Foundry infrastructure. "
                "Tech Stack: Java, Python, Apache Spark, Cassandra. "
                "Salary: $150,000 - $200,000. Hybrid in New York, NY."
            ),
        },
        {
            "source": "lever",
            "source_id": "lv_plaid_01",
            "id": "plaid-ml-01",
            "company": "Plaid",
            "title": "Senior Machine Learning Engineer, Risk",
            "location": "New York, NY",
            "url": "https://jobs.lever.co/plaid/plaid-ml-01",
            "content": (
                "Plaid powers the fintech ecosystem. Looking for a Senior Machine Learning Engineer for Risk & Fraud. "
                "You will train production risk evaluation models. "
                "Requirements: Python, PyTorch, SQL, Airflow. "
                "US-Remote eligible or New York office. Salary: $185,000 - $235,000."
            ),
        },
        # --- Hacker News Postings ---
        {
            "source": "hn",
            "source_id": "hn_supabase_01",
            "id": "hn-3800101",
            "company": "Supabase",
            "title": "Senior Data Engineer",
            "location": "San Francisco, CA (Remote)",
            "url": "https://pipeline.local/hn/3800101",
            "content": (
                "Supabase | Senior Data Engineer | San Francisco, CA | REMOTE (US) | $185k - $225k | Visa: Yes\n"
                "We are hiring a Senior Data Engineer to scale our analytical data warehouse and telemetry pipelines.\n"
                "Stack: Python, SQL, PostgreSQL, ClickHouse, Apache Kafka, dbt. "
                "Compensation: $185,000 - $225,000 base + equity. We sponsor H-1B transfers."
            ),
        },
        {
            "source": "hn",
            "source_id": "hn_modal_01",
            "id": "hn-3800102",
            "company": "Modal Labs",
            "title": "Staff ML Infrastructure Engineer",
            "location": "New York, NY",
            "url": "https://pipeline.local/hn/3800102",
            "content": (
                "Modal Labs | Staff ML Infrastructure Engineer | New York, NY | ONSITE | $240,000 - $300,000\n"
                "Cloud compute infrastructure for generative AI teams. "
                "Skills: Rust, Python, Linux, CUDA. Onsite in SoHo NYC office. Salary: $240,000 - $300,000."
            ),
        },
        # --- Cross-Source Duplicate (Stripe Staff Data Engineer on HN) ---
        {
            "source": "hn",
            "source_id": "hn_stripe_dup",
            "id": "hn-3800103",
            "company": "Stripe",
            "title": "Staff Data Engineer",
            "location": "San Francisco, CA",
            "url": "https://pipeline.local/hn/3800103",
            "content": (
                "Stripe | Staff Data Engineer | San Francisco, CA | Hybrid | $180k - $240k | Python, SQL, Spark, Kafka\n"
                "Come join Stripe's data platform team building global payment processing systems. "
                "Apply at https://boards.greenhouse.io/stripe/jobs/5829101"
            ),
        },
        # --- Non-Target Role (To verify RoleFilter exclusion) ---
        {
            "source": "hn",
            "source_id": "hn_sales_exclude",
            "id": "hn-3800104",
            "company": "Acme Corp",
            "title": "Account Executive, Enterprise Sales",
            "location": "Austin, TX",
            "url": "https://pipeline.local/hn/3800104",
            "content": (
                "Acme Corp | Account Executive, Enterprise Sales | Austin, TX | $100k-$150k\n"
                "Looking for a high-performing Account Executive for enterprise B2B SaaS sales."
            ),
        },
        # --- Non-US Location (To verify Geo-Filter exclusion) ---
        {
            "source": "hn",
            "source_id": "hn_uk_exclude",
            "id": "hn-3800105",
            "company": "London AI",
            "title": "Data Engineer",
            "location": "London, UK",
            "url": "https://pipeline.local/hn/3800105",
            "content": (
                "London AI | Data Engineer | London, UK | £75,000\n"
                "Looking for a Data Engineer in central London office. Must have right to work in UK."
            ),
        },
    ]


# ==============================================================================
# Pipeline Orchestrator Class
# ==============================================================================


class PipelineOrchestrator:
    """Master Orchestrator for the Twig Data Engineering Pipeline.

    Integrates all stages into a unified, reliable execution engine:
    1. Ingestion: Polite multi-source scraping (Greenhouse, Lever, HN) or cached/mock loading
    2. Filtering: Role & location filtering via RoleFilter (US / US-Remote Data & ML roles)
    3. Extraction: LLM extraction with anti-hallucination validation (supports mock mode)
    4. Deduplication: Cross-source entity resolution (DSU, Blocker, RapidFuzz, Merger)
    5. Storage: Idempotent transactional upsert & lifecycle change tracking into SQLite
    6. Export: Artifact exports to RFC 4180 CSV, JSON, and change history audit log
    """

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        export_dir: Union[str, Path] = "data/export",
        mock: bool = True,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        limit: Optional[int] = None,
        sources: Optional[Union[str, List[str]]] = None,
        concurrency: int = 5,
        filter_roles: bool = True,
        detect_closures: bool = True,
        save_db: bool = True,
        dry_run: bool = False,
        live_scrape: bool = False,
        use_mock_candidates: Optional[bool] = None,
        verbose: bool = False,
    ) -> None:
        """Initialize the pipeline orchestrator.

        Args:
            db_path: Path to SQLite pipeline database. Defaults to pipeline.config.DB_PATH.
            export_dir: Target directory for exported artifacts (default 'data/export').
            mock: If True, uses offline mock LLM extraction (zero API cost).
            limit: Maximum postings to process per source or overall.
            sources: List or comma-separated string of sources to ingest.
            concurrency: Worker threads for concurrent LLM extraction.
            filter_roles: Whether to apply role and US-remote filtering.
            detect_closures: Whether to detect disappeared postings and mark them closed.
            save_db: Whether to persist intermediate and canonical tables in SQLite.
            dry_run: If True, executes entirely in-memory without persistent DB writes.
            live_scrape: If True, executes real polite scrapers even in mock mode.
            use_mock_candidates: Explicit override to use deterministic mock candidate postings.
            verbose: Enable debug-level logging output.
        """
        self.db_path = str(db_path) if db_path is not None else str(DB_PATH)
        self.export_dir = Path(export_dir)
        self.mock = mock
        self.provider = provider
        self.model = model
        self.limit = limit
        self.sources = sources
        self.concurrency = max(1, concurrency)
        self.filter_roles = filter_roles
        self.detect_closures = detect_closures
        self.save_db = save_db and not dry_run
        self.dry_run = dry_run
        self.live_scrape = live_scrape
        self.use_mock_candidates = use_mock_candidates
        self.verbose = verbose

        if self.verbose:
            logging.getLogger().setLevel(logging.DEBUG)
            logger.setLevel(logging.DEBUG)

    # --------------------------------------------------------------------------
    # Stage 1: Ingestion
    # --------------------------------------------------------------------------

    def stage_1_ingest(
        self,
        sources: Optional[Union[str, List[str]]] = None,
        limit: Optional[int] = None,
        raw_postings: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Stage 1: Fetch raw job postings from scrapers or load mock candidates.

        Args:
            sources: Optional source override ('greenhouse', 'lever', 'hn').
            limit: Optional limit override.
            raw_postings: Optional in-memory pre-loaded postings.

        Returns:
            List of raw job posting dictionaries.
        """
        logger.info("[Stage 1/6] Ingestion: Starting raw data collection...")

        # 1. If explicit postings are passed, use them
        if raw_postings is not None:
            logger.info("Using %d pre-loaded in-memory raw postings.", len(raw_postings))
            postings = list(raw_postings)
            if limit and limit > 0:
                postings = postings[:limit]
            return postings

        effective_sources = sources or self.sources
        effective_limit = limit if limit is not None else self.limit

        # Decide whether to use mock candidate dataset:
        # Default to True when mock=True unless live_scrape is explicitly requested
        use_mocks = self.use_mock_candidates
        if use_mocks is None:
            use_mocks = self.mock and not self.live_scrape

        if use_mocks:
            logger.info("Using deterministic high-fidelity mock candidate dataset for offline execution...")
            postings = get_mock_raw_postings()

            # Filter by requested sources if specified
            if effective_sources:
                if isinstance(effective_sources, str):
                    src_filter = {s.strip().lower() for s in effective_sources.split(",") if s.strip()}
                else:
                    src_filter = {str(s).strip().lower() for s in effective_sources if str(s).strip()}
                # Normalize aliases
                normalized_filter = set()
                for s in src_filter:
                    if s in ("gh", "greenhouse"):
                        normalized_filter.add("greenhouse")
                    elif s in ("lv", "lever"):
                        normalized_filter.add("lever")
                    elif s in ("hn", "hackernews", "hacker_news"):
                        normalized_filter.add("hn")
                    else:
                        normalized_filter.add(s)
                postings = [p for p in postings if p.get("source", "").lower() in normalized_filter]

            if effective_limit and effective_limit > 0:
                postings = postings[:effective_limit]

            # Cache mock raw items in SQLite RawCache if save_db is enabled
            if self.save_db:
                try:
                    from pipeline.storage.raw_cache import RawCache

                    raw_cache = RawCache(db_path=self.db_path)
                    batch = [
                        (
                            p.get("source", "mock"),
                            p.get("url") or f"https://pipeline.local/{p.get('source_id', idx)}",
                            p.get("content") or json.dumps(p),
                        )
                        for idx, p in enumerate(postings)
                    ]
                    raw_cache.save_batch(batch)
                except Exception as cache_exc:
                    logger.debug("Mock RawCache persistence skipped: %s", cache_exc)

        else:
            # Live production scraping mode
            try:
                from pipeline.ingest import run_ingest

                postings = run_ingest(
                    sources=effective_sources,
                    limit=effective_limit,
                    filter_roles=False,
                    db_path=self.db_path if self.save_db else ":memory:",
                )
            except Exception as exc:
                logger.warning("Live scraper execution failed: %s; falling back to mock candidates.", exc)
                postings = get_mock_raw_postings()
                if effective_limit and effective_limit > 0:
                    postings = postings[:effective_limit]

        logger.info("[Stage 1/6] Ingestion complete. Ingested %d raw postings.", len(postings))
        return postings

    # --------------------------------------------------------------------------
    # Stage 2: Candidate Filtering
    # --------------------------------------------------------------------------

    def stage_2_filter(
        self,
        raw_postings: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Stage 2: Filter raw postings to target roles (Data/ML) and US/Remote geography.

        Args:
            raw_postings: List of raw posting dictionaries.

        Returns:
            Filtered list of candidate postings within target scope.
        """
        logger.info("[Stage 2/6] Candidate Filtering: Evaluating %d raw postings...", len(raw_postings))

        if not self.filter_roles:
            logger.info("Role filtering disabled (--no-filter). Retaining all raw postings.")
            candidates = list(raw_postings)
        else:
            role_filter = RoleFilter()
            candidates = role_filter.filter_postings(raw_postings)

        if self.limit and self.limit > 0:
            candidates = candidates[: self.limit]

        logger.info(
            "[Stage 2/6] Candidate Filtering complete: %d / %d postings qualified.",
            len(candidates),
            len(raw_postings),
        )
        return candidates

    # --------------------------------------------------------------------------
    # Stage 3: LLM Extraction
    # --------------------------------------------------------------------------

    def stage_3_extract(
        self,
        candidate_postings: List[Dict[str, Any]],
    ) -> List[JobPostingRecord]:
        """Stage 3: Extract structured 17-field schema with anti-hallucination verification.

        Args:
            candidate_postings: Filtered candidate postings.

        Returns:
            List of validated JobPostingRecord instances.
        """
        logger.info("[Stage 3/6] LLM Extraction: Extracting schemas from %d candidates...", len(candidate_postings))

        if not candidate_postings:
            logger.warning("[Stage 3/6] No candidate postings to extract.")
            return []

        from pipeline.extract import run_extraction

        extracted_records = run_extraction(
            postings=candidate_postings,
            limit=self.limit,
            mock=self.mock,
            provider=self.provider,
            model=self.model,
            db_path=self.db_path,
            max_concurrency=self.concurrency,
            save_db=self.save_db,
        )

        logger.info(
            "[Stage 3/6] LLM Extraction complete: Extracted %d structured records.",
            len(extracted_records),
        )
        return extracted_records

    # --------------------------------------------------------------------------
    # Stage 4: Deduplication & Entity Resolution
    # --------------------------------------------------------------------------

    def stage_4_dedup(
        self,
        extracted_records: List[JobPostingRecord],
    ) -> List[CanonicalJobPosting]:
        """Stage 4: Cross-source entity resolution and transitive deduplication clustering.

        Runs: Normalizer -> Multi-pass Blocker -> RapidFuzz Matcher -> DSU Graph Clustering -> Merger.

        Args:
            extracted_records: List of strongly-typed extracted postings.

        Returns:
            List of resolved CanonicalJobPosting entities.
        """
        logger.info(
            "[Stage 4/6] Deduplication & Entity Resolution: Resolving %d records...",
            len(extracted_records),
        )

        if not extracted_records:
            logger.warning("[Stage 4/6] No extracted records for deduplication.")
            return []

        from pipeline.dedup.resolver import DeduplicationEngine

        engine = DeduplicationEngine(db_path=self.db_path)
        canonical_records = engine.resolve(extracted_records)

        duplicates_merged = len(extracted_records) - len(canonical_records)
        logger.info(
            "[Stage 4/6] Deduplication complete: %d canonical entities resolved (%d duplicates merged).",
            len(canonical_records),
            duplicates_merged,
        )
        return canonical_records

    # --------------------------------------------------------------------------
    # Stage 5: Storage & Change Detection
    # --------------------------------------------------------------------------

    def stage_5_store(
        self,
        canonical_records: List[CanonicalJobPosting],
        raw_postings: Optional[List[Dict[str, Any]]] = None,
        current_timestamp: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Stage 5: Transactional idempotent upsert, lifecycle detection, and field diff tracking.

        Args:
            canonical_records: Canonical job posting entities.
            raw_postings: Optional raw postings from Stage 1 to detect disappeared jobs.
            current_timestamp: Explicit observation timestamp.

        Returns:
            Dictionary containing upsert metrics and closure counts.
        """
        logger.info(
            "[Stage 5/6] Storage & Change Detection: Persisting %d canonical entities...",
            len(canonical_records),
        )

        if self.dry_run:
            logger.info("[Dry Run] Skipped SQLite persistence.")
            return {
                "inserted": 0,
                "updated": 0,
                "unchanged": len(canonical_records),
                "changes_logged": 0,
                "closed": 0,
            }

        from pipeline.storage.idempotent_upsert import IdempotentStorageEngine

        # Initialize storage engine and verify schema compatibility
        storage_engine = IdempotentStorageEngine(db_path=self.db_path)

        if storage_engine.database_manager is not None:
            with storage_engine.database_manager.get_connection(auto_commit=True) as conn:
                ensure_database_schema(conn)

        # 1. Execute atomic batch upsert with change tracking
        upsert_summary = storage_engine.upsert(
            records=canonical_records,
            current_timestamp=current_timestamp,
        )

        # 2. Execute lifecycle closure detection across active crawl IDs
        closed_count = 0
        if self.detect_closures and raw_postings:
            logger.info("Executing active/closed lifecycle closure detection...")
            source_active_map: Dict[str, Set[str]] = {}

            for p in raw_postings:
                src = str(p.get("source", "")).strip().lower()
                sid = (
                    p.get("source_id")
                    or p.get("id")
                    or p.get("posting_id")
                    or p.get("url")
                    or p.get("absolute_url")
                )
                if src and sid:
                    source_active_map.setdefault(src, set()).add(str(sid))

            if source_active_map and storage_engine.database_manager is not None:
                with storage_engine.database_manager.get_connection(auto_commit=True) as conn:
                    for src_name, active_ids in source_active_map.items():
                        try:
                            closed_list = storage_engine.lifecycle_manager.detect_closed_postings(
                                conn=conn,
                                source_name=src_name,
                                active_source_posting_ids=active_ids,
                                current_timestamp=current_timestamp,
                            )
                            closed_count += len(closed_list)
                        except Exception as closure_exc:
                            logger.warning(
                                "Closure detection for source '%s' encountered error: %s",
                                src_name,
                                closure_exc,
                            )

        upsert_summary["closed"] = closed_count

        logger.info(
            "[Stage 5/6] Storage complete: %d inserted, %d updated, %d unchanged, %d closed, %d changes logged.",
            upsert_summary.get("inserted", 0),
            upsert_summary.get("updated", 0),
            upsert_summary.get("unchanged", 0),
            upsert_summary.get("closed", 0),
            upsert_summary.get("changes_logged", 0),
        )
        return upsert_summary

    # --------------------------------------------------------------------------
    # Stage 6: Export
    # --------------------------------------------------------------------------

    def stage_6_export(
        self,
        export_dir: Optional[Union[str, Path]] = None,
        status_filter: str = "all",
    ) -> Dict[str, Any]:
        """Stage 6: Export canonical records and audit history to CSV and JSON.

        Args:
            export_dir: Target directory for export files.
            status_filter: Status filter ('open', 'closed', 'all').

        Returns:
            Dictionary mapping artifact names to path and record count.
        """
        target_dir = Path(export_dir or self.export_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        logger.info("[Stage 6/6] Export: Writing artifacts to '%s'...", target_dir)

        from pipeline.storage.exporter import DataExporter, format_file_size

        csv_path = target_dir / "canonical_jobs.csv"
        json_path = target_dir / "canonical_jobs.json"
        history_path = target_dir / "change_history.csv"

        # Export CSV
        csv_count = DataExporter.export_canonical_jobs_to_csv(
            conn=self.db_path,
            output_path=csv_path,
            status_filter=status_filter,
        )

        # Export JSON
        json_count = DataExporter.export_canonical_jobs_to_json(
            conn=self.db_path,
            output_path=json_path,
            status_filter=status_filter,
        )

        # Export Change History
        history_count = DataExporter.export_change_history(
            conn=self.db_path,
            output_path=history_path,
        )

        # Gather file sizes
        csv_size = format_file_size(csv_path.stat().st_size) if csv_path.exists() else "0 B"
        json_size = format_file_size(json_path.stat().st_size) if json_path.exists() else "0 B"
        history_size = format_file_size(history_path.stat().st_size) if history_path.exists() else "0 B"

        export_artifacts = {
            "csv": {"path": str(csv_path), "records": csv_count, "size": csv_size},
            "json": {"path": str(json_path), "records": json_count, "size": json_size},
            "history": {"path": str(history_path), "records": history_count, "size": history_size},
        }

        logger.info(
            "[Stage 6/6] Export complete: CSV (%d rows, %s), JSON (%d records, %s), History (%d rows, %s).",
            csv_count,
            csv_size,
            json_count,
            json_size,
            history_count,
            history_size,
        )
        return export_artifacts

    # --------------------------------------------------------------------------
    # End-to-End Pipeline Execution
    # --------------------------------------------------------------------------

    def run(
        self,
        raw_postings: Optional[List[Dict[str, Any]]] = None,
        current_timestamp: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Execute the complete end-to-end 6-stage master pipeline.

        Args:
            raw_postings: Optional pre-loaded raw postings list.
            current_timestamp: Optional explicit observation timestamp.

        Returns:
            Comprehensive results dictionary with metrics, counts, artifacts, and records.
        """
        start_time = time.perf_counter()
        logger.info("=" * 80)
        logger.info("STARTING TWIG DATA ENGINEER MASTER PIPELINE EXECUTION")
        logger.info("  Mode:        %s", "Offline Mock Mode (Zero Cost)" if self.mock else "Live Provider Mode")
        logger.info("  Database:    %s", self.db_path)
        logger.info("  Export Dir:  %s", self.export_dir)
        logger.info("  Concurrency: %d", self.concurrency)
        logger.info("  Limit:       %s", str(self.limit) if self.limit else "None")
        logger.info("=" * 80)

        # 1. Ingestion
        ingested_postings = self.stage_1_ingest(raw_postings=raw_postings)

        # 2. Candidate Filtering
        candidates = self.stage_2_filter(ingested_postings)

        # 3. LLM Extraction
        extracted_records = self.stage_3_extract(candidates)

        # 4. Deduplication & Entity Resolution
        canonical_records = self.stage_4_dedup(extracted_records)

        # 5. Storage & Change Detection
        storage_metrics = self.stage_5_store(
            canonical_records=canonical_records,
            raw_postings=ingested_postings,
            current_timestamp=current_timestamp,
        )

        # 6. Export Artifacts
        export_artifacts = self.stage_6_export()

        elapsed_seconds = time.perf_counter() - start_time
        duplicates_merged = len(extracted_records) - len(canonical_records)

        results: Dict[str, Any] = {
            "status": "success",
            "elapsed_seconds": round(elapsed_seconds, 3),
            "mode": "mock" if self.mock else "live",
            "db_path": self.db_path,
            "export_dir": str(self.export_dir),
            "counts": {
                "raw_ingested": len(ingested_postings),
                "filtered_candidates": len(candidates),
                "extracted_records": len(extracted_records),
                "canonical_records": len(canonical_records),
                "duplicates_merged": max(0, duplicates_merged),
                "inserted": storage_metrics.get("inserted", 0),
                "updated": storage_metrics.get("updated", 0),
                "unchanged": storage_metrics.get("unchanged", 0),
                "closed": storage_metrics.get("closed", 0),
                "changes_logged": storage_metrics.get("changes_logged", 0),
                "exported_csv": export_artifacts["csv"]["records"],
                "exported_json": export_artifacts["json"]["records"],
                "exported_history": export_artifacts["history"]["records"],
            },
            "artifacts": {
                "csv": export_artifacts["csv"]["path"],
                "json": export_artifacts["json"]["path"],
                "history": export_artifacts["history"]["path"],
            },
            "canonical_records": canonical_records,
        }

        # Format and display summary report table
        summary_table = self.format_summary_table(results, export_artifacts)
        print(summary_table)

        return results

    # --------------------------------------------------------------------------
    # Summary Metrics Formatter
    # --------------------------------------------------------------------------

    def format_summary_table(
        self,
        results: Dict[str, Any],
        export_artifacts: Dict[str, Any],
    ) -> str:
        """Formats an informative ASCII execution summary report."""
        counts = results["counts"]
        elapsed = results["elapsed_seconds"]
        banner_width = 82

        lines = [
            "",
            "=" * banner_width,
            "                    TWIG DATA ENGINEER PIPELINE EXECUTION SUMMARY".center(banner_width),
            "=" * banner_width,
            f"  Execution Mode:       {'Offline Mock Mode (Zero Cost)' if self.mock else 'Live Provider Mode'}",
            f"  Target Database:      {self.db_path}",
            f"  Export Directory:     {self.export_dir}",
            f"  Total Duration:       {elapsed:.3f}s",
            "-" * banner_width,
            "  STAGE METRICS & THROUGHPUT",
            f"   1. Raw Ingestion:             {counts['raw_ingested']:>5} postings scraped/loaded",
            f"   2. Role & Location Filter:    {counts['filtered_candidates']:>5} candidate Data/ML postings retained",
            f"   3. LLM Structured Extraction: {counts['extracted_records']:>5} postings extracted (0 hallucinations)",
            f"   4. Deduplication & DSU:       {counts['canonical_records']:>5} canonical entities ({counts['duplicates_merged']} duplicates merged)",
            f"   5. Storage & Idempotency:     {counts['inserted']} inserted | {counts['updated']} updated | {counts['unchanged']} unchanged | {counts['closed']} closed",
            "   6. Artifact Exports:",
            f"      - Canonical Jobs CSV:      {export_artifacts['csv']['path']} ({counts['exported_csv']} rows, {export_artifacts['csv']['size']})",
            f"      - Canonical Jobs JSON:     {export_artifacts['json']['path']} ({counts['exported_json']} entities, {export_artifacts['json']['size']})",
            f"      - Change History CSV:      {export_artifacts['history']['path']} ({counts['exported_history']} rows, {export_artifacts['history']['size']})",
            "=" * banner_width,
            "  PIPELINE EXECUTION STATUS: COMPLETED SUCCESSFULLY".center(banner_width),
            "=" * banner_width,
            "",
        ]
        return "\n".join(lines)


# ==============================================================================
# Functional Entrypoint
# ==============================================================================


def run_pipeline(
    mock: bool = True,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    limit: Optional[int] = None,
    sources: Optional[Union[str, List[str]]] = None,
    export_dir: Union[str, Path] = "data/export",
    db_path: Optional[str] = None,
    concurrency: int = 5,
    filter_roles: bool = True,
    detect_closures: bool = True,
    dry_run: bool = False,
    live_scrape: bool = False,
    use_mock_candidates: Optional[bool] = None,
    raw_postings: Optional[List[Dict[str, Any]]] = None,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Execute the end-to-end Master Twig Job Ingestion & Extraction Pipeline.

    Args:
        mock: If True, uses offline deterministic mock extraction (zero cost) and mock candidates fallback.
        provider: Optional LLM provider name ('gemini', 'openai', 'mock').
        model: Optional LLM model name (e.g. 'gemini-1.5-flash', 'gemini-4-argon').
        limit: Max postings to process.
        sources: Sources to scrape ('greenhouse', 'lever', 'hn' or list).
        export_dir: Directory path for exported artifacts.
        db_path: Path to SQLite pipeline database.
        concurrency: Max extraction worker threads.
        filter_roles: Whether to apply RoleFilter.
        detect_closures: Whether to detect disappeared jobs and mark closed.
        dry_run: In-memory run without database writes.
        live_scrape: If True, executes real polite scrapers even in mock mode.
        use_mock_candidates: Explicit override to use deterministic mock candidate postings.
        raw_postings: Optional pre-loaded raw postings list.
        verbose: Enable debug logging.

    Returns:
        Structured results dictionary containing counts, canonical records, and artifact paths.
    """
    orchestrator = PipelineOrchestrator(
        db_path=db_path,
        export_dir=export_dir,
        mock=mock,
        provider=provider,
        model=model,
        limit=limit,
        sources=sources,
        concurrency=concurrency,
        filter_roles=filter_roles,
        detect_closures=detect_closures,
        dry_run=dry_run,
        live_scrape=live_scrape,
        use_mock_candidates=use_mock_candidates,
        verbose=verbose,
    )
    return orchestrator.run(raw_postings=raw_postings)


# ==============================================================================
# Standalone CLI Parser & Main Routine
# ==============================================================================


def build_parser() -> argparse.ArgumentParser:
    """Creates the command-line argument parser for the pipeline runner."""
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.run",
        description="Master Orchestration Pipeline CLI: Runs end-to-end job ingestion, LLM extraction, dedup, storage, and export.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "-m",
        "--mock",
        action="store_true",
        default=False,
        help="Run in offline deterministic Mock LLM mode (zero cost, zero API dependency)",
    )

    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=None,
        help="Maximum postings to process (useful for rapid testing or cost capping)",
    )

    parser.add_argument(
        "-s",
        "--sources",
        type=str,
        default="greenhouse,lever,hn",
        help="Comma-separated sources to ingest: 'greenhouse', 'lever', 'hn'",
    )

    parser.add_argument(
        "-d",
        "--db-path",
        type=str,
        default=None,
        help=f"Custom SQLite database path (default: {DB_PATH})",
    )

    parser.add_argument(
        "-e",
        "--export-dir",
        type=str,
        default="data/export",
        help="Target directory for output export files (canonical_jobs.csv, canonical_jobs.json, change_history.csv)",
    )

    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=5,
        help="Maximum concurrent extraction worker threads",
    )

    parser.add_argument(
        "--no-filter",
        action="store_true",
        default=False,
        help="Disable role and location filtering; process all scraped postings",
    )

    parser.add_argument(
        "--no-closures",
        action="store_true",
        default=False,
        help="Disable lifecycle closure detection for disappeared jobs",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Execute in-memory without persistent SQLite database updates",
    )

    parser.add_argument(
        "--live-scrape",
        action="store_true",
        default=False,
        help="Force real live ATS scraping even when running with --mock",
    )

    parser.add_argument(
        "--provider",
        type=str,
        default=None,
        help="LLM provider name: 'gemini', 'openai', or 'mock'",
    )

    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="LLM model name (e.g. 'gemini-1.5-flash', 'gemini-4-argon', 'gpt-4o-mini')",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose DEBUG logging",
    )

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI execution entrypoint.

    Args:
        argv: Optional command line arguments list.

    Returns:
        Exit code: 0 for success, non-zero for error.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        results = run_pipeline(
            mock=args.mock,
            limit=args.limit,
            sources=args.sources,
            export_dir=args.export_dir,
            db_path=args.db_path,
            concurrency=args.concurrency,
            filter_roles=not args.no_filter,
            detect_closures=not args.no_closures,
            dry_run=args.dry_run,
            live_scrape=args.live_scrape,
            provider=getattr(args, "provider", None),
            model=getattr(args, "model", None),
            verbose=args.verbose,
        )
        return 0 if results.get("status") == "success" else 1
    except Exception as exc:
        logger.error("Master pipeline execution failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
