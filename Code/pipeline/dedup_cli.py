"""CLI Entrypoint wrapper for Phase 3 Deduplication.

Enables running via:
    python pipeline/dedup_cli.py --help
    python -m pipeline.dedup_cli --dry-run
"""

from pipeline.dedup.cli import main, run_dedup

if __name__ == "__main__":
    main()
