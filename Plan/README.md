# Twig Engineering Plans & Step Alignment

This directory contains the strategic roadmaps, phase specifications, and task alignment matrices used to engineer the **Twig Data Engineering Pipeline**.

---

## 1. Directory Contents

| Document | Purpose & Scope |
|---|---|
| [**`Task_Specification_and_Step_Alignment.md`**](file:///d:/Project_Files/AGY_Assignments/Twig/Plan/Task_Specification_and_Step_Alignment.md) | **Authoritative Specification & Step Compliance Matrix**: Detailed breakdown mapping every client requirement to pipeline implementation, multi-model execution, and verification proof. |
| [**`Five_Phase_Implementation_Plan.md`**](file:///d:/Project_Files/AGY_Assignments/Twig/Plan/Five_Phase_Implementation_Plan.md) | **Master Strategic Architecture**: Executive 5-phase breakdown covering Ingestion, LLM Extraction, DSU Deduplication, SQLite WAL Storage, and Evaluation. |
| [**`Phase_1_Ingestion_and_Scraping.md`**](file:///d:/Project_Files/AGY_Assignments/Twig/Plan/Phase_1_Ingestion_and_Scraping.md) | Phase 1 plan: Scraper design, rate limiting ($\le 1.0\text{ req/s}$), and in-memory response caching. |
| [**`Phase_2_LLM_Extraction_and_Validation.md`**](file:///d:/Project_Files/AGY_Assignments/Twig/Plan/Phase_2_LLM_Extraction_and_Validation.md) | Phase 2 plan: LLM client abstraction, strict null rule, evidence verification, and multi-model rate cards. |
| [**`Phase_3_Deduplication_and_Entity_Resolution.md`**](file:///d:/Project_Files/AGY_Assignments/Twig/Plan/Phase_3_Deduplication_and_Entity_Resolution.md) | Phase 3 plan: Title family categorization, blocking keys, Jaro-Winkler fuzzy matching, and DSU clustering. |
| [**`Phase_4_Storage_Idempotency_and_Change_History.md`**](file:///d:/Project_Files/AGY_Assignments/Twig/Plan/Phase_4_Storage_Idempotency_and_Change_History.md) | Phase 4 plan: SQLite WAL transactions, idempotent upsert hashes, and change data capture logging. |
| [**`Phase_5_Evaluation_Testing_and_Documentation.md`**](file:///d:/Project_Files/AGY_Assignments/Twig/Plan/Phase_5_Evaluation_Testing_and_Documentation.md) | Phase 5 plan: Labeled benchmark dataset curation, exact match accuracy, skills F1, and cost auditing. |

---

## 2. Engineering Standards & Deliverables Verification

All plans adhere strictly to:
- **100% Deliverable Completion**: Every acceptance criterion across all five phase specifications is **fully verified and checked** (`[x]`).
- **Test-Driven Verification**: 1,006 automated tests passing across 24 test suites with zero regressions.
- **Defense-in-Depth Anti-Hallucination**: Dual-barrier verbatim substring verification rejecting ungrounded LLM outputs.
- **Relational Integrity**: SQLite WAL mode with idempotent upserts, foreign key enforcement, and field-level CDC tracking.
