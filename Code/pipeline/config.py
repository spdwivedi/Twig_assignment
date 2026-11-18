"""Configuration module for the Twig Data Ingestion Pipeline.

Defines target ATS tokens, API/submission endpoints, polite crawling rate limits,
user-agent headers, and filesystem paths for raw file caching and database storage.
"""

from pathlib import Path
from typing import List

# Target ATS tokens for Greenhouse API ingestion
GREENHOUSE_TOKENS: List[str] = ["stripe", "airbnb", "databricks", "figma"]
GREENHOUSE_FALLBACK_TOKENS: List[str] = ["cloudflare", "gitlab"]

# Target sites for Lever API ingestion
LEVER_SITES: List[str] = ["palantir", "plaid"]

# Hacker News submitted submissions URL for 'whoishiring' threads
HN_SUBMITTED_URL: str = "https://news.ycombinator.com/submitted?id=whoishiring"

# Rate limiting delay: maximum 1 request per second per domain (in seconds)
RATE_LIMIT_DELAY: float = 1.0

# User-Agent string adhering to polite scraping standards
DEFAULT_USER_AGENT: str = "TwigDataPipelineBot/1.0 (+https://github.com/spdwivedi/Twig_assignment)"

# Database and file cache paths
DB_PATH: str = "D:/Project_Files/AGY_Assignments/Twig/Code/data/pipeline.db"
RAW_CACHE_DIR: str = "D:/Project_Files/AGY_Assignments/Twig/Code/data/raw"

# Ensure data directories exist upon initialization
Path(RAW_CACHE_DIR).mkdir(parents=True, exist_ok=True)
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
