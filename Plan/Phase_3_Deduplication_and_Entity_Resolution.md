# Phase 3: Deduplication & Cross-Source Entity Resolution

## 1. Objective & Scope
The same job opening frequently appears across multiple sources (e.g., direct Greenhouse ATS, Lever feed, and Hacker News posts) with slight variations in titles, formatting, or descriptions (e.g. *"Sr. Data Engineer"* vs *"Senior Data Engineer, Platform"* vs *"Data Engineer - Data Platform"*).
This phase defines deterministic matching and entity resolution to merge duplicate postings into a single canonical record while preserving all source references and auditing history.

---

## 2. Deduplication Architecture

```
Raw Ingested Records (Greenhouse, Lever, HN)
                  │
                  ▼
         [Standardization Stage]
    - Company name normalization (e.g. "Stripe, Inc." -> "Stripe")
    - Title normalization (strip tags, levels, team affixes)
    - Location normalization (US state codes, Remote flags)
                  │
                  ▼
         [Candidate Blocking]
    - Block on: (normalized_company, remote_policy_or_state)
                  │
                  ▼
         [Similarity Scoring]
    - Exact Match: company + exact title + location
    - Fuzzy Match: RapidFuzz token_set_ratio > 85 on title within same company block
    - Cross-reference: Direct URL matching across careers pages & ATS
                  │
                  ▼
         [Canonical Record Merge]
    - Combine source_urls: list(set(existing_urls + new_urls))
    - Prioritize ATS structured data over raw HN text for canonical fields
    - Preserve earliest first_seen and latest last_seen
```

---

## 3. Matching Rules & Edge Cases

### A. Company Normalization
- Strip legal entities (`Inc`, `LLC`, `Corp`, `Technologies`, `Labs`).
- Lowercase and whitespace normalization.
- Mapping table for known alias variations (e.g. `Databricks Inc.` -> `Databricks`, `Palantir Technologies` -> `Palantir`).

### B. Title Standardization & Seniority Disambiguation
- Disentangle title tokens:
  - Seniority: `Junior`, `Mid`, `Senior`, `Lead`, `Staff`, `Principal`, `Director`.
  - Core role: `Data Engineer`, `Machine Learning Engineer`, `Analytics Engineer`, `Platform Engineer`.
- Differentiate distinct concurrent roles at the same company:
  - *"Staff Data Engineer - Core Infrastructure"* vs *"Senior Data Engineer - Product Analytics"* must **not** be merged.
  - Require both high title similarity (>88%) and compatible seniority levels.

### C. Hacker News Attribution
- HN comments often do not link to the ATS requisition ID.
- Match on company name + extracted role title + approximate posted date window.
- When an HN comment matches an existing ATS record, merge the HN discussion URL into `source_urls` and supplement missing metadata if unpopulated in ATS.

---

## 4. Conflict Resolution Strategy
When duplicate sources report differing details:
1. **Source Authority Hierarchy**: Greenhouse/Lever ATS API > Direct Careers Page > HN Forum Post.
2. **Salary Ranges**: If one source has salary and another is null, take the non-null value and combine evidence snippets. If both have ranges, take the most recent posting's range and log an audit trail entry.
3. **Skills**: Union of unique normalized skills across all duplicate sources.

---

## 5. Deliverables & Acceptance Criteria
- [ ] Deduplication module `pipeline.dedup` with clear, deterministic matching algorithms.
- [ ] Automated test suite with edge cases:
  - Same company, identical title, different source -> Merge.
  - Same company, Senior vs Staff -> Separate canonical records.
  - Same company, Data Engineer vs ML Engineer -> Separate canonical records.
  - Same posting re-ingested on subsequent runs -> 0 duplicates created.
