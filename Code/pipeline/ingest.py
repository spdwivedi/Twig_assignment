"""Phase 1 Ingestion Pipeline Orchestrator and CLI.

Orchestrates the multi-source scraping flow across Greenhouse, Lever, and Hacker News,
persists raw payloads into SQLite RawCache, applies role and US-remote filtering,
and outputs a candidate dataset ready for downstream LLM extraction.
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Ensure project root is in sys.path for direct script execution
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.config import DB_PATH
from pipeline.filters.role_filter import RoleFilter
from pipeline.scrapers.base import PoliteHTTPClient
from pipeline.scrapers.greenhouse import GreenhouseScraper
from pipeline.scrapers.hackernews import HackerNewsScraper
from pipeline.scrapers.lever import LeverScraper
from pipeline.storage.raw_cache import RawCache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("pipeline.ingest")


def _extract_url(posting: Dict[str, Any], source: str, index: int) -> str:
    """Extract canonical or synthetic URL from a posting dictionary."""
    url = posting.get("url") or posting.get("absolute_url")
    if url and isinstance(url, str) and url.strip():
        return url.strip()

    source_id = posting.get("source_id") or posting.get("id") or str(index)
    return f"https://pipeline.local/{source}/{source_id}"


def _extract_payload_string(posting: Dict[str, Any]) -> str:
    """Extract raw string representation of payload for caching."""
    if "raw_payload" in posting:
        payload = posting["raw_payload"]
        if isinstance(payload, str):
            return payload
        elif isinstance(payload, (dict, list)):
            return json.dumps(payload, ensure_ascii=False)

    if "description_raw" in posting and isinstance(posting["description_raw"], str):
        return posting["description_raw"]

    if "content" in posting and isinstance(posting["content"], str):
        return posting["content"]

    return json.dumps(posting, ensure_ascii=False)


def _format_summary_table(
    raw_counts: Dict[str, int],
    cached_counts: Dict[str, int],
    filtered_counts: Dict[str, int],
    db_path: str,
    filter_roles: bool,
) -> str:
    """Format an informative summary table string for logging and stdout."""
    total_raw = sum(raw_counts.values())
    total_cached = sum(cached_counts.values())
    total_filtered = sum(filtered_counts.values())

    status_str = (
        "IN TARGET RANGE"
        if 100 <= total_filtered <= 300
        else ("FILTERED" if total_filtered > 0 else "EMPTY")
    )

    lines = [
        "",
        "=" * 82,
        "                     Twig Phase 1 Ingestion Pipeline Summary",
        "=" * 82,
        f" {'Source':<14} | {'Raw Scraped':>13} | {'Cached in DB':>14} | {'Candidate Jobs':>16} | Status",
        "-" * 16 + "+" + "-" * 15 + "+" + "-" * 16 + "+" + "-" * 18 + "+" + "-" * 13,
    ]

    for src in sorted(raw_counts.keys()):
        r_cnt = raw_counts.get(src, 0)
        c_cnt = cached_counts.get(src, 0)
        f_cnt = filtered_counts.get(src, 0)
        src_status = "SUCCESS" if r_cnt > 0 else "NO DATA"
        lines.append(
            f" {src:<14} | {r_cnt:>13} | {c_cnt:>14} | {f_cnt:>16} | {src_status}"
        )

    lines.extend([
        "-" * 16 + "+" + "-" * 15 + "+" + "-" * 16 + "+" + "-" * 18 + "+" + "-" * 13,
        f" {'TOTAL':<14} | {total_raw:>13} | {total_cached:>14} | {total_filtered:>16} | {status_str}",
        "=" * 82,
        f" Filtering Enabled:    {filter_roles} (Data/ML Engineering & US/US-Remote)",
        f" Target Scope Window:  ~100–300 candidate postings",
        f" SQLite Database Path: {db_path}",
        "=" * 82,
        "",
    ])

    return "\n".join(lines)


def run_ingest(
    sources: Optional[Union[str, List[str]]] = None,
    limit: Optional[int] = None,
    filter_roles: bool = True,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Execute the full Phase 1 ingestion flow across selected sources.

    Args:
        sources: Single or list of sources to scrape ('greenhouse', 'lever', 'hn').
                 Defaults to all three.
        limit: Optional maximum number of postings to retain per source (for testing).
        filter_roles: Whether to apply RoleFilter (target Data/ML and US/Remote). Defaults to True.
        db_path: Optional custom SQLite database path. Defaults to pipeline.config.DB_PATH.

    Returns:
        List of candidate job posting dictionaries ready for extraction.
    """
    if sources is None:
        selected_sources = ["greenhouse", "lever", "hn"]
    elif isinstance(sources, str):
        selected_sources = [s.strip().lower() for s in sources.split(",") if s.strip()]
    else:
        selected_sources = [str(s).strip().lower() for s in sources if str(s).strip()]

    # Normalize source aliases
    normalized_sources: List[str] = []
    for s in selected_sources:
        if s in ("gh", "greenhouse"):
            normalized_sources.append("greenhouse")
        elif s in ("lv", "lever"):
            normalized_sources.append("lever")
        elif s in ("hn", "hackernews", "hacker_news"):
            normalized_sources.append("hn")
        else:
            logger.warning("Unrecognized source '%s' requested; will attempt standard lookup.", s)
            normalized_sources.append(s)

    # 1. Instantiate core shared components
    effective_db_path = db_path or str(DB_PATH)
    client = PoliteHTTPClient()
    raw_cache = RawCache(db_path=effective_db_path)
    role_filter = RoleFilter()

    raw_counts: Dict[str, int] = {}
    cached_counts: Dict[str, int] = {}
    filtered_counts: Dict[str, int] = {}

    all_candidate_postings: List[Dict[str, Any]] = []

    logger.info("Commencing Phase 1 Ingestion for sources: %s", normalized_sources)

    # 2. Scrape each requested source
    for source in normalized_sources:
        postings: List[Dict[str, Any]] = []

        try:
            if source == "greenhouse":
                scraper = GreenhouseScraper(client=client, raw_cache=raw_cache)
                logger.info("Executing GreenhouseScraper...")
                postings = scraper.scrape()
            elif source == "lever":
                scraper = LeverScraper(client=client, raw_cache=raw_cache)
                logger.info("Executing LeverScraper...")
                postings = scraper.scrape()
            elif source == "hn":
                scraper = HackerNewsScraper(client=client, raw_cache=raw_cache)
                logger.info("Executing HackerNewsScraper...")
                postings = scraper.scrape()
            else:
                logger.error("No scraper available for source: '%s'", source)
                continue
        except Exception as exc:
            logger.error(
                "Error encountered during scrape of source '%s': %s",
                source,
                exc,
                exc_info=True,
            )
            postings = []

        # Apply testing limit if requested
        if limit is not None and limit > 0:
            postings = postings[:limit]
            logger.info("Applied limit of %d postings for source '%s'", limit, source)

        raw_counts[source] = len(postings)

        # 3. Save raw payloads into RawCache using high-efficiency batch insertion
        batch_items = []
        for idx, posting in enumerate(postings):
            url = _extract_url(posting, source, idx)
            payload_str = _extract_payload_string(posting)
            batch_items.append((source, url, payload_str))

        try:
            cache_ids = raw_cache.save_batch(batch_items)
            for posting, cid in zip(postings, cache_ids):
                posting["cache_id"] = cid
            cached_count = len(cache_ids)
        except Exception as exc:
            logger.warning(
                "Batch caching failed for source '%s': %s; falling back to sequential caching.",
                source,
                exc,
            )
            cached_count = 0
            for idx, posting in enumerate(postings):
                url = _extract_url(posting, source, idx)
                payload_str = _extract_payload_string(posting)
                try:
                    cid = raw_cache.save(source=source, url=url, payload=payload_str)
                    posting["cache_id"] = cid
                    cached_count += 1
                except Exception as inner_exc:
                    logger.debug("Failed saving single cache item: %s", inner_exc)

        cached_counts[source] = cached_count

        # 4. Run RoleFilter on postings
        if filter_roles:
            candidates = role_filter.filter_postings(postings)
        else:
            candidates = list(postings)

        filtered_counts[source] = len(candidates)
        all_candidate_postings.extend(candidates)

        logger.info(
            "Source '%s': %d raw scraped, %d cached, %d candidate postings retained.",
            source,
            len(postings),
            cached_count,
            len(candidates),
        )

    # 5. Output formatted summary table
    summary_report = _format_summary_table(
        raw_counts=raw_counts,
        cached_counts=cached_counts,
        filtered_counts=filtered_counts,
        db_path=effective_db_path,
        filter_roles=filter_roles,
    )
    print(summary_report)

    return all_candidate_postings


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line flags."""
    parser = argparse.ArgumentParser(
        description="Twig Phase 1 Ingestion Pipeline: Scrapes ATS/HN sources, caches raw payloads, and filters target roles."
    )
    parser.add_argument(
        "--sources",
        type=str,
        default="greenhouse,lever,hn",
        help="Comma-separated sources to scrape: greenhouse,lever,hn (default: 'greenhouse,lever,hn')",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum postings per source to process (for rapid test execution)",
    )
    parser.add_argument(
        "--no-filter",
        action="store_true",
        default=False,
        help="Disable role and location filtering; output all raw scraped postings",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Optional custom SQLite database file path for RawCache",
    )
    return parser.parse_args(argv)


def main() -> None:
    """Entry point for CLI execution."""
    args = parse_args()
    filter_roles = not args.no_filter
    run_ingest(
        sources=args.sources,
        limit=args.limit,
        filter_roles=filter_roles,
        db_path=args.db_path,
    )


if __name__ == "__main__":
    main()
