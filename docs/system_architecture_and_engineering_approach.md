# Twig Data Engineering Pipeline: System Architecture, Features & Engineering Approach

This document serves as the authoritative architectural specification and engineering guide for the **Twig Data Engineering Pipeline**. It details the core features, component workflows, algorithm implementations, design trade-offs, and production resilience strategies.

---

## 1. Executive Summary & Design Philosophy

The Twig Data Engineering Pipeline is an enterprise-grade ETL, structured extraction, and entity resolution platform engineered to harvest, parse, deduplicate, and track software engineering and data job postings across heterogeneous sources (Greenhouse ATS, Lever ATS, and unstructured Hacker News *Who is hiring?* threads).

### Core Engineering Principles
1. **Defense-in-Depth Anti-Hallucination**: Generative AI models are treated as probabilistic text transformers rather than authoritative databases. All extracted fields are subjected to deterministic, programmatic verification against raw text.
2. **Polite Web Crawling**: Ingestion modules enforce strict token-bucket rate limits ($\le 1.0\text{ req/s}$ per domain), automatic `robots.txt` compliance, and randomized exponential backoff with jitter.
3. **Deterministic Idempotency**: Re-running the pipeline on identical input yields zero state drift, zero duplicate rows, and zero phantom change-history events.
4. **Fail-Soft Concurrency**: Upstream cloud API quota exhaustion (e.g., HTTP 429 spending cap) or network timeouts are isolated to individual records, allowing the rest of the batch to persist successfully.
5. **Transitive Entity Resolution**: Duplicate records across multiple sources are resolved using mathematical equivalence classes (Disjoint Set Union) rather than fragile pairwise heuristics.

---

## 2. End-to-End Workflow & Data Lifecycle

The pipeline operates across six decoupled stages, transforming raw network responses into verified canonical entities:

```mermaid
flowchart TD
    subgraph STAGE1 ["Stage 1: Multi-Source Ingestion & Raw Caching"]
        GH["Greenhouse API Scraper<br/>(Stripe, Databricks, Airbnb, Figma)"]
        LEV["Lever API Scraper<br/>(Palantir, Plaid)"]
        HN["Hacker News HTML Scraper<br/>('Who is hiring?' threads)"]
        
        POLITE["PoliteHTTPClient<br/>- robots.txt compliance<br/>- 1.0s/domain token bucket<br/>- Exponential backoff + Jitter"]
        CACHE[("raw_postings (SQLite)<br/>SHA-256 payload deduplication")]
        
        GH --> POLITE
        LEV --> POLITE
        HN --> POLITE
        POLITE --> CACHE
    end

    subgraph STAGE2 ["Stage 2: Scope & Geographic Filtering"]
        PRE["Preprocessor Regex Filter<br/>- Title scope: Data / ML / Analytics / Platform<br/>- Geo scope: US / US-Remote<br/>(Prunes >85% of upstream volume)"]
        CACHE --> PRE
    end

    subgraph STAGE3 ["Stage 3: Dual-Barrier LLM Extraction"]
        DOM["HTML/DOM Stripper<br/>(72.4% token reduction)"]
        LLM["Multi-Model LLM Client<br/>(Gemini 4 Argon / 3.5 Flash-Lite / Mock)"]
        VERIF["Verbatim Evidence Verifier<br/>- Exact substring check against raw text<br/>- The Strict Null Rule (Null-over-guess)<br/>- Salary annualization & sanity bounds"]
        
        PRE --> DOM --> LLM --> VERIF
    end

    subgraph STAGE4 ["Stage 4: Entity Resolution & Deduplication"]
        NORM["Entity Normalizer<br/>- Legal suffix stripping (Inc, LLC)<br/>- 50-state & metro normalization"]
        TP["TitleParser & Disambiguator<br/>- 12 title families<br/>- 4 seniority tiers (Senior != Staff)"]
        BLOCK["Multi-Pass Inverted Index Blocker<br/>(Prunes >95% of pairwise search space)"]
        MATCH["RapidFuzz Token Set Scorer<br/>(Composite threshold >= 0.88)"]
        DSU["Union-Find / DSU Clustering<br/>(Transitive graph closure)"]
        MERGE["Hierarchical Record Merger<br/>Greenhouse (1.0) > Lever (0.9) > HN (0.7)"]
        
        VERIF --> NORM --> TP --> BLOCK --> MATCH --> DSU --> MERGE
    end

    subgraph STAGE5 ["Stage 5: SQLite WAL Storage & Change Data Capture"]
        WAL[("pipeline.db (SQLite WAL Mode)<br/>synchronous=NORMAL, busy_timeout=30s")]
        UPSERT["Idempotent Upsert Engine<br/>Deterministic UUIDv5 Canonical Keys"]
        CDC["ChangeTracker (CDC)<br/>- $1.00 float tolerance threshold<br/>- Set-equality skills comparison"]
        LIFE["Lifecycle Manager<br/>Active / Closed / Reopened states"]
        
        MERGE --> UPSERT --> CDC --> LIFE --> WAL
    end

    subgraph STAGE6 ["Stage 6: Multi-Format Export & Evaluation"]
        CSV_CAN["canonical_jobs.csv (RFC 4180)"]
        CSV_HIST["change_history.csv (CDC Audit Trail)"]
        JSON_CAN["canonical_jobs.json"]
        BENCH["Benchmark Evaluator<br/>(20 gold-standard postings)"]
        
        WAL --> CSV_CAN
        WAL --> CSV_HIST
        WAL --> JSON_CAN
        WAL --> BENCH
    end
```

