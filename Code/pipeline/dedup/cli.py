"""CLI entrypoint and orchestrator for Phase 3 Deduplication and Entity Resolution.

Loads extracted job postings from SQLite or JSON files, resolves cross-source
duplicates using DeduplicationEngine (DSU / RapidFuzz / Authority hierarchy),
persists canonical records to `canonical_job_postings` table, and outputs
comprehensive deduplication metrics.

Usage:
    python -m pipeline.dedup --help
    python -m pipeline.dedup --dry-run
    python -m pipeline.dedup --limit 50 --output-json data/canonical_sample.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.config import DB_PATH
from pipeline.dedup.resolver import DeduplicationEngine
from pipeline.models.canonical import CanonicalJobPosting
from pipeline.models.schema import JobPostingRecord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("pipeline.dedup")


def print_summary_table(
    raw_count: int,
    canonical_postings: List[CanonicalJobPosting],
    elapsed_seconds: float,
    dry_run: bool = False,
    db_path: str = "",
) -> None:
    """Prints an ASCII summary of deduplication metrics."""
    canonical_count = len(canonical_postings)
    duplicates_merged = raw_count - canonical_count
    dedup_ratio = (duplicates_merged / raw_count * 100.0) if raw_count > 0 else 0.0

    multi_source_clusters = [
        c for c in canonical_postings if c.duplicate_count > 1
    ]
    max_cluster_size = max([c.duplicate_count for c in canonical_postings], default=1)

    # Source breakdown
    source_counts: Dict[str, int] = {}
    for c in canonical_postings:
        for src in c.sources:
            source_counts[src] = source_counts.get(src, 0) + 1

    banner_width = 80
    print("\n" + "=" * banner_width)
    print("PHASE 3: DEDUPLICATION & CROSS-SOURCE ENTITY RESOLUTION SUMMARY".center(banner_width))
    print("=" * banner_width)
    print(f"Database Path:                 {db_path}")
    print(f"Dry Run Mode:                  {dry_run}")
    print(f"Elapsed Time:                  {elapsed_seconds:.3f}s")
    print("-" * banner_width)
    print(f"Total Extracted Postings In:   {raw_count}")
    print(f"Canonical Job Entities Out:    {canonical_count}")
    print(f"Total Duplicates Merged:       {duplicates_merged}")
    print(f"Deduplication Ratio:           {dedup_ratio:.1f}%")
    print(f"Multi-Source Merged Clusters:  {len(multi_source_clusters)}")
    print(f"Maximum Cluster Size:          {max_cluster_size}")
    print("-" * banner_width)
    print("Source Distribution across Canonical Entities:")
    for src, count in sorted(source_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"  - {src:<20} {count:>5} entities")
    print("=" * banner_width + "\n")


def run_dedup(
    postings: Optional[List[JobPostingRecord]] = None,
    db_path: Optional[str] = None,
    table_in: str = "extracted_postings",
    table_out: str = "canonical_job_postings",
    limit: Optional[int] = None,
    dry_run: bool = False,
    output_json: Optional[Union[str, Path]] = None,
) -> List[CanonicalJobPosting]:
    """Execute the cross-source deduplication and entity resolution pipeline.

    Args:
        postings: In-memory list of JobPostingRecords. If None, loaded from SQLite table.
        db_path: SQLite database path (default: pipeline.config.DB_PATH).
        table_in: Source table name for extracted postings.
        table_out: Destination table name for canonical entities.
        limit: Max postings to load.
        dry_run: If True, resolves in-memory without database persistence.
        output_json: Optional file path to export canonical postings.

    Returns:
        List of resolved CanonicalJobPosting entities.
    """
    effective_db = str(db_path or DB_PATH)
    start_time = time.perf_counter()

    # 1. Load input postings
    if postings is not None:
        input_postings = list(postings)
        logger.info("Using %d in-memory postings.", len(input_postings))
    else:
        logger.info("Loading extracted postings from '%s' (table: %s)...", effective_db, table_in)
        input_postings = DeduplicationEngine.load_extracted_postings_from_db(
            db_path=effective_db,
            table_name=table_in,
            limit=limit,
        )

    if not input_postings:
        logger.warning("No postings found for deduplication. Exiting.")
        return []

    # 2. Initialize DeduplicationEngine and resolve
    engine = DeduplicationEngine(db_path=effective_db)
    canonical_postings = engine.resolve(input_postings)

    # 3. SQLite Persistence (unless dry-run)
    if not dry_run:
        logger.info("Persisting %d canonical postings into table '%s'...", len(canonical_postings), table_out)
        engine.save_to_db(
            canonical_postings=canonical_postings,
            db_path=effective_db,
            table_name=table_out,
        )
    else:
        logger.info("[Dry Run] Skipped SQLite persistence.")

    # 4. Optional JSON export
    if output_json:
        out_path = Path(output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        dump_data = [p.to_dict() for p in canonical_postings]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(dump_data, f, indent=2, default=str)
        logger.info("Exported %d canonical entities to JSON: %s", len(canonical_postings), out_path)

    elapsed = time.perf_counter() - start_time
    print_summary_table(
        raw_count=len(input_postings),
        canonical_postings=canonical_postings,
        elapsed_seconds=elapsed,
        dry_run=dry_run,
        db_path=effective_db,
    )

    return canonical_postings


def main() -> None:
    """CLI Entrypoint parser for pipeline.dedup."""
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.dedup",
        description="Phase 3: Cross-Source Deduplication & Entity Resolution CLI",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=str(DB_PATH),
        help=f"SQLite database file path (default: {DB_PATH})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum input postings to process",
    )
    parser.add_argument(
        "--table-in",
        type=str,
        default="extracted_postings",
        help="Input SQLite table for extracted postings (default: extracted_postings)",
    )
    parser.add_argument(
        "--table-out",
        type=str,
        default="canonical_job_postings",
        help="Target SQLite table for canonical entities (default: canonical_job_postings)",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="Optional path to write canonical results to JSON",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute deduplication in-memory without saving to database",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)

    run_dedup(
        db_path=args.db_path,
        limit=args.limit,
        table_in=args.table_in,
        table_out=args.table_out,
        output_json=args.output_json,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
