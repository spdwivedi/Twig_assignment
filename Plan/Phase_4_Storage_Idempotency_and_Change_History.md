# Phase 4: Storage, Idempotency & Change History

## 1. Objective & Scope
Design a performant local storage engine using SQLite or DuckDB that supports:
1. **Idempotent Re-runs**: Re-running the pipeline multiple times without data duplication.
2. **Raw Response Caching**: Storing raw responses to allow reproducible, offline-capable replays.
3. **Change Detection & Audit Trail**: Tracking when a posting closes, reopens, or undergoes salary/location changes over time.
4. **Final Export**: Exporting clean canonical datasets to CSV and tabular formats.

---

## 2. Database Schema (SQLite / DuckDB)

### Table 1: `raw_ingestion_cache`
Stores untouched responses from scrapers to ensure zero data loss and enable idempotent replays.
- `cache_id`: TEXT PRIMARY KEY (SHA-256 hash of URL + query parameters)
- `source`: TEXT (e.g. `greenhouse`, `lever`, `hn`)
- `source_url`: TEXT NOT NULL
- `payload`: TEXT NOT NULL (Raw JSON or HTML string)
- `fetched_at`: TIMESTAMP DEFAULT CURRENT_TIMESTAMP

### Table 2: `canonical_job_postings`
Stores active canonical unified records.
- `posting_id`: TEXT PRIMARY KEY (UUID or hash of `company` + `canonical_title` + `location`)
- `company`: TEXT NOT NULL
- `title`: TEXT NOT NULL
- `seniority`: TEXT CHECK(seniority IN ('junior', 'mid', 'senior', 'staff_plus'))
- `city`: TEXT
- `country`: TEXT
- `remote_policy`: TEXT CHECK(remote_policy IN ('onsite', 'hybrid', 'remote'))
- `salary_min`: REAL
- `salary_max`: REAL
- `currency`: TEXT
- `employment_type`: TEXT CHECK(employment_type IN ('full_time', 'contract', 'intern'))
- `skills`: TEXT (JSON array of strings)
- `visa_sponsorship`: BOOLEAN
- `posted_date`: DATE
- `status`: TEXT DEFAULT 'open' CHECK(status IN ('open', 'closed'))
- `source_urls`: TEXT NOT NULL (JSON array of URLs)
- `evidence`: TEXT NOT NULL (JSON object of field -> snippet)
- `first_seen`: TIMESTAMP NOT NULL
- `last_seen`: TIMESTAMP NOT NULL

### Table 3: `posting_change_history`
Audit log recording every modification to tracked fields across pipeline re-runs.
- `history_id`: INTEGER PRIMARY KEY AUTOINCREMENT
- `posting_id`: TEXT NOT NULL REFERENCES canonical_job_postings(posting_id)
- `field_name`: TEXT NOT NULL (e.g., `salary_min`, `salary_max`, `status`, `location`, `remote_policy`)
- `old_value`: TEXT
- `new_value`: TEXT
- `detected_at`: TIMESTAMP DEFAULT CURRENT_TIMESTAMP
- `reason`: TEXT (e.g., 'posting_closed', 'compensation_updated', 'remote_status_changed')

---

## 3. Idempotency & Change Detection Logic
1. **Upsert Pipeline**:
   - On incoming record: Query `canonical_job_postings` for matching canonical posting ID.
   - If not found: Insert new record, set `status = 'open'`, `first_seen = now`, `last_seen = now`.
   - If found:
     - Compare versioned fields (`salary_min`, `salary_max`, `remote_policy`, `city`, `skills`).
     - If changed: Write a row to `posting_change_history` for each modified field, update canonical record, and update `last_seen = now`.
     - If unchanged: Update `last_seen = now` with no change history inserted.
2. **Posting Closure Detection**:
   - If an active posting (`status = 'open'`) from a company was present in previous runs but is missing from the fresh full crawl of that source:
     - Mark `status = 'closed'`.
     - Log `status: open -> closed` to `posting_change_history`.
     - Retain historical evidence and timestamps.

---

## 4. Export & Interface
- **Command**: `python -m pipeline.export --format csv --output data/export/canonical_jobs.csv`
- Generates a flat CSV representation with JSON columns (skills, source_urls) cleanly formatted.
- Optional query CLI to inspect change histories: `python -m pipeline.history --posting-id <id>`

---

## 5. Deliverables & Acceptance Criteria
- [ ] Database migration / initialization script in `pipeline.db`.
- [ ] Zero duplicate records created when running `python -m pipeline.run` twice consecutively against the same source cache.
- [ ] Verified change history logging when mock salary or status changes are introduced.
- [ ] Formatted CSV export matching the exact assignment specification.
