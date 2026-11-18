# Phase 5: Evaluation, Quality Benchmark, Testing & Documentation

## 1. Objective & Scope
Meet the rigorous deliverables and rubric requirements:
1. **Hand-Labeled Benchmark**: Create a ground-truth dataset of 20 hand-labeled postings (at least 10 from Hacker News).
2. **Quality & Cost Measurement**: Measure and report field-level extraction accuracy (precision, recall, F1) and average LLM dollar cost per posting.
3. **Automated Testing**: Implement automated pytest unit tests covering scrapers, dedup edge cases, schema validations, and change-history logic.
4. **README & Scaling Architecture**: Write a comprehensive, one-page README detailing architecture, trade-offs, failure points, and a concrete plan to scale to 5,000 sources refreshed weekly.

---

## 2. Hand-Labeled Ground Truth Dataset (20 Postings)
- **Source Breakdown**:
  - 10+ postings from Hacker News *Who is hiring?* thread (testing free-form unstructured text).
  - 5 postings from Greenhouse (Stripe, Airbnb, Databricks, Figma).
  - 5 postings from Lever (Palantir, Plaid).
- **Format**: `data/labeled_benchmark.json` containing exact expected fields for:
  - `company`, `title`, `seniority`, `city`, `country`, `remote_policy`, `salary_min`, `salary_max`, `currency`, `employment_type`, `skills`, `visa_sponsorship`.
- **Accuracy Evaluation Script**:
  - Script: `python -m pipeline.evaluate --benchmark data/labeled_benchmark.json`
  - Computes:
    - Field-level exact match / F1 accuracy.
    - Hallucination rate (cases where model extracted a value without supporting text in source snippet).
    - Total prompt tokens, completion tokens, and dollar cost breakdown (e.g. Gemini / OpenAI pricing).

---

## 3. Unit & Integration Test Suite (`tests/`)
- `tests/test_scrapers.py`:
  - Mocked HTTP requests verifying rate-limiting (<=1 req/sec).
  - HTML parsing of Hacker News comments with multi-line text and pagination.
  - Lever JSON / HTML content-type handling.
- `tests/test_schema_and_extractor.py`:
  - Pydantic validation rejects unverified enums or missing IDs.
  - Evidence validator ensures all extracted snippets exist verbatim in source text.
  - Null check: fields unstated in mock postings evaluate strictly to `None`.
- `tests/test_dedup.py`:
  - Cross-source merging: Same job on Greenhouse and HN correctly resolves to one row.
  - Distinct role preservation: Senior Data Engineer vs Staff Data Engineer are not merged.
- `tests/test_idempotency_and_history.py`:
  - Dual pipeline runs generate identical row counts.
  - Mock salary increase generates an audit record in `posting_change_history`.
  - Missing job triggers closure status and audit record.

---

## 4. Production README Specification
A crisp, one-page document covering:
1. **Quickstart & One-Command Run**:
   - `pip install -r requirements.txt`
   - `python -m pipeline run`
   - `pytest`
2. **Architecture & Pipeline Design**:
   - Visual flow: Ingestion -> Raw Cache -> Role Filter -> LLM Extraction -> Dedup -> Canonical Storage -> Change History -> Export.
3. **Design Trade-offs**:
   - Choosing SQLite/DuckDB over full PostgreSQL for local reproducibility.
   - LLM extraction vs rule-based regex parsing for salary and location.
   - Exact vs fuzzy blocking in deduplication.
4. **Failure Modes ("What breaks first")**:
   - Anti-bot / Cloudflare challenges on career portals.
   - Sudden ATS HTML format changes.
   - LLM schema drift or rate limits during large batch runs.
5. **Scaling to 5,000 Sources Refreshed Weekly**:
   - Distributed crawling architecture (Scrapy/Playwright clusters with Redis queues and proxy pools).
   - Cost reduction: Two-tier extraction (cheap regex/NER filter for 80% standard postings, LLM only for high-entropy free text).
   - Incremental change detection via HTTP `ETag` / `Last-Modified` headers to avoid re-extracting unchanged postings.

---

## 5. Deliverables & Acceptance Criteria
- [ ] Ground-truth file `data/labeled_benchmark.json` with 20 manually verified records.
- [ ] Automated evaluation report output with accuracy metrics and cost per posting.
- [ ] Clean test run (`pytest tests/`) passing with 100% green tests.
- [ ] Complete `README.md` fulfilling all take-home assignment grading criteria.
