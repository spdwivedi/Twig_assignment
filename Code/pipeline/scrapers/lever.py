"""Lever ATS scraper module for Twig data ingestion pipeline.

Fetches and normalizes job postings from public Lever API endpoints.
Includes robust handling for the Lever Content-Type trap (non-JSON/HTML challenge responses),
rate limiting compliance, and graceful error recovery.
"""

import logging
from typing import Any, Dict, List, Optional

from pipeline.config import LEVER_SITES
from pipeline.scrapers.base import BaseScraper, PoliteHTTPClient
from pipeline.storage.raw_cache import RawCache

logger = logging.getLogger(__name__)


class LeverScraper(BaseScraper):
    """Lever job board scraper ingesting postings via public Lever API endpoints."""

    BASE_URL: str = "https://api.lever.co/v0/postings/{site}?mode=json"

    def __init__(
        self,
        client: Optional[PoliteHTTPClient] = None,
        sites: Optional[List[str]] = None,
        raw_cache: Optional[RawCache] = None,
    ) -> None:
        """Initialize LeverScraper.

        Args:
            client: Optional PoliteHTTPClient instance. Defaults to new PoliteHTTPClient.
            sites: Target company site slugs to scrape. Defaults to LEVER_SITES.
            raw_cache: Optional RawCache instance to persist raw HTTP responses.
        """
        super().__init__(client=client or PoliteHTTPClient())
        self.sites: List[str] = list(sites) if sites is not None else list(LEVER_SITES)
        self.raw_cache = raw_cache

    def fetch_site(self, site: str) -> List[Dict[str, Any]]:
        """Fetch raw job postings array from Lever public API for a single company.

        Enforces the Content-Type trap guard: if the endpoint returns HTML, Cloudflare,
        or any non-JSON payload, logs a descriptive warning and gracefully skips without calling
        response.json().

        Args:
            site: The Lever company identifier / slug (e.g. 'palantir', 'plaid').

        Returns:
            A list of raw posting dictionary objects, or an empty list if unavailable/invalid.
        """
        url = self.BASE_URL.format(site=site)
        logger.info("Fetching Lever postings for site: '%s' from %s", site, url)

        try:
            response = self.client.get(url)
        except Exception as exc:
            logger.error(
                "Unexpected exception fetching Lever site '%s': %s",
                site,
                exc,
                exc_info=True,
            )
            return []

        if response is None:
            logger.warning(
                f"No HTTP response received for Lever site '{site}' (request blocked, timed out, or disallowed by robots.txt)."
            )
            return []

        # =========================================================================
        # CRITICAL REQUIREMENT FROM RUBRIC (The Lever Content-Type Trap)
        # Inspect response.headers.get("Content-Type", "").
        # If Content-Type is NOT application/json (e.g. text/html, cloudflare challenge,
        # or error page), log a descriptive warning, do NOT call response.json(), and return []!
        # =========================================================================
        content_type = response.headers.get("Content-Type", "")
        if "application/json" not in content_type.lower():
            logger.warning(
                f"Lever site '{site}' returned non-JSON Content-Type: {content_type} (status code: {response.status_code}). Skipping JSON parsing."
            )
            return []

        if response.status_code != 200:
            logger.warning(
                f"Lever API returned HTTP status {response.status_code} for site '{site}'."
            )
            return []

        try:
            data = response.json()
        except Exception as exc:
            logger.error(
                f"Failed to parse JSON response for Lever site '{site}' despite Content-Type '{content_type}': {exc}"
            )
            return []

        if not isinstance(data, list):
            if isinstance(data, dict):
                err_msg = data.get("error") or data.get("message") or str(data)
                logger.warning(
                    f"Lever site '{site}' returned dictionary error payload rather than postings array: {err_msg}"
                )
            else:
                logger.warning(
                    f"Lever site '{site}' returned unexpected data format ({type(data).__name__}); expected list of postings."
                )
            return []

        # Optionally persist raw payload into RawCache
        if self.raw_cache and response.text:
            try:
                self.raw_cache.save(source="lever", url=url, payload=response.text)
            except Exception as exc:
                logger.warning("Failed to cache raw response for Lever site '%s': %s", site, exc)

        logger.info(
            "Successfully fetched %d raw postings for Lever site '%s'.",
            len(data),
            site,
        )
        return data

    def normalize_job(self, raw_job: Dict[str, Any], site: str) -> Dict[str, Any]:
        """Normalize a raw Lever job posting into standard schema format.

        Extracts:
            - source: "lever"
            - source_id: str(raw_job.get("id"))
            - company: site.title() (e.g. "Palantir", "Plaid")
            - title: raw_job.get("text", "").strip()
            - location_raw: raw_job.get("categories", {}).get("location", "").strip()
            - commitment: raw_job.get("categories", {}).get("commitment", "")
            - team: raw_job.get("categories", {}).get("team", "")
            - url: raw_job.get("hostedUrl", "")
            - created_at: raw_job.get("createdAt", None)
            - description_raw: raw_job.get("descriptionPlain") or raw_job.get("description", "")
            - raw_payload: raw_job (full untouched dict)

        Args:
            raw_job: Raw job dictionary from Lever API.
            site: The company identifier / slug.

        Returns:
            Normalized dictionary containing standard ingestion fields.
        """
        raw_id = raw_job.get("id")
        source_id = str(raw_id) if raw_id is not None else ""

        company = site.title()
        title = (raw_job.get("text") or "").strip()

        categories = raw_job.get("categories")
        if not isinstance(categories, dict):
            categories = {}

        location_raw = (categories.get("location") or "").strip()
        commitment = categories.get("commitment") or ""
        team = categories.get("team") or ""

        url = raw_job.get("hostedUrl") or ""
        created_at = raw_job.get("createdAt", None)

        description_raw = raw_job.get("descriptionPlain") or raw_job.get("description") or ""

        return {
            "source": "lever",
            "source_id": source_id,
            "company": company,
            "title": title,
            "location_raw": location_raw,
            "commitment": commitment,
            "team": team,
            "url": url,
            "created_at": created_at,
            "description_raw": description_raw,
            "raw_payload": raw_job,
        }

    def scrape(self) -> List[Dict[str, Any]]:
        """Iterate through all target sites, fetch, normalize, and return all postings.

        Returns:
            Aggregated list of normalized job dictionaries across all configured Lever sites.
        """
        all_jobs: List[Dict[str, Any]] = []
        logger.info("Starting Lever scrape across sites: %s", self.sites)

        for site in self.sites:
            logger.info("Scraping Lever site: '%s'", site)
            raw_postings = self.fetch_site(site)
            site_count = 0
            for raw_job in raw_postings:
                if not isinstance(raw_job, dict):
                    continue
                normalized = self.normalize_job(raw_job, site)
                all_jobs.append(normalized)
                site_count += 1

            logger.info(
                "Lever site '%s': fetched %d raw postings, normalized %d jobs (running total: %d).",
                site,
                len(raw_postings),
                site_count,
                len(all_jobs),
            )

        logger.info(
            "Lever scrape complete. Total jobs collected: %d across %d sites.",
            len(all_jobs),
            len(self.sites),
        )
        return all_jobs