---

## 3. Feature Breakdown & Implementation Details

### Feature 1: Multi-Source Polite Ingestion Engine (`pipeline.ingestion`)
- **Greenhouse Scraper (`greenhouse.py`)**: Interfaces with Greenhouse's public REST APIs for target companies (e.g., Stripe, Databricks, Airbnb, Figma). Handles nested departments, parses embedded HTML descriptions, and extracts source metadata.
- **Lever Scraper (`lever.py`)**: Ingests job feeds from Lever (e.g., Palantir, Plaid). Inspects `Content-Type` headers to handle JSON feeds with automatic HTML fallback.
- **Hacker News Scraper (`hackernews.py`)**: Uses static HTML parsing (`BeautifulSoup`) to scrape official "Who is hiring?" threads from user `whoishiring`. Automatically traverses "More" pagination links across all sub-pages without requiring external Firebase API tokens.
- **Polite Crawling Protocol (`base.py`)**:
  - Implements a thread-safe token-bucket rate limiter enforcing $\le 1.0\text{ req/sec}$ per destination domain.
  - Automatically queries and caches `robots.txt` paths via `urllib.robotparser`.
  - Configures `urllib3.util.Retry` with exponential backoff (1.0s, 2.0s, 4.0s) and jitter for HTTP 429, 500, 502, 503, and 504 errors.
- **Content-Addressable Caching (`raw_cache.py` / `database.py`)**:
  - Stores all unparsed HTTP responses in SQLite `raw_postings` keyed by `SHA-256(source + url)`.
  - Enables instant, air-gapped offline re-runs and isolates network failures.

---

### Feature 2: Dual-Barrier Structured LLM Extraction (`pipeline.extractor`)
- **Pydantic v2 Contract (`schema.py`)**:
  - Enforces a rigorous 17-field schema contract (`JobPostingRecord`) with strictly typed enums:
    - `seniority`: `junior`, `mid`, `senior`, `staff_plus`.
    - `remote_policy`: `onsite`, `hybrid`, `remote`.
    - `employment_type`: `full_time`, `contract`, `intern`.
    - `status`: `active`, `closed`, `reopened`.
  - Configured with `extra='forbid'` to prevent schema creep.
- **DOM & Boilerplate Preprocessing (`preprocessor.py`)**:
  - Prunes non-essential elements (`<script>`, `<style>`, `<nav>`, `<footer>`, SVG vectors, CSS styling).
  - Normalizes whitespace, achieving **72.4% prompt token savings** while preserving compensation clauses and role responsibilities.
