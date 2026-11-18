# Twig Data Engineer Take-Home Assignment

Production-minded Python pipeline that scrapes job postings from Greenhouse, Lever, and Hacker News, extracts clean structured records with an LLM adhering to a strict Pydantic schema, deduplicates records across sources, maintains change history across idempotent re-runs, and benchmarks accuracy on a labeled evaluation set.

## Project Structure
- `Plan/`: Detailed 5-phase implementation and architectural specifications.
  - [Five Phase Implementation Plan](Plan/Five_Phase_Implementation_Plan.md)
  - [Phase 1: Ingestion & Polite Scraping](Plan/Phase_1_Ingestion_and_Scraping.md)
  - [Phase 2: LLM Extraction & Schema Validation](Plan/Phase_2_LLM_Extraction_and_Validation.md)
  - [Phase 3: Deduplication & Cross-Source Resolution](Plan/Phase_3_Deduplication_and_Entity_Resolution.md)
  - [Phase 4: Storage, Idempotency & Change History](Plan/Phase_4_Storage_Idempotency_and_Change_History.md)
  - [Phase 5: Evaluation, Benchmark, Tests & Scaling](Plan/Phase_5_Evaluation_Testing_and_Documentation.md)
- `Task/`: Original take-home assignment documentation and requirements.
- `Code/`: Source code implementation (scrapers, LLM extractor, dedup engine, database storage, test suite).
