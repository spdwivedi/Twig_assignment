"""Greenhouse ATS scraper module for Twig data ingestion pipeline.

Fetches, sanitizes, and normalizes job postings from public Greenhouse API endpoints
with automatic fallback handling, polite rate-limiting, and error resilience.
"""

import html
import logging
from typing import Any, Dict, List, Optional

from pipeline.config import GREENHOUSE_FALLBACK_TOKENS, GREENHOUSE_TOKENS
from pipeline.scrapers.base import BaseScraper, PoliteHTTPClient
from pipeline.storage.raw_cache import RawCache

logger = logging.getLogger(__name__)


class GreenhouseScraper(BaseScraper):
    """Greenhouse job board scraper ingesting postings via Greenhouse Boards API."""

    BASE_URL: str = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"

    def __init__(
        self,
        client: Optional[PoliteHTTPClient] = None,
        tokens: Optional[List[str]] = None,
        fallback_tokens: Optional[List[str]] = None,
        raw_cache: Optional[RawCache] = None,
    ) -> None:
        """Initialize GreenhouseScraper.

        Args:
            client: Optional PoliteHTTPClient instance. Defaults to new PoliteHTTPClient.
            tokens: Target board tokens to scrape. Defaults to GREENHOUSE_TOKENS.
            fallback_tokens: Fallback tokens if a board fails. Defaults to GREENHOUSE_FALLBACK_TOKENS.
            raw_cache: Optional RawCache instance to persist raw HTTP responses.
        """
        super().__init__(client=client or PoliteHTTPClient())
        self.tokens: List[str] = list(tokens) if tokens is not None else list(GREENHOUSE_TOKENS)
        self.fallback_tokens: List[str] = (
            list(fallback_tokens) if fallback_tokens is not None else list(GREENHOUSE_FALLBACK_TOKENS)
        )
        self.raw_cache = raw_cache

    def _fetch_raw_jobs(self, token: str) -> Optional[List[Dict[str, Any]]]:
        """Fetch raw job list directly for a specific token without fallback recursion.

        Args:
            token: The Greenhouse board token.

        Returns:
            List of raw job dictionaries if successful and non-empty, None otherwise.
        """
        url = self.BASE_URL.format(token=token)
        response = self.client.get(url)

        if response is None:
            logger.warning("No HTTP response received for Greenhouse token '%s'", token)
            return None

        if response.status_code != 200:
            logger.warning(
                "Greenhouse API returned HTTP %d for token '%s'",
                response.status_code,
                token,
            )
            return None

        try:
            data = response.json()
        except Exception as exc:
            logger.warning(
                "Failed to parse JSON response for Greenhouse token '%s': %s",
                token,
                exc,
            )
            return None

        if not isinstance(data, dict):
            logger.warning(
                "Expected JSON dict response for token '%s', got %s",
                token,
                type(data).__name__,
            )
            return None

        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            logger.warning(
                "No 'jobs' list found in JSON payload for token '%s'",
                token,
            )
            return None

        if len(jobs) == 0:
            logger.warning(
                "Greenhouse board for token '%s' returned 0 jobs",
                token,
            )
            return None

        # Optionally persist raw payload into RawCache
        if self.raw_cache:
            try:
                self.raw_cache.save(source="greenhouse", url=url, payload=response.text)
            except Exception as exc:
                logger.warning("Failed to cache raw response for '%s': %s", token, exc)

        # Tag each job with effective board token for downstream identification
        for job in jobs:
            if isinstance(job, dict):
                job["_board_token"] = token

        return jobs

    def fetch_board(self, token: str) -> List[Dict[str, Any]]:
        """Fetch raw job postings for a given Greenhouse company token.

        If the board returns 0 jobs, 404, or fails, attempts fallback tokens
        from GREENHOUSE_FALLBACK_TOKENS and logs a clear warning note.

        Args:
            token: Greenhouse board token (e.g. 'stripe', 'airbnb').

        Returns:
            List of raw job dictionaries from the board or a fallback board.
        """
        jobs = self._fetch_raw_jobs(token)
        if jobs is not None:
            logger.info("Successfully fetched %d jobs for token '%s'", len(jobs), token)
            return jobs

        # Primary token returned 0 jobs or failed; attempt fallback tokens
        logger.warning(
            "Greenhouse board for token '%s' failed or returned 0 jobs. Initiating fallback search...",
            token,
        )
        for fb_token in self.fallback_tokens:
            if fb_token == token:
                continue

            logger.warning(
                "Attempting fallback token '%s' in place of failed token '%s'...",
                fb_token,
                token,
            )
            fb_jobs = self._fetch_raw_jobs(fb_token)
            if fb_jobs is not None:
                logger.warning(
                    "Fallback succeeded: Token '%s' replaced by fallback token '%s' (%d jobs retrieved).",
                    token,
                    fb_token,
                    len(fb_jobs),
                )
                return fb_jobs

        logger.warning(
            "All fallback tokens %s failed for token '%s'. Returning empty job list.",
            self.fallback_tokens,
            token,
        )
        return []

    def normalize_job(self, raw_job: Dict[str, Any], token: str) -> Dict[str, Any]:
        """Normalize a raw Greenhouse job posting into standard schema format.

        Extracts:
            - source: "greenhouse"
            - source_id: str(raw_job.get("id"))
            - company: token.title() (e.g. "Stripe", "Airbnb")
            - title: raw_job.get("title", "").strip()
            - location_raw: raw_job.get("location", {}).get("name", "").strip()
            - url: raw_job.get("absolute_url", "")
            - updated_at: raw_job.get("updated_at", "")
            - description_raw: sanitized unescaped HTML content
            - raw_payload: full untouched dict

        Args:
            raw_job: Raw job dictionary from Greenhouse API.
            token: Company token associated with the job board.

        Returns:
            Standardized job posting dictionary.
        """
        effective_token = raw_job.get("_board_token", token)
        company = effective_token.title()

        # Build clean raw_payload without internal helper keys
        raw_payload = {k: v for k, v in raw_job.items() if k != "_board_token"}

        raw_id = raw_job.get("id")
        source_id = str(raw_id) if raw_id is not None else ""

        title = (raw_job.get("title") or "").strip()

        location_data = raw_job.get("location")
        if isinstance(location_data, dict):
            location_raw = (location_data.get("name") or "").strip()
        elif isinstance(location_data, str):
            location_raw = location_data.strip()
        else:
            location_raw = ""

        url = raw_job.get("absolute_url") or ""
        updated_at = raw_job.get("updated_at") or ""

        # Decode unescaped HTML entities (e.g. &lt;h2&gt; -> <h2>, &amp; -> &)
        raw_content = raw_job.get("content") or ""
        description_raw = html.unescape(raw_content).strip()

        return {
            "source": "greenhouse",
            "source_id": source_id,
            "company": company,
            "title": title,
            "location_raw": location_raw,
            "url": url,
            "updated_at": updated_at,
            "description_raw": description_raw,
            "raw_payload": raw_payload,
        }

    def scrape(self) -> List[Dict[str, Any]]:
        """Iterate through all target tokens, fetch, normalize, and return all postings.

        Returns:
            Aggregated list of normalized job dictionaries.
        """
        aggregated_jobs: List[Dict[str, Any]] = []
        logger.info("Starting Greenhouse scrape across tokens: %s", self.tokens)

        for token in self.tokens:
            logger.info("Scraping Greenhouse board for token: '%s'", token)
            raw_jobs = self.fetch_board(token)
            board_count = 0
            for raw_job in raw_jobs:
                normalized = self.normalize_job(raw_job, token)
                aggregated_jobs.append(normalized)
                board_count += 1
            logger.info(
                "Company token '%s': scraped and normalized %d jobs (running total: %d)",
                token,
                board_count,
                len(aggregated_jobs),
            )

        logger.info(
            "Greenhouse scrape complete. Total jobs collected: %d across %d tokens",
            len(aggregated_jobs),
            len(self.tokens),
        )
        return aggregated_jobs