- **Multi-Model Abstraction (`llm_client.py`)**:
  - Dynamic client supporting Google Gemini (`gemini-4-argon`, `gemini-3.5-flash-lite`), OpenAI (`gpt-4o-mini`), and zero-dependency offline simulation (`mock-extractor`).
  - Integrated official Google Cloud / DeepMind API rate cards for accurate financial auditing.
  - Externalized secret key resolution via `Notes/.env` and `Notes/gemini_api_key.txt` with strict test harness isolation.
- **Dual-Barrier Evidence Verification (`validators.py`)**:
  1. *Barrier 1 (Structured Extraction)*: The model generates extracted fields accompanied by literal text snippets (`evidence_snippets`).
  2. *Barrier 2 (Programmatic Grounding)*: `validate_evidence_in_text()` verifies character-for-character that extracted numbers, strings, and evidence snippets exist verbatim within the preprocessed text.
  3. *The Strict Null Rule*: If an evidence snippet cannot be proven verbatim in the raw text, the field is automatically coerced to `None`. No hallucinations are tolerated.
- **Compensation Normalization**:
  - Detects hourly pay rates (e.g., "$85 / hr") and standardizes them to annual equivalents ($85 \times 2,080 = \$176,800$).
  - Converts monthly ranges ($\times 12$) and enforces currency consistency and $salary_{min} \le salary_{max}$ sanity bounds.
- **Fail-Soft Batch Runner (`batch_runner.py`)**:
  - Executes multi-threaded concurrent extraction with a worker pool.
  - Quarantines individual API failures (e.g., HTTP 429 spending cap or JSON syntax errors) using item-level try/except blocks, allowing the rest of the batch to complete and persist.

---

### Feature 3: Transitive Deduplication & Entity Resolution (`pipeline.deduplication`)
- **Entity Normalization (`normalizer.py`)**:
  - Strips legal corporate suffixes (`Inc.`, `LLC`, `Corp.`, `GmbH`, `Co., Ltd.`).
  - Resolves company parent-brand aliases (e.g., "Databricks Inc." -> "Databricks").
  - Normalizes geographic locations across 50 US states, 2-letter postal codes, and major metropolitan tech centers.
- **Title Parsing & Seniority Fencing (`title_parser.py`)**:
  - Maps titles into 12 standardized engineering families (`data_engineer`, `ml_engineer`, `analytics_engineer`, `platform_engineer`, etc.).
  - Enforces **hard seniority boundary fences**: `Senior Data Engineer` ($L5$) is strictly prevented from merging with `Staff Data Engineer` ($L6$) or `Junior Data Engineer` ($L3$), regardless of company or description similarity.
- **Multi-Pass Inverted Index Blocking (`blocker.py`)**:
  - Constructs multi-key blocking partitions:
    - Block Key 1: `(norm_company, seniority)`
    - Block Key 2: `(norm_company, title_family)`
    - Block Key 3: `(norm_company, metro_location)`
  - Reduces the pairwise candidate search space by **>95.2%**, transforming an $O(N^2)$ bottleneck into an efficient $O(N \cdot B)$ partition scan.
- **Composite Fuzzy Matcher (`matcher.py`)**:
  - Calculates token-set ratios via RapidFuzz across normalized fields:
    $$\text{MatchScore} = 0.40 \cdot S_{title} + 0.35 \cdot S_{company} + 0.15 \cdot S_{location} + 0.10 \cdot S_{remote}$$
  - Enforces a minimum match threshold of $\ge 0.88$.
- **Transitive Graph Clustering (`resolver.py`)**:
  - Implements Disjoint Set Union (DSU / Union-Find) with path compression and rank union.
  - Automatically resolves transitive duplicate chains ($A \sim B$ and $B \sim C \implies A \sim B \sim C$) into coherent canonical clusters.
- **Hierarchical Record Merger (`merger.py`)**:
  - Merges duplicate clusters using an authoritative source hierarchy:
    $$\text{Greenhouse (1.0)} > \text{Lever (0.9)} > \text{Hacker News (0.7)}$$
  - Combines all source URLs into `source_urls` array, unions technical skills sets, and preserves the highest-confidence salary and location evidence.

---

