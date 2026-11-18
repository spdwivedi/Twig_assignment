"""Data models, enums, schemas, and validators for the job posting pipeline.

Exports:
- Strict Enums: SeniorityEnum, RemotePolicyEnum, EmploymentTypeEnum, StatusEnum
- Core Models: JobPostingRecord, RawExtractionOutput
- Deterministic Hashing: generate_deterministic_id
- Anti-Hallucination Validators: validate_salary, normalize_annual_salary,
  validate_evidence_presence, validate_evidence_in_text, validate_record_evidence
"""

from pipeline.models.schema import (
    EmploymentTypeEnum,
    JobPostingRecord,
    RawExtractionOutput,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
    generate_deterministic_id,
)
from pipeline.models.validators import (
    CURRENCY_ANNUAL_BOUNDS,
    DEFAULT_ANNUAL_BOUNDS,
    FIELD_EVIDENCE_ALIASES,
    normalize_annual_salary,
    validate_evidence_in_text,
    validate_evidence_presence,
    validate_record_evidence,
    validate_salary,
)

__all__ = [
    # Strict Enums
    "SeniorityEnum",
    "RemotePolicyEnum",
    "EmploymentTypeEnum",
    "StatusEnum",
    # Schemas & Models
    "JobPostingRecord",
    "RawExtractionOutput",
    "generate_deterministic_id",
    # Validation & Normalization Functions
    "validate_salary",
    "normalize_annual_salary",
    "validate_evidence_presence",
    "validate_evidence_in_text",
    "validate_record_evidence",
    # Constants
    "CURRENCY_ANNUAL_BOUNDS",
    "DEFAULT_ANNUAL_BOUNDS",
    "FIELD_EVIDENCE_ALIASES",
]
