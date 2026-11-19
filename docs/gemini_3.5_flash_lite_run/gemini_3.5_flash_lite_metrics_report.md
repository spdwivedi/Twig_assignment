# Gemini 3.5 Flash-Lite Pipeline Execution Metrics Report

**Run Timestamp**: 2026-11-19 04:48:03 – 04:48:51 IST  
**Provider**: Google Gemini (`google-genai`)  
**Model**: `gemini-3.5-flash-lite`  
**Execution Status**: COMPLETED SUCCESSFULLY  
**Duration**: 48.089 seconds  

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
| | Successful Extractions | 112 | postings |
| | Failed Postings (Fail-Soft Quarantined) | 0 | postings |
| | Extraction Success Rate | 100.00% | % |
| | Extraction Batch Duration | 33.11 | seconds |
| | Extraction Batch Throughput | 3.38 | postings/s |
| **Stage 3 (Deduplication)** | Extracted Postings Evaluated | 112 | postings |
| | Candidate Blocks Formed | 40 | blocks |
| | Pairwise Comparisons Executed | 761 | comparisons |
| | Matching Pairs Identified | 68 | pairs |
| | Connected Component Clusters | 75 | clusters |
| | Cross-Source Duplicates Merged | 37 | postings |
| | Unique Canonical Entities Formed | 75 | entities |
| | Deduplication Compression Ratio | 33.04% | % |
| **Stage 4 (Database & CDC)**| Canonical Entities Evaluated | 75 | rows |
| | Newly Inserted Entities | 34 | rows |
| | Unchanged Entities | 41 | rows |
| | Updated Entities | 0 | rows |
| | Closed Postings | 0 | rows |
| **Stage 6 (Export)** | Cumulative Canonical Jobs Exported | 92 | rows |
| | Canonical CSV File Size | 40.8 | KB |
| | Canonical JSON File Size | 103.5 | KB |

---

## 2. Financial & Token Consumption Accounting

| Token Metric | Quantity | Rate Card (per 1M) | Total Cost (USD) |
|---|:---:|:---:|:---:|
| **Prompt Tokens** | 665,765 | $0.075 | $0.049932 |
| **Completion Tokens** | 17,543 | $0.300 | $0.005263 |
| **Total LLM Tokens** | **683,308** | — | **$0.055195** |
| **Average Cost per Processed Posting** | 6,101 tokens | — | **$0.000493** |
| **Average Cost per Successful Extraction** | 6,101 tokens | — | **$0.000493** |

---

## 3. Resilience & Exception Handling Audit

| Metric | Measured Value | Target / Specification |
|---|:---:|:---:|
| **API Errors / 429 Statuses Encountered** | 0 | None (Zero rate limits hit) |
| **Failed Postings** | 0 | 100% completion rate |
| **Anti-Hallucination Guard Status** | Active (Caught non-verbatim quotes) | Strictly preserved schema nulls |
| **Data Preservation** | 100% of 112 extractions saved | Zero data loss |
| **Final Exit Code** | `0` (Success) | 0 |
