# Twig Data Engineer Pipeline - Benchmark Evaluation Report

- **Evaluation Timestamp**: `2026-11-18T20:50:22.195375+00:00`
- **Benchmark Dataset**: `data\labeled_benchmark.json`
- **Total Postings Evaluated**: `20`
- **Execution Mode**: `Hermetic Offline Mock` (`mock-extractor`)

---

## 1. Executive Summary & Source Breakdown

| Source | Postings | Exact Match Acc (%) | Skills F1 (%) | Strict Null Compliance (%) | Verbatim Evidence Rate (%) | Avg Cost / Job ($) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Greenhouse** | 5 | 97.8% | 77.7% | 100.0% | 100.0% | $0.000092 |
| **Hacker News** | 10 | 94.5% | 89.0% | 100.0% | 100.0% | $0.000092 |
| **Lever** | 5 | 91.1% | 86.5% | 100.0% | 100.0% | $0.000092 |
| **OVERALL** | **20** | **94.4%** | **85.4%** | **100.0%** | **100.0%** | **$0.000092** |

---

## 2. Field-Level Exact Match Accuracy

| Field Name | Evaluated Records | Exact Matches | Accuracy (%) | Rubric / Evaluation Notes |
| :--- | :---: | :---: | :---: | :--- |
| `company` | 20 | 20 | **100.0%** | Normalized corporate legal identity |
| `title` | 20 | 20 | **100.0%** | Canonical job title preservation |
| `seniority` | 20 | 16 | **80.0%** | Strict enum bounds (junior, mid, senior, staff_plus) |
| `remote_policy` | 20 | 19 | **95.0%** | Strict enum bounds (onsite, hybrid, remote) |
| `salary_min` | 20 | 17 | **85.0%** | Annualized minimum bound (within $1.00 float delta) |
| `salary_max` | 20 | 19 | **95.0%** | Annualized maximum bound (within $1.00 float delta) |
| `currency` | 20 | 20 | **100.0%** | Strict null when salary unstated; standard ISO code |
| `employment_type` | 20 | 20 | **100.0%** | Strict enum bounds (full_time, contract, intern) |
| `visa_sponsorship` | 20 | 19 | **95.0%** | Strict boolean / null adherence (no unstated guessing) |
| **Overall Scalar Average** | **180** | **170** | **94.4%** | **Exceeds benchmark target threshold** |

---

## 3. Technical Skills Extraction Metrics (Set-Based Overlap)

| Metric | Score (%) | Absolute Breakdown | Significance |
| :--- | :---: | :---: | :--- |
| **Micro Precision** | **88.9%** | TP: 88 / (TP + FP: 99) | Accuracy of extracted skills against ground truth |
| **Micro Recall** | **82.2%** | TP: 88 / (TP + FN: 107) | Coverage of required ground-truth skills captured |
| **Micro F1 Score** | **85.4%** | Harmonic Mean | Global balance of precision and recall |
| **Macro F1 Score** | **85.5%** | Per-Posting Average | Mean F1 across heterogeneous job descriptions |

---

## 4. Strict Null Compliance Analysis

> [!IMPORTANT]
> **The Strict Null Rule**: Unstated attributes in raw job text must evaluate strictly to `null`.
> Guessing compensation, assuming visa sponsorship, or fabricating seniority is strictly penalized.

| Metric | Count | Rate (%) | Description |
| :--- | :---: | :---: | :--- |
| Total Unstated Ground-Truth Fields | 13 | 100.0% | Baseline fields requiring null evaluation |
| Correctly Nulled Fields | 13 | **100.0%** | Extractor correctly refrained from guessing |
| False Guesses / Hallucinations | 0 | 0.0% | Penalized non-null extrapolations |

---

## 5. Verbatim Evidence Grounding Audit

| Metric | Count | Rate (%) | Architectural Defense |
| :--- | :---: | :---: | :--- |
| Total Populated Extracted Fields | 203 | 100.0% | Non-null extracted attributes |
| Verbatim Verified Evidence Snippets | 203 | **100.0%** | Evidence quote verified verbatim in source text |
| Ungrounded / Fabricated Snippets | 0 | 0.0% | Nullified prior to database ingestion |

---

## 6. Token & LLM Cost Accounting

| Accounting Metric | Value | Reference Basis |
| :--- | :---: | :--- |
| **Prompt Tokens** | 11,699 | $0.075 per 1M tokens |
| **Completion Tokens** | 3,223 | $0.300 per 1M tokens |
| **Total Tokens Consumed** | 14,922 | 20 benchmark items |
| **Total Evaluation Cost** | **$0.001844** | Cumulative inference expenditure |
| **Average Cost per Job** | **$0.000092** | **~$0.092 per 1,000 postings** |
