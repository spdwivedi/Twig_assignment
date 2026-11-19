# Gemini 4 Argon Pipeline Execution Metrics Report

**Run Timestamp**: 2026-11-19 04:51:19 – 04:54:05 IST  
**Provider**: Google Gemini (`google-genai`)  
**Model**: `gemini-4-argon`  
**Execution Status**: COMPLETED SUCCESSFULLY  
**Duration**: 166.547 seconds  

---

## 1. Stage Funnel & Throughput

| Pipeline Stage | Metric Description | Value | Unit |
|---|---|:---:|:---:|
| **Stage 1 (Ingestion)** | Total Raw Postings Crawled | 2,576 | postings |
| | Greenhouse REST API Postings | 2,027 | postings |
| | Lever REST API Postings | 306 | postings |
| | Hacker News Thread Comments | 243 | postings |
| **Stage 1b (Scope Filter)** | Pre-Filtered Non-Target Postings | 2,464 | postings |
| | Retained US Data/ML Candidate Postings | 112 | postings |
| | Pre-Filter Token Savings | ~1,500,000 | input tokens |
| **Stage 2 (LLM Extraction)** | Total Postings Processed | 112 | postings |
| | Successful Extractions | 82 | postings |
| | Failed Postings (Fail-Soft Quarantined) | 30 | postings |
| | Extraction Success Rate | 73.21% | % |
| | Extraction Batch Duration | 150.97 | seconds |
| | Extraction Batch Throughput | 0.74 | postings/s |
| **Stage 3 (Deduplication)** | Extracted Postings Evaluated | 82 | postings |
| | Candidate Blocks Formed | 10 | blocks |
| | Pairwise Comparisons Executed | 761 | comparisons |
| | Matching Pairs Identified | 68 | pairs |
| | Connected Component Clusters | 45 | clusters |
| | Cross-Source Duplicates Merged | 37 | postings |
| | Unique Canonical Entities Formed | 45 | entities |
| | Deduplication Compression Ratio | 45.12% | % |
| **Stage 4 (Database & CDC)**| Canonical Entities Evaluated | 45 | rows |
| | Newly Inserted Entities | 3 | rows |
| | Unchanged Entities | 42 | rows |
| | Updated Entities | 0 | rows |
| | Closed Postings Detected | 0 | rows |
| **Stage 6 (Export)** | Cumulative Canonical Jobs Exported | 95 | rows |
| | Canonical CSV File Size | 42.4 | KB |
| | Canonical JSON File Size | 107.9 | KB |
| | Change History Records | 8 | rows (1.2 KB) |

---

## 2. Financial & Token Consumption Accounting

| Token Metric | Quantity | Rate Card (per 1M) | Total Cost (USD) |
|---|:---:|:---:|:---:|
| **Prompt Tokens** | 489,883 | $0.15 | $0.073482 |
| **Completion Tokens** | 13,792 | $0.60 | $0.008275 |
| **Total LLM Tokens** | **503,675** | — | **$0.081758** |
| **Average Cost per Processed Posting** | 4,497 tokens | — | **$0.000730** |
| **Average Cost per Successful Extraction** | 6,142 tokens | — | **$0.000997** |

---

## 3. Resilience & Exception Handling Audit

| Metric | Measured Value | Target / Specification |
|---|:---:|:---:|
| **Exceptions Encountered** | HTTP 429 (`RESOURCE_EXHAUSTED: monthly spending cap`) | Expected when cap reached |
| **Retry Backoff Schedule** | 1.00s (Attempt 1), 1.50s (Attempt 2) | Exponential backoff active |
| **Error Isolation** | Per-posting exception isolation (`BatchRunner`) | Zero pipeline termination |
| **Data Preservation** | 100% of the 82 processed records committed | Zero data loss on failure |
| **Final Exit Code** | `0` (Success) | 0 |
