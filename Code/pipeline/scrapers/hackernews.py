"""Hacker News HTML scraper module for Twig data ingestion pipeline.

Crawls and parses monthly 'Ask HN: Who is hiring?' threads from static HTML
without using the official Firebase API, adhering strictly to the assignment
HTML parsing mandate, polite rate limiting, and pagination traversal.
"""

import logging
from typing import Any, Dict, List, Optional
import urllib.parse

from bs4 import BeautifulSoup

from pipeline.config import HN_SUBMITTED_URL
from pipeline.scrapers.base import BaseScraper, PoliteHTTPClient
from pipeline.storage.raw_cache import RawCache

logger = logging.getLogger(__name__)


class HackerNewsScraper(BaseScraper):
    """Hacker News HTML scraper ingesting top-level job comments from 'Who is hiring?' threads."""

    BASE_ITEM_URL: str = "https://news.ycombinator.com/item?id={thread_id}"
    SUBMITTED_URL: str = HN_SUBMITTED_URL

    def __init__(
        self,
        client: Optional[PoliteHTTPClient] = None,
        max_pages: int = 5,
        submitted_url: Optional[str] = None,
        raw_cache: Optional[RawCache] = None,
    ) -> None:
        """Initialize HackerNewsScraper.

        Args:
            client: Optional PoliteHTTPClient instance. Defaults to a new PoliteHTTPClient.
            max_pages: Maximum number of comment pages to paginate through (default: 5).
            submitted_url: URL to search for 'whoishiring' threads (default from pipeline.config).
            raw_cache: Optional RawCache instance to persist raw HTTP responses.
        """
        super().__init__(client=client or PoliteHTTPClient())
        self.max_pages = max_pages
        self.submitted_url = submitted_url or self.SUBMITTED_URL
        self.raw_cache = raw_cache

    def find_latest_hiring_thread(self, submitted_url: Optional[str] = None) -> Optional[Dict[str, str]]:
        """Scrape submitted submissions page and find the latest 'Ask HN: Who is hiring?' thread.

        Args:
            submitted_url: Optional override URL for user submissions.

        Returns:
            Dictionary containing 'id', 'title', and 'url' of the latest hiring thread,
            or None if no matching thread was found or request failed.
        """
        url = submitted_url or self.submitted_url
        logger.info("Searching for latest 'Who is hiring?' thread at %s", url)

        response = self.client.get(url)
        if response is None:
            logger.warning("No HTTP response received for submitted URL: %s", url)
            return None

        if response.status_code != 200:
            logger.warning("HTTP status %d received for submitted URL: %s", response.status_code, url)
            return None

        if self.raw_cache:
            try:
                self.raw_cache.save(source="hackernews", url=url, payload=response.text)
            except Exception as exc:
                logger.warning("Failed to cache raw response for %s: %s", url, exc)

        soup = BeautifulSoup(response.text, "html.parser")
        athings = soup.select("tr.athing")

        for tr in athings:
            title_el = tr.select_one(".titleline a")
            if not title_el:
                continue

            title_text = title_el.get_text(strip=True)
            if "ask hn: who is hiring?" in title_text.lower():
                thread_id = tr.get("id")
                if not thread_id:
                    # Fallback to query parameter in href
                    href = title_el.get("href", "")
                    parsed = urllib.parse.urlparse(href)
                    params = urllib.parse.parse_qs(parsed.query)
                    thread_id = params.get("id", [""])[0]

                if thread_id:
                    thread_url = f"https://news.ycombinator.com/item?id={thread_id}"
                    logger.info("Discovered latest hiring thread: '%s' (ID: %s, URL: %s)", title_text, thread_id, thread_url)
                    return {
                        "id": str(thread_id),
                        "title": title_text,
                        "url": thread_url,
                    }

        logger.warning("No 'Ask HN: Who is hiring?' thread found on submitted page: %s", url)
        return None

    def fetch_thread_comments(
        self,
        thread_id: str,
        max_pages: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch and extract top-level comments from an HN hiring thread with pagination.

        Follows pagination links (<a class="morelink">) up to max_pages. Filters
        strictly for top-level comments identified by img[width="0"] indentation.

        Args:
            thread_id: Hacker News item ID of the target thread.
            max_pages: Maximum number of pages to fetch (defaults to self.max_pages).

        Returns:
            List of raw extracted comment dictionaries containing:
                - comment_id: str
                - text: str
                - raw_html: str
                - thread_id: str
        """
        pages_limit = max_pages if max_pages is not None else self.max_pages
        current_url: Optional[str] = self.BASE_ITEM_URL.format(thread_id=thread_id)
        page_count = 0
        comments: List[Dict[str, Any]] = []

        logger.info(
            "Fetching comments for HN thread ID %s (max_pages=%d)...",
            thread_id,
            pages_limit,
        )

        while current_url and page_count < pages_limit:
            page_count += 1
            logger.info("Fetching thread page %d/%d: %s", page_count, pages_limit, current_url)

            response = self.client.get(current_url)
            if response is None:
                logger.warning("No response received for thread page: %s. Halting pagination.", current_url)
                break

            if response.status_code != 200:
                logger.warning(
                    "HTTP %d received for thread page: %s. Halting pagination.",
                    response.status_code,
                    current_url,
                )
                break

            if self.raw_cache:
                try:
                    self.raw_cache.save(source="hackernews", url=current_url, payload=response.text)
                except Exception as exc:
                    logger.warning("Failed to cache raw response for %s: %s", current_url, exc)

            soup = BeautifulSoup(response.text, "html.parser")
            page_comments_count = 0

            # Scan comment rows
            for tr in soup.select("tr.athing"):
                # Top-level comments have indentation img[width="0"]
                indent_img = tr.select_one("td.ind img")
                if not indent_img:
                    continue

                width = indent_img.get("width")
                if str(width) != "0":
                    continue

                comment_id = tr.get("id")
                if not comment_id:
                    continue

                comm_el = tr.select_one(".commtext, span.commtext, div.commtext, div.comment")
                if not comm_el:
                    continue

                # Remove reply button artifacts if div.comment was selected
                reply_box = comm_el.select_one(".reply")
                if reply_box:
                    reply_box.decompose()

                text = comm_el.get_text(separator="\n", strip=True)
                if not text or text.lower() in ("[deleted]", "[flagged]", "[dead]"):
                    continue

                raw_html = str(comm_el)

                comments.append({
                    "comment_id": str(comment_id),
                    "text": text,
                    "raw_html": raw_html,
                    "thread_id": str(thread_id),
                })
                page_comments_count += 1

            logger.info("Page %d retrieved %d top-level comments (Cumulative: %d)", page_count, page_comments_count, len(comments))

            # Check for pagination: <a class="morelink" href="...">
            more_link = soup.select_one("a.morelink")
            if more_link and more_link.get("href"):
                next_href = more_link.get("href")
                current_url = urllib.parse.urljoin("https://news.ycombinator.com/", next_href)
            else:
                logger.info("No more pages found or last page reached.")
                current_url = None

        logger.info(
            "Finished comment fetching for thread ID %s. Total top-level comments extracted: %d",
            thread_id,
            len(comments),
        )
        return comments

    @staticmethod
    def normalize_comment(
        comment_id: str,
        text: str,
        raw_html: str,
        thread_id: str,
    ) -> Dict[str, Any]:
        """Normalize raw comment data into standard job posting dictionary format.

        Extracts:
            - source: "hackernews"
            - source_id: str(comment_id)
            - company: Basic first-token extraction (e.g. "Stripe | SF" -> "Stripe", else "HackerNews_Post")
            - title: First line of comment or first 80 chars
            - location_raw: ""
            - url: https://news.ycombinator.com/item?id={comment_id}
            - description_raw: text.strip()
            - raw_payload: {"thread_id": thread_id, "comment_id": comment_id, "html": raw_html, "text": text}

        Args:
            comment_id: Hacker News comment item ID.
            text: Comment body text.
            raw_html: Raw HTML snippet of comment.
            thread_id: Parent thread item ID.

        Returns:
            Normalized dictionary following ingestion raw schema.
        """
        # Find first non-empty line
        first_line = ""
        for line in text.split("\n"):
            cleaned_line = line.strip()
            if cleaned_line:
                first_line = cleaned_line
                break

        # Company extraction: First token if pipe-separated, else "HackerNews_Post"
        company = "HackerNews_Post"
        if "|" in first_line:
            first_token = first_line.split("|")[0].strip()
            # Clean formatting such as markdown bold/italics/quotes
            cleaned_token = first_token.strip("*_`~#\"' ")
            if cleaned_token and len(cleaned_token) <= 60:
                company = cleaned_token

        # Title: First line or first 80 chars
        title = "HackerNews_Post"
        if first_line:
            title = first_line[:80].strip()

        return {
            "source": "hackernews",
            "source_id": str(comment_id),
            "company": company,
            "title": title,
            "location_raw": "",
            "url": f"https://news.ycombinator.com/item?id={comment_id}",
            "description_raw": text.strip(),
            "raw_payload": {
                "thread_id": str(thread_id),
                "comment_id": str(comment_id),
                "html": raw_html,
                "text": text,
            },
        }

    def scrape(self, thread_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Execute full Hacker News scraper workflow.

        Discovers the latest hiring thread (or uses provided thread_id),
        fetches and extracts top-level comments across pagination pages,
        and returns normalized comment dictionaries.

        Args:
            thread_id: Optional explicit thread ID to scrape. If omitted,
                       discovers the latest hiring thread automatically.

        Returns:
            List of normalized job posting dictionaries.
        """
        target_thread_id = thread_id
        if not target_thread_id:
            thread_info = self.find_latest_hiring_thread()
            if not thread_info:
                logger.warning("Could not discover latest 'Who is hiring?' thread. Aborting HN scrape.")
                return []
            target_thread_id = thread_info["id"]

        logger.info("Executing scrape for Hacker News thread ID: %s", target_thread_id)
        raw_comments = self.fetch_thread_comments(thread_id=target_thread_id, max_pages=self.max_pages)

        normalized_postings: List[Dict[str, Any]] = [
            self.normalize_comment(
                comment_id=comment["comment_id"],
                text=comment["text"],
                raw_html=comment["raw_html"],
                thread_id=comment["thread_id"],
            )
            for comment in raw_comments
        ]

        logger.info("Normalized %d Hacker News job postings.", len(normalized_postings))
        return normalized_postings
