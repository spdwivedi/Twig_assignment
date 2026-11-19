# Default Mock Pipeline Execution Metrics Report

**Run Timestamp**: 2026-11-19 04:44:23 IST  
**Provider**: Offline Mock (`mock-extractor`)  
**Execution Status**: COMPLETED SUCCESSFULLY  
**Duration**: 0.307 seconds  

---

## 1. Stage Funnel & Throughput

| Pipeline Stage | Metric Description | Value | Unit |
|---|---|:---:|:---:|
| **Stage 1 (Ingestion)** | Synthetic Fixtures Ingested | 11 | postings |
| | Hacker News Postings | 4 | postings |
| | Greenhouse Postings | 4 | postings |
| | Lever Postings | 3 | postings |
| **Stage 1b (Scope Filter)** | Pre-Filtered Non-Target Postings | 3 | postings |
| | Retained Candidate Postings | 8 | postings |
| **Stage 2 (Mock Extraction)**| Total Postings Processed | 8 | postings |
| | Successful Extractions | 8 | postings |
| | Failed Postings | 0 | postings |
| | Extraction Success Rate | 100.00% | % |
| | Verbatim Evidence Grounding Rate | 100.00% | % (8/8) |
| | Hallucinations Detected & Nulled | 0 | items |
| | Batch Extraction Throughput | 756.94 | postings/s |
| **Stage 3 (Deduplication)** | Extracted Postings Evaluated | 8 | postings |
| | Cross-Source Duplicates Merged | 1 | postings |
| | Unique Canonical Entities Formed | 7 | entities |
| **Stage 4 (Database & CDC)**| Canonical Entities Evaluated | 7 | rows |
| | **Idempotent Re-Run Check** | **0 inserted, 0 updated, 7 unchanged, 0 closed** | rows |
| **Stage 6 (Export)** | Cumulative Canonical Jobs in Database | 58 | rows (28.8 KB) |
| | Cumulative Change History Records | 8 | rows (1.2 KB) |

---

## 2. Financial & Token Accounting (Simulated)

| Token Metric | Quantity | Simulated Rate (per 1M) | Total Cost (USD) |
|---|:---:|:---:|:---:|
| **Prompt Tokens** | 4,035 | $0.15 | $0.000605 |
| **Completion Tokens** | 1,468 | $0.60 | $0.000881 |
| **Total LLM Tokens** | **5,503** | — | **$0.000743** |
| **Actual Billed Cost** | 0 tokens | $0.00 | **$0.000000 (Free Offline Mode)** |
| **Average Cost per Posting** | 688 tokens | — | **$0.000093 (Simulated)** |