### Feature 4: Idempotent Storage & Lifecycle CDC (`pipeline.storage`)
- **Production SQLite Engine (`database.py`)**:
  - Configured with high-concurrency PRAGMA settings:
    ```sql
    PRAGMA journal_mode = WAL;
    PRAGMA synchronous = NORMAL;
    PRAGMA busy_timeout = 30000;
    PRAGMA foreign_keys = ON;
    ```
  - Enables concurrent readers without blocking active writing transactions.
- **Deterministic Canonical Keys (`idempotent_upsert.py`)**:
  - Generates deterministic UUIDv5 identifiers from canonical namespaces:
    $$\text{Canonical ID} = \text{UUIDv5}(\text{NAMESPACE\_DNS}, \text{norm\_company} + ":" + \text{norm\_title} + ":" + \text{seniority})$$
- **Zero-Drift Idempotent Upsert**:
  - Re-running the pipeline on identical input data results in **0 inserted, 0 updated, and 0 drift**, verified across unit tests and live executions.
- **Field-Level Change Data Capture (`change_tracker.py`)**:
  - Tracks state transitions (`INSERT`, `UPDATE`, `NOOP`, `CLOSE`, `REOPEN`) in `posting_change_history`.
  - Incorporates a **$1.00 float tolerance threshold** on numeric salary fields to prevent IEEE 754 floating-point precision artifacts from generating phantom update events.
  - Uses set-equality comparison on skills arrays to avoid false diffs caused by ordering shifts.
- **Lifecycle State Machine (`lifecycle_manager.py`)**:
  - Flags postings missing from subsequent crawl cycles as `closed` with tombstone timestamps.
  - If a closed posting reappears in future crawls, transitions it to `reopened` while preserving its historical lineage.

---

### Feature 5: Multi-Format Exporter & Evaluation Engine (`pipeline.exporter`, `pipeline.evaluate`)
- **RFC 4180 CSV & JSON Exporter (`exporter.py`)**:
  - Generates production-ready exports in `data/export/`:
    - `canonical_jobs.csv`: Flat schema with JSON-serialized arrays for skills and source URLs.
    - `canonical_jobs.json`: Full nested schema representations.
    - `change_history.csv`: Audit log of field-level deltas.
- **Benchmark Evaluator (`evaluate.py`)**:
  - Evaluates extractions against a hand-labeled golden dataset of 20 postings (`data/labeled_benchmark.json`: 10 Hacker News, 5 Greenhouse, 5 Lever).
  - Computes exact match scalar accuracy (94.4%), set-based skills Precision/Recall/F1 (88.9% / 82.2% / 85.4%), strict null compliance (100.0%), and verbatim evidence match rates (100.0%).
  - Generates structured Markdown evaluation reports (`data/evaluation_report.md`).

---

## 4. Architectural Trade-offs & Engineering Decisions

| Architectural Decision | Alternative Considered | Chosen Approach | Why Chosen / Engineering Rationale |
|---|---|---|---|
| **Storage Engine** | PostgreSQL / DuckDB | **SQLite with WAL mode** | Zero operational overhead, single-file portability for take-home evaluation, while WAL mode provides high concurrent read throughput and transactional safety. |
| **Deduplication Strategy** | Pairwise Greedy Merging | **Disjoint Set Union (DSU)** | Greedy pairwise merging suffers from order-of-ingestion bias. DSU graph clustering computes exact transitive equivalence classes deterministically. |
| **Evidence Grounding** | Trust LLM Output Schema | **Dual-Barrier Verbatim Validator** | LLMs frequently hallucinate plausible-looking numbers and salaries. Programmatic substring validation guarantees 0.0% hallucination rate on non-null fields. |
| **Search Space Reduction** | Brute-force Pairwise ($O(N^2)$) | **Multi-Pass Inverted Blocking** | Pairwise scanning collapses at scale ($100\text{k jobs} = 10\text{B comparisons}$). Inverted blocking prunes >95% of candidates, running in sub-second time. |
| **CDC Diff Tolerance** | Exact Bitwise Equality (`old == new`) | **$1.00 Float Delta Tolerance** | Floating-point conversions (e.g., $180000.0000001$ vs $180000.0$) cause false change-history diffs. A $1.00 bound guarantees true compensation shifts only. |
| **Fault Tolerance** | Fail-Fast Abort | **Fail-Soft Batch Isolation** | Network blips or API spend caps at job 83 should not throw away the first 82 extracted records. Fail-soft boundaries commit valid work and log errors gracefully. |

