# Phase 1: Ingestion & Polite Scraping Engine

## 1. Objective & Scope
Build a robust, modular, and polite multi-source ingestion engine to collect job postings from:
1. **Greenhouse Job Boards (API)**: `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` for tokens: `stripe`, `airbnb`, `databricks`, `figma`.
2. **Lever Job Boards (API)**: `https://api.lever.co/v0/postings/{site}?mode=json` for sites: `palantir`, `plaid`.
3. **Hacker News "Who is hiring?" (HTML)**: Current month's thread submitted by user `whoishiring` (`https://news.ycombinator.com/submitted?id=whoishiring`). Crawl the static HTML comment tree following "More" pagination (no Firebase API).

Target scope: Filter and keep only **US or US-Remote** roles in **Data Engineering & ML Engineering** (titles containing data, ML, analytics, or platform engineer). Expectation: ~100–300 raw candidate postings.

---

## 2. Source-by-Source Technical Specification

### A. Greenhouse (`GreenhouseScraper`)
- **Endpoint**: `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
- **Format**: JSON containing an array of job objects with HTML body in `content`.
- **Parsing**:
  - Decode HTML entities and sanitize content for downstream LLM processing.
  - Extract basic metadata: `id`, `title`, `location.name`, `updated_at`, `absolute_url`.
- **Fallback**: If a company board returns 0 jobs or 404, swap in a backup token (e.g. `cloudflare` or `gitlab`) and log.

### B. Lever (`LeverScraper`)
- **Endpoint**: `GET https://api.lever.co/v0/postings/{site}?mode=json`
- **Format**: JSON / HTML verification (Lever endpoints sometimes return text/html on error or rate-limit; inspect `Content-Type`).
- **Parsing**:
  - Extract `id`, `text` (title), `categories.location`, `categories.commitment`, `descriptionPlain` or `description` (HTML).
  - Normalize field keys to standard raw stage schema.

### C. Hacker News HTML (`HackerNewsScraper`)
- **Thread Discovery**: Fetch `https://news.ycombinator.com/submitted?id=whoishiring`, identify the top post with title matching `Ask HN: Who is hiring? (<Month> <Year>)`.
- **Pagination & HTML Parsing**:
  - Parse root page using `BeautifulSoup4` or `lxml`.
  - Extract top-level comments (`td.default` / `.comment`). Ignore child replies unless parent context dictates.
  - Follow `<a class="morelink" ...>` pagination until thread ends or safe depth reached.
- **Handling Free Text**:
  - A single HN comment often lists multiple roles or non-standard formatting.
  - Isolate comments mentioning US/Remote + Data/ML/Analytics/Platform engineering before calling full extraction.

---

## 3. Polite Scraping & Operational Hygiene
- **Rate Limiting**: Domain-specific token-bucket / leaky-bucket limiter enforcing max 1 request/second per host (`boards-api.greenhouse.io`, `api.lever.co`, `news.ycombinator.com`).
- **Robots.txt Adherence**: Check `urllib.robotparser.RobotFileParser` before issuing requests to each domain.
- **Resilience**:
  - `tenacity` or `requests.adapters.HTTPAdapter` with exponential backoff (`factor=1.5`, `max_retries=3`) for status codes 429, 500, 502, 503, 504.
  - Fail-soft: A single failed board or comment must log a structured error and continue without crashing the run.
- **User-Agent Header**: Custom identifiable User-Agent (e.g., `TwigDataPipelineBot/1.0 (+research@data-pipeline)`).

---

## 4. Raw Response Caching
- Store unparsed HTTP responses and raw payloads locally (e.g. `data/raw/{source}_{identifier}_{timestamp}.json` or SQLite `raw_cache` table keyed by URL + SHA256 of payload).
- Enables deterministic offline re-runs, unit testing, and avoiding redundant network requests.

---

## 5. Deliverables & Acceptance Criteria
- [ ] Working ingestion CLI command: `python -m pipeline.ingest --sources greenhouse,lever,hn`
- [ ] Raw cache populated in local SQLite / disk directory.
- [ ] Rate limits verified under load (max 1 req/sec per domain).
- [ ] Structured logging indicating total scraped, filtered, and discarded postings.
