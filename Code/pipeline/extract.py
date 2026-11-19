"""Phase 2 LLM Extraction Pipeline Orchestrator and CLI.

Provides the standalone entrypoint and orchestration function for Phase 2:
- Loads candidate job postings from JSON files, SQLite RawCache, or Phase 1 ingestion
- Executes concurrent extraction with controlled ThreadPool concurrency
- Enforces anti-hallucination guardrails and target Pydantic schema validation
- Persists structured records into SQLite table `extracted_postings` for Phase 3 deduplication
- Outputs detailed ASCII summary metrics on cost, token accounting, and field coverage

Usage:
    python -m pipeline.extract --help
    python -m pipeline.extract --mock --limit 10
    python -m pipeline.extract --source greenhouse --concurrency 5
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.config import DB_PATH
from pipeline.extractor.batch_runner import BatchExtractor
from pipeline.models.schema import JobPostingRecord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("pipeline.extract")


def run_extraction(
    postings: Optional[List[Dict[str, Any]]] = None,
    limit: Optional[int] = None,
    mock: bool = False,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    source: Optional[Union[str, List[str]]] = None,
    input_file: Optional[Union[str, Path]] = None,
    db_path: Optional[str] = None,
    max_concurrency: int = 5,
    save_db: bool = True,
    output_json: Optional[Union[str, Path]] = None,
) -> List[JobPostingRecord]:
    """Execute the Phase 2 LLM extraction pipeline.

    Args:
        postings: In-memory candidate postings. If None, loaded from file/db/ingest.
        limit: Max postings to extract (useful for testing and cost caps).
        mock: If True, uses offline deterministic Mock LLM mode (zero API cost).
        provider: Optional LLM provider name ('gemini', 'openai', 'mock').
        model: Optional LLM model name (e.g. 'gemini-1.5-flash', 'gemini-4-argon').
        source: Scraper source filter ('greenhouse', 'lever', 'hn', 'all').
        input_file: Path to candidate JSON file.
        db_path: Custom SQLite database path. Defaults to pipeline.config.DB_PATH.
        max_concurrency: Worker threads for ThreadPoolExecutor (default 5).
        save_db: Whether to persist extracted records into SQLite table `extracted_postings`.
        output_json: Optional path to write extracted records to JSON.

    Returns:
        List of strongly-typed JobPostingRecord instances.
    """
    effective_db = db_path or str(DB_PATH)
    candidate_postings: List[Dict[str, Any]] = []

    # 1. Resolve source postings
    if postings is not None:
        logger.info("Using %d in-memory postings provided to run_extraction.", len(postings))
        candidate_postings = list(postings)

    elif input_file:
        logger.info("Loading candidate postings from file: %s", input_file)
        candidate_postings = BatchExtractor.load_postings_from_file(input_file)

    else:
        # Normalize source filter
        src_str: Optional[str] = None
        if source and source != "all":
            if isinstance(source, list):
                src_str = source[0] if len(source) == 1 else None
            else:
                s_lower = str(source).strip().lower()
                if s_lower not in ("all", "both"):
                    src_str = s_lower

        # Attempt loading from SQLite raw_cache
        logger.info("Checking SQLite raw_cache at '%s' (source filter: %s)...", effective_db, src_str)
        cached_postings = BatchExtractor.load_postings_from_db(
            db_path=effective_db,
            source=src_str,
            limit=limit,
        )

        if cached_postings:
            logger.info("Loaded %d postings from SQLite raw_cache.", len(cached_postings))
            candidate_postings = cached_postings
        else:
            logger.info("No cached postings found. Invoking Phase 1 Ingestion...")
            try:
                from pipeline.ingest import run_ingest

                candidate_postings = run_ingest(
                    sources=source if source != "all" else None,
                    limit=limit,
                    filter_roles=True,
                    db_path=effective_db,
                )
                logger.info("Phase 1 Ingestion yielded %d candidate postings.", len(candidate_postings))
            except Exception as exc:
                logger.warning("Phase 1 Ingestion could not be executed: %s", exc)
                candidate_postings = []

    # 2. Apply limit
    if limit is not None and limit > 0:
        candidate_postings = candidate_postings[:limit]
        logger.info("Applied limit of %d postings for extraction.", limit)

    if not candidate_postings:
        logger.warning("No candidate postings available for extraction.")
        return []

    # 3. Initialize BatchExtractor
    logger.info(
        "Initializing BatchExtractor (mock=%s, provider=%s, model=%s, db_path=%s, concurrency=%d)...",
        mock,
        provider,
        model,
        effective_db,
        max_concurrency,
    )
    runner = BatchExtractor(mock=mock, provider=provider, model=model, db_path=effective_db)

    # 4. Run batch extraction
    extracted_records = runner.run_batch(
        postings=candidate_postings,
        max_concurrency=max_concurrency,
    )

    # 5. Persist extracted records into SQLite table `extracted_postings`
    if save_db and extracted_records:
        logger.info("Persisting %d extracted records to SQLite table 'extracted_postings'...", len(extracted_records))
        saved_count = runner.save_extracted_records(
            records=extracted_records,
            db_path=effective_db,
            table_name="extracted_postings",
        )
        logger.info("Successfully persisted %d records into '%s'.", saved_count, effective_db)

    # 6. Optional JSON export
    if output_json and extracted_records:
        out_p = Path(output_json)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        serialized_records = [r.model_dump(mode="json") for r in extracted_records]
        with out_p.open("w", encoding="utf-8") as f:
            json.dump(serialized_records, f, indent=2, ensure_ascii=False)
        logger.info("Exported %d extracted records to JSON: %s", len(extracted_records), out_p)

    # 7. Generate and display ASCII metrics summary table
    report = runner.generate_metrics_report()
    summary_table = runner.format_ascii_metrics_table(report)
    print(summary_table)

    return extracted_records


def _build_cli_parser() -> argparse.ArgumentParser:
    """Build command-line interface argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.extract",
        description="Phase 2 LLM Job Extraction Pipeline: Extracts structured schemas from raw postings.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "-s",
        "--source",
        type=str,
        default="all",
        help="Source to extract from: 'greenhouse', 'lever', 'hn', or 'all' (can be comma-separated)",
    )

    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=None,
        help="Maximum candidate postings to extract (useful for testing or cost controls)",
    )

    parser.add_argument(
        "-i",
        "--input-file",
        type=str,
        default=None,
        help="Optional path to candidate postings JSON file",
    )

    parser.add_argument(
        "-d",
        "--db-path",
        type=str,
        default=str(DB_PATH),
        help="Custom SQLite database path for reading cache and persisting extracted records",
    )

    parser.add_argument(
        "-m",
        "--mock",
        action="store_true",
        default=False,
        help="Enable offline deterministic Mock LLM mode (zero API key dependency, zero cost)",
    )

    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=5,
        help="Maximum concurrent extraction worker threads",
    )

    parser.add_argument(
        "--no-save-db",
        action="store_true",
        default=False,
        help="Disable SQLite database persistence to table 'extracted_postings'",
    )

    parser.add_argument(
        "-o",
        "--output-json",
        type=str,
        default=None,
        help="Optional path to export extracted records as a JSON file",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose DEBUG logging",
    )

    return parser


def main() -> None:
    """CLI entrypoint."""
    parser = _build_cli_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)

    try:
        run_extraction(
            limit=args.limit,
            mock=args.mock,
            source=args.source,
            input_file=args.input_file,
            db_path=args.db_path,
            max_concurrency=args.concurrency,
            save_db=not args.no_save_db,
            output_json=args.output_json,
        )
    except KeyboardInterrupt:
        logger.warning("Extraction aborted by user.")
        sys.exit(130)
    except Exception as exc:
        logger.critical("Fatal error during extraction pipeline execution: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
