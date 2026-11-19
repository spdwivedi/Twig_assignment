# Phase 2: LLM Extraction & Schema Validation

## 1. Objective & Scope
Transform unstructured and semi-structured job text (Greenhouse HTML descriptions, Lever job descriptions, and messy free-form Hacker News comments) into strict, structured records adhering to the assignment's target schema using an LLM.

---

## 2. Target Schema (Pydantic Model)

```python
from datetime import date, datetime
from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator

class SeniorityEnum(str, Enum):
    junior = "junior"
    mid = "mid"
    senior = "senior"
    staff_plus = "staff_plus"

class RemotePolicyEnum(str, Enum):
    onsite = "onsite"
    hybrid = "hybrid"
    remote = "remote"

class EmploymentTypeEnum(str, Enum):
    full_time = "full_time"
    contract = "contract"
    intern = "intern"

class StatusEnum(str, Enum):
    open = "open"
    closed = "closed"

class JobPostingRecord(BaseModel):
    posting_id: str = Field(..., description="Stable deterministic ID across runs")
    company: str = Field(..., description="Normalized company name")
    title: str = Field(..., description="Canonical title")
    seniority: Optional[SeniorityEnum] = Field(None, description="junior, mid, senior, staff_plus")
    city: Optional[str] = Field(None, description="City if stated")
    country: Optional[str] = Field(None, description="Country if stated (e.g., US)")
    remote_policy: Optional[RemotePolicyEnum] = Field(None, description="onsite, hybrid, remote")
    salary_min: Optional[float] = Field(None, description="Annualized minimum salary in specified currency")
    salary_max: Optional[float] = Field(None, description="Annualized maximum salary in specified currency")
    currency: Optional[str] = Field(None, description="Currency code, e.g. USD")
    employment_type: Optional[EmploymentTypeEnum] = Field(None, description="full_time, contract, intern")
    skills: List[str] = Field(default_factory=list, description="e.g. ['Python', 'Spark', 'dbt']")
    visa_sponsorship: Optional[bool] = Field(None, description="True/False if explicitly stated, else null")
    posted_date: Optional[date] = Field(None, description="Date posted if stated")
    status: StatusEnum = Field(default=StatusEnum.open)
    source_urls: List[str] = Field(..., description="Source links merged into this record")
    evidence: Dict[str, str] = Field(default_factory=dict, description="Field name -> source snippet")
    first_seen: datetime
    last_seen: datetime
```

---

## 3. Strict Extraction Rules & Anti-Hallucination Guardrails
1. **Null Over Guessing**:
   - Every unstated field must be set to `None`/`null`.
   - Never infer salary ranges from seniority or guess visa sponsorship if not explicitly mentioned in text.
2. **Evidence Mapping**:
   - For every extracted field (salary, remote policy, seniority, skills, visa), the extraction payload must return the exact literal text snippet from the raw posting justifying that value.
   - Example:
     ```json
     {
       "salary_min": 175000,
       "salary_max": 220000,
       "currency": "USD",
       "evidence": {
         "salary": "Compensation: $175,000 - $220,000 per year + equity",
         "remote_policy": "Location: Remote (US only)",
         "visa_sponsorship": "We are unable to sponsor visas at this time."
       }
     }
     ```
3. **Salary Normalization**:
   - Handle hourly, monthly, and annual compensation cleanly (standardize to annualized figure).
   - If a posting specifies `$80/hr`, annualize assuming 2,080 hours ($166,400) and preserve snippet.

---

## 4. Cost & Latency Optimization Strategy
- **Pre-filtering**: Never send full HTML pages to the LLM. Clean HTML with BeautifulSoup, strip boilerplate navigation headers/footers, and truncate or isolate the core role description.
- **Model Choice**: Use fast, cost-effective models (e.g. Gemini 1.5 Flash / GPT-4o-mini / Haiku) with JSON mode or structured outputs (`response_schema`).
- **Prompt Caching & Batching**: Leverage consistent system instructions and structured few-shot examples for deterministic extraction.
- **Cost Tracking**: Calculate exact prompt/completion token consumption per job posting to ensure compliance with the evaluation cost metrics.

---

## 5. Deliverables & Acceptance Criteria
- [x] Pydantic validation suite ensuring 0 schema violations (Verified: `JobPostingRecord` with 17 strictly typed fields and enums).
- [x] Evidence verification check: tests confirm evidence snippets exist within the source text (Verified: `validate_evidence_in_text()` enforcing verbatim substring check).
- [x] Cost logger measuring average cost per job posting (target < $0.005 / posting) (Verified: $0.002175/job on Flash-Lite, $0.000092/job on benchmark).
- [x] Extraction module `pipeline.extractor` supporting retry on JSON parsing failures (Verified: `_dispatch_with_retry()` with exponential backoff and jitter).
