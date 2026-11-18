"""Command-line interface (CLI) for exporting pipeline records and audit history.

Usage:
    python -m pipeline.export --format csv --output data/export/canonical_jobs.csv
    python -m pipeline.export --format json --output data/export/canonical_jobs.json --status open
    python -m pipeline.export --include-history --status all
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from pipeline.config import DB_PATH
from pipeline.storage.exporter import DataExporter, format_file_size

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pipeline.export")


# ==============================================================================
# CLI Argument Parser Setup
# ==============================================================================


def create_parser() -> argparse.ArgumentParser:
    """Creates argument parser for the export CLI."""
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.export",
        description="Export canonical job postings and change history to CSV or JSON.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--format",
        choices=["csv", "json"],
        default="csv",
        help="Export file format: 'csv' (RFC 4180) or 'json'.",
    )

    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Destination path for canonical jobs export file. "
        "Defaults to 'data/export/canonical_jobs.csv' (or .json if format is json).",
    )

    parser.add_argument(
        "--status",
        choices=["all", "open", "closed"],
        default="all",
        help="Filter canonical job postings by lifecycle status: 'open', 'closed', or 'all'.",
    )

    parser.add_argument(
        "--include-history",
        action="store_true",
        default=False,
        help="Flag to also export change history audit trail from posting_change_history to CSV.",
    )

    parser.add_argument(
        "--history-output",
        type=str,
        default="data/export/change_history.csv",
        help="Destination path for change history audit trail export.",
    )

    parser.add_argument(
        "--db-path",
        type=str,
        default=str(DB_PATH) if DB_PATH else "data/pipeline.db",
        help="Path to SQLite pipeline database.",
    )

    return parser


# ==============================================================================
# Summary Table Formatter
# ==============================================================================


def print_summary_table(
    db_path: str,
    status_filter: str,
    export_format: str,
    export_results: List[dict],
) -> None:
    """Prints a beautiful, human-readable summary table of exported artifacts.

    Args:
        db_path: Database path string.
        status_filter: Status filter applied ('open', 'closed', 'all').
        export_format: Target format ('csv', 'json').
        export_results: List of dicts containing artifact type, path, record count, and size.
    """
    total_records = sum(r["records"] for r in export_results)
    total_files = len(export_results)

    title = "Twig Pipeline Export Summary Report"
    divider = "=" * 80
    subdivider = "-" * 80

    print()
    print(divider)
    print(f"{title:^80}")
    print(divider)
    print(f"  Target Database : {db_path}")
    print(f"  Status Filter   : {status_filter.lower()}")
    print(f"  Export Format   : {export_format.upper()}")
    print(subdivider)
    print(f"  {'Artifact Type':<18} {'Output File':<42} {'Records':>8} {'File Size':>10}")
    print(subdivider)

    for item in export_results:
        art_type = item["type"]
        out_path = item["path"]
        recs = item["records"]
        size_str = item["size"]
        print(f"  {art_type:<18} {out_path:<42} {recs:>8} {size_str:>10}")

    print(divider)
    print(f"  Total Records Exported : {total_records}")
    print(f"  Total Output Files     : {total_files}")
    print(divider)
    print()


# ==============================================================================
# CLI Entrypoint Logic
# ==============================================================================


def main(args: Optional[Sequence[str]] = None) -> int:
    """Main CLI execution routine.

    Args:
        args: Optional list of command-line argument strings.

    Returns:
        Exit code: 0 for success, non-zero for error.
    """
    parser = create_parser()
    parsed_args = parser.parse_args(args)

    fmt = parsed_args.format.lower()
    status_filter = parsed_args.status.lower()
    db_path = parsed_args.db_path

    # Determine default canonical output path based on format
    output_path = parsed_args.output
    if output_path is None:
        if fmt == "json":
            output_path = "data/export/canonical_jobs.json"
        else:
            output_path = "data/export/canonical_jobs.csv"

    # Verify database file existence or connection
    db_file = Path(db_path)
    if not db_file.exists():
        logger.error("Database file not found: %s", db_path)
        print(f"Error: Database file does not exist at '{db_path}'.", file=sys.stderr)
        return 1

    try:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
    except Exception as exc:
        logger.error("Failed to connect to SQLite database at '%s': %s", db_path, exc)
        print(f"Error: Could not connect to database '{db_path}': {exc}", file=sys.stderr)
        return 1

    export_results: List[dict] = []

    try:
        # 1. Export Canonical Job Postings
        if fmt == "json":
            count = DataExporter.export_canonical_jobs_to_json(
                conn=conn,
                output_path=output_path,
                status_filter=status_filter if status_filter != "all" else None,
            )
        else:
            count = DataExporter.export_canonical_jobs_to_csv(
                conn=conn,
                output_path=output_path,
                status_filter=status_filter if status_filter != "all" else None,
            )

        out_size = Path(output_path).stat().st_size if Path(output_path).exists() else 0
        export_results.append({
            "type": "Canonical Jobs",
            "path": output_path,
            "records": count,
            "size": format_file_size(out_size),
        })

        # 2. Export Change History if requested
        if parsed_args.include_history:
            hist_out = parsed_args.history_output
            hist_count = DataExporter.export_change_history(
                conn=conn,
                output_path=hist_out,
            )
            hist_size = Path(hist_out).stat().st_size if Path(hist_out).exists() else 0
            export_results.append({
                "type": "Change History",
                "path": hist_out,
                "records": hist_count,
                "size": format_file_size(hist_size),
            })

        # 3. Print Human-Readable Summary Table
        print_summary_table(
            db_path=db_path,
            status_filter=status_filter,
            export_format=fmt,
            export_results=export_results,
        )

        return 0

    except Exception as exc:
        logger.exception("Export failed due to unexpected error: %s", exc)
        print(f"Error during export: {exc}", file=sys.stderr)
        return 2

    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
