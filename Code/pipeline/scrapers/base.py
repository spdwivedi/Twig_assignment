"""Base classes and polite HTTP infrastructure for data scrapers.

Provides domain-based rate limiting, robots.txt compliance caching, polite HTTP
sessions with automated retries and exponential backoff with jitter, and an
abstract base scraper class.
"""

from abc import ABC, abstractmethod
import logging
import random
import threading
import time
from typing import Any, Dict, List, Optional
import urllib.parse
import urllib.robotparser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from pipeline.config import DEFAULT_USER_AGENT, RATE_LIMIT_DELAY

logger = logging.getLogger(__name__)


class DomainRateLimiter:
    """Thread-safe rate limiter tracking and enforcing delays per domain."""

    def __init__(self, delay: float = RATE_LIMIT_DELAY) -> None:
        """Initialize domain rate limiter.

        Args:
            delay: Minimum delay between requests to the same domain (in seconds).
        """
        self.delay = delay
        self._last_request_time: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        """Block until the minimum delay has elapsed for the URL's domain.

        Args:
            url: The destination URL to rate-limit.
        """
        if not url:
            return

        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower()
        if not domain and parsed.path:
            # Fallback if URL lacks scheme (e.g. 'example.com/path')
            domain = parsed.path.split("/")[0].lower()

        if not domain:
            return

        sleep_duration = 0.0
        with self._lock:
            now = time.monotonic()
            last_time = self._last_request_time.get(domain, 0.0)
            elapsed = now - last_time
            if elapsed < self.delay:
                sleep_duration = self.delay - elapsed
                self._last_request_time[domain] = now + sleep_duration
            else:
                self._last_request_time[domain] = now

        if sleep_duration > 0:
            time.sleep(sleep_duration)


