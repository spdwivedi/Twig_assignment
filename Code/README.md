# Twig Codebase Architecture & Execution Guide

This directory contains the production source code and verification test suite for the **Twig Data Engineering Pipeline**.

---

## 1. Directory Structure

```
Twig/Code/
├── data/                      # Local data artifacts, SQLite WAL database, and export outputs
│   ├── export/                # Canonical jobs CSV/JSON and change data capture CSV
│   ├── pipeline.db            # Production SQLite database (WAL mode enabled)
│   ├── labeled_benchmark.json # Ground-truth benchmark dataset (20 postings)
│   └── evaluation_report.md   # Benchmark accuracy and cost evaluation report
├── pipeline/                  # Modular pipeline engine packages
│   ├── ingestion/             # Phase 1: Robust scrapers (Greenhouse, Lever, Hacker News)
│   ├── extractor/             # Phase 2: LLM client, prompts, evidence verifier, batch runner
│   ├── deduplication/         # Phase 3: Normalizer, blocker, title parser, fuzzy matcher, DSU merger
│   ├── storage/               # Phase 4: SQLite WAL manager, schema, change tracker, upsert engine
│   ├── exporter/              # Phase 5: Multi-format canonical and CDC exporter (CSV, JSON)
│   ├── evaluate.py            # Phase 5: Benchmark accuracy evaluator and cost reporting engine
│   ├── extract.py             # Standalone CLI entrypoint for batch extraction
│   ├── ingest.py              # Standalone CLI entrypoint for raw web scraping
│   ├── models.py              # Central Pydantic models, schemas, and enum definitions
│   └── run.py                 # Master orchestrator coordinating all 6 pipeline stages
├── tests/                     # 24 test suites containing 1,006 automated tests
└── pyproject.toml / requirements.txt # Project dependency configurations
```

---

## 2. Core Modules & Responsibilities

1. **`pipeline.ingestion`**:
   - Collects raw job postings from multiple sources (Greenhouse APIs, Lever job boards, and Hacker News *Who is Hiring?* threads).
   - Enforces polite scraping policies ($\le 1.0\text{ req/s}$ per domain, exponential backoff, and in-memory raw response caching).
2. **`pipeline.extractor`**:
   - Multi-provider LLM client (`llm_client.py`) supporting Google Gemini (`gemini-4-argon`, `gemini-3.5-flash-lite`), OpenAI (`gpt-4o-mini`), and zero-dependency offline mock (`mock-extractor`).
   - Anti-hallucination evidence verifier ensuring all populated fields map to verbatim substrings.
   - Batch runner with fail-soft concurrency isolation to prevent single API errors from crashing the batch.
3. **`pipeline.deduplication`**:
   - Company entity normalization and legal suffix stripping (`Inc.`, `LLC`, `Corp`).
   - Standardized title parsing across 12 families and 4 seniority tiers.
   - Multi-field blocking and Jaro-Winkler fuzzy matching.
   - Disjoint Set Union (DSU) graph clustering with strict seniority mismatch guards.
4. **`pipeline.storage`**:
   - Production SQLite engine operating in Write-Ahead Logging (`WAL`) mode with `NORMAL` synchronous commits.
   - Idempotent upserts ensuring re-running identical data causes 0 duplicate inserts or updates.
   - Change Data Capture (`canonical_job_history`) logging old value, new value, and field diffs.
5. **`pipeline.exporter`**:
   - Generates production-ready exports in `data/export/`: `canonical_jobs.csv`, `canonical_jobs.json`, and `change_history.csv`.
6. **`pipeline.evaluate`**:
   - Measures exact match scalar accuracy, set-based skills F1 scores, strict null compliance, and token-based financial accounting.

---

## 3. Quickstart & CLI Commands

### 1. Run Complete Pipeline (Offline Mock Mode)
```powershell
Set-Location -Path "Twig/Code"
python -m pipeline run --mock
```

### 2. Run Complete Pipeline (Live Gemini Mode)
```powershell
# Set your API key in environment or Twig/Notes/.env
$env:GEMINI_API_KEY = "your_api_key_here"

# Execute with high-throughput Gemini 3.5 Flash-Lite
python -m pipeline run --provider gemini --model gemini-3.5-flash-lite

# Execute with frontier Gemini 4 Argon
python -m pipeline run --provider gemini --model gemini-4-argon
```

### 3. Run Verification Test Suite (1,006 Tests)
```powershell
Set-Location -Path "Twig/Code"
python -m pytest
```