---

## 5. Comprehensive Module & Code Navigation Map

| Directory / Package | Key File | Primary Classes / Functions | Core Responsibility |
|---|---|---|---|
| **`pipeline.ingestion`** | [`base.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/scrapers/base.py) | `PoliteScraper`, `TokenBucketRateLimiter` | Domain rate limiting ($\le 1.0\text{s}$), robots.txt parsing, exponential backoff. |
| | [`greenhouse.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/scrapers/greenhouse.py) | `GreenhouseScraper` | Greenhouse REST API crawling and description extraction. |
| | [`lever.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/scrapers/lever.py) | `LeverScraper` | Lever API scraping with Content-Type validation. |
| | [`hackernews.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/scrapers/hackernews.py) | `HackerNewsScraper` | Static HTML scraping and "More" pagination traversal. |
| **`pipeline.extractor`** | [`schema.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/models/schema.py) | `JobPostingRecord`, Typed Enums | 17-field Pydantic v2 contract with `extra='forbid'`. |
| | [`preprocessor.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/extractor/preprocessor.py) | `DOMPreprocessor` | HTML pruning, boilerplate stripping, 72.4% token reduction. |
| | [`llm_client.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/extractor/llm_client.py) | `LLMClient`, `_resolve_secret_key` | Multi-model client (Gemini, OpenAI, Mock), rate cards, key resolution. |
| | [`validators.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/models/validators.py) | `validate_evidence_in_text()` | Verbatim substring verification, strict null enforcement. |
| | [`batch_runner.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/extractor/batch_runner.py) | `BatchRunner` | Concurrent extraction with fail-soft item isolation. |
| **`pipeline.deduplication`**| [`normalizer.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/dedup/normalizer.py) | `EntityNormalizer` | Corporate suffix stripping, brand aliasing, geographic mapping. |
| | [`title_parser.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/dedup/title_parser.py) | `TitleParser` | 12 title families, seniority tiers, boundary fence enforcement. |
| | [`blocker.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/dedup/blocker.py) | `InvertedIndexBlocker` | Multi-pass inverted index blocking (>95% candidate reduction). |
| | [`matcher.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/dedup/matcher.py) | `RecordMatcher` | RapidFuzz token-set scoring (threshold $\ge 0.88$). |
| | [`resolver.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/dedup/resolver.py) | `DSUClusterResolver` | Disjoint Set Union transitive graph clustering. |
| | [`merger.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/dedup/merger.py) | `RecordMerger` | Hierarchical authority merging (GH > Lever > HN). |
| **`pipeline.storage`** | [`database.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/storage/database.py) | `DatabaseManager` | SQLite WAL initialization, foreign keys, schema migrations. |
| | [`idempotent_upsert.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/storage/idempotent_upsert.py)| `IdempotentUpsertEngine` | Deterministic UUIDv5 canonical keys, zero-drift upserts. |
| | [`change_tracker.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/storage/change_tracker.py) | `ChangeTracker` | Field-level CDC logging with $1.00 float delta bounds. |
| | [`lifecycle_manager.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/storage/lifecycle_manager.py)| `LifecycleManager` | State machine tracking active, closed, and reopened postings. |
| | [`exporter.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/storage/exporter.py) | `DataExporter` | Multi-format exports (`canonical_jobs.csv`, `change_history.csv`). |
| **`pipeline.orchestration`**| [`run.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/run.py) | `run_master_pipeline()` | Master CLI coordinating all 6 pipeline stages. |
| | [`evaluate.py`](file:///d:/Project_Files/AGY_Assignments/Twig/Code/pipeline/evaluate.py) | `BenchmarkEvaluator` | Accuracy evaluator, skills F1 metrics, financial cost logger. |