class RobotsParser:
    """Thread-safe robots.txt parser with per-domain caching and fail-safe defaults."""

    def __init__(self, timeout: float = 5.0) -> None:
        """Initialize RobotsParser with a request timeout.

        Args:
            timeout: Network timeout for fetching robots.txt files (in seconds).
        """
        self.timeout = timeout
        self._cache: Dict[str, urllib.robotparser.RobotFileParser] = {}
        self._lock = threading.Lock()

    def _fetch_and_parse(self, domain: str, scheme: str, user_agent: str) -> urllib.robotparser.RobotFileParser:
        """Fetch and parse robots.txt for a domain, returning a configured parser."""
        robots_url = f"{scheme}://{domain}/robots.txt"
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)

        try:
            resp = requests.get(
                robots_url,
                headers={"User-Agent": user_agent},
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            elif resp.status_code == 404:
                logger.info("robots.txt not found (404) at %s; allowing access.", robots_url)
                parser.allow_all = True
            else:
                logger.warning(
                    "robots.txt fetch at %s returned status %d; defaulting to allow.",
                    robots_url,
                    resp.status_code,
                )
                parser.allow_all = True
        except (requests.RequestException, Exception) as exc:
            logger.warning(
                "Could not fetch robots.txt at %s (%s); defaulting to allow.",
                robots_url,
                exc,
            )
            parser.allow_all = True

        return parser

    def can_fetch(self, user_agent: str, url: str) -> bool:
        """Check whether fetching the specified URL is permitted for the given user-agent.

        Args:
            user_agent: User-agent string to check permissions against.
            url: The target URL to check.

        Returns:
            True if fetching is allowed or if robots.txt could not be retrieved,
            False if explicitly disallowed by robots.txt.
        """
        if not url:
            return True

        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower()
        if not domain:
            return True

        scheme = parsed.scheme or "https"

        with self._lock:
            parser = self._cache.get(domain)

        if parser is None:
            parser = self._fetch_and_parse(domain, scheme, user_agent)
            with self._lock:
                self._cache[domain] = parser

        try:
            return parser.can_fetch(user_agent, url)
        except Exception as exc:
            logger.warning("Error evaluating robots.txt for %s: %s; allowing access.", url, exc)
            return True


class PoliteHTTPClient:
    """Polite HTTP client enforcing robots.txt, rate limits, and exponential retries."""

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        rate_limiter: Optional[DomainRateLimiter] = None,
        robots_parser: Optional[RobotsParser] = None,
        total_retries: int = 3,
        backoff_factor: float = 1.5,
    ) -> None:
        """Initialize polite HTTP client.

        Args:
            user_agent: Default User-Agent header string.
            rate_limiter: Rate limiter instance; defaults to new DomainRateLimiter.
            robots_parser: Robots.txt parser instance; defaults to new RobotsParser.
            total_retries: Maximum retry attempts for transient errors.
            backoff_factor: Factor for exponential backoff on retries.
        """
        self.user_agent = user_agent
        self.rate_limiter = rate_limiter or DomainRateLimiter()
        self.robots_parser = robots_parser or RobotsParser()
        self.total_retries = total_retries
        self.backoff_factor = backoff_factor

        self.session = requests.Session()
        retry_strategy = Retry(
            total=total_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def get(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Optional[requests.Response]:
        """Perform a polite GET request with robots checks, rate limiting, and retries.

        Args:
            url: The destination URL.
            headers: Optional HTTP headers to include with the request.
            params: Optional query parameters.
            **kwargs: Additional parameters forwarded to `requests.Session.get`.

        Returns:
            The `requests.Response` object, or None if disallowed or if request failed.
        """
        # 1. Enforce robots.txt policy
        if not self.robots_parser.can_fetch(self.user_agent, url):
            logger.warning(
                "Fetching disallowed by robots.txt: %s (User-Agent: %s)",
                url,
                self.user_agent,
            )
            return None

        # 2. Build default request headers
        request_headers: Dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/html, */*",
        }
        if headers:
            request_headers.update(headers)

        kwargs.setdefault("timeout", 30.0)

        # 3. Perform request with rate limiting and retry handling
        for attempt in range(1, self.total_retries + 1):
            try:
                self.rate_limiter.wait(url)
                response = self.session.get(
                    url,
                    headers=request_headers,
                    params=params,
                    **kwargs,
                )
                if not response.ok:
                    logger.warning(
                        "HTTP request returned status %d for URL: %s",
                        response.status_code,
                        url,
                    )
                return response
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                if attempt == self.total_retries:
                    logger.error(
                        "Transient error exhausted %d attempts for URL: %s - Error: %s",
                        self.total_retries,
                        url,
                        exc,
                    )
                    return None
                jitter = random.uniform(0.1, 0.5)
                sleep_time = (self.backoff_factor ** attempt) + jitter
                logger.warning(
                    "Transient network error on attempt %d/%d for URL: %s (%s). Retrying in %.2fs...",
                    attempt,
                    self.total_retries,
                    url,
                    exc,
                    sleep_time,
                )
                time.sleep(sleep_time)
            except requests.exceptions.RequestException as exc:
                logger.error("HTTP request failed for URL: %s - Error: %s", url, exc)
                return None
            except Exception as exc:
                logger.error(
                    "Unexpected error during HTTP GET for URL: %s - Error: %s",
                    url,
                    exc,
                    exc_info=True,
                )
                return None

        return None

    def close(self) -> None:
        """Close the underlying requests session."""
        self.session.close()

    def __enter__(self) -> "PoliteHTTPClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


class BaseScraper(ABC):
    """Abstract base class for all source scrapers in the ingestion pipeline."""

    def __init__(self, client: PoliteHTTPClient) -> None:
        """Initialize the scraper with a polite HTTP client.

        Args:
            client: Configured PoliteHTTPClient instance.
        """
        self.client = client

    @abstractmethod
    def scrape(self) -> List[Dict[str, Any]]:
        """Execute the scraper to retrieve and return raw record dictionaries.

        Returns:
            A list of dictionary records retrieved from the source.
        """
        raise NotImplementedError
