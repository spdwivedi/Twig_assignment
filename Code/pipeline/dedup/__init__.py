"""Deduplication and cross-source entity resolution module for the job posting pipeline.

Exports:
- CompanyNormalizer, LocationNormalizer: Corporate legal suffix stripping and geographic normalization.
- TitleParser, ParsedTitle: Standardizes titles, extracts seniority, role family, and specialization.
- CandidateBlocker: Multi-pass candidate duplicate blocker and inverted indexing engine.
- JobPostingMatcher, MatchResult: 7-step similarity engine for fuzzy matching and deduplication.
- RecordMerger: Source authority prioritized golden record merger.
- DeduplicationEngine: Cross-source clustering and entity resolution orchestrator.
- DisjointSetUnion: DSU / Union-Find data structure for connected components.
"""

from pipeline.dedup.blocker import CandidateBlocker
from pipeline.dedup.matcher import JobPostingMatcher, MatchResult
from pipeline.dedup.merger import (
    DEFAULT_SOURCE_AUTHORITY,
    RecordMerger,
    extract_source_name,
)
from pipeline.dedup.normalizer import CompanyNormalizer, LocationNormalizer
from pipeline.dedup.resolver import (
    DeduplicationEngine,
    DefaultCandidateBlocker,
    DefaultCompanyNormalizer,
    DefaultJobPostingMatcher,
    DefaultTitleParser,
    DisjointSetUnion,
)
from pipeline.dedup.title_parser import ParsedTitle, TitleParser

__all__ = [
    # Normalization & Parsing
    "CompanyNormalizer",
    "LocationNormalizer",
    "TitleParser",
    "ParsedTitle",
    # Candidate Blocking
    "CandidateBlocker",
    # Pairwise Matching
    "JobPostingMatcher",
    "MatchResult",
    # Record Merging & Canonical Resolution
    "RecordMerger",
    "extract_source_name",
    "DEFAULT_SOURCE_AUTHORITY",
    # Deduplication Orchestration & Graph Resolution
    "DeduplicationEngine",
    "DisjointSetUnion",
    # Resilient Defaults
    "DefaultCompanyNormalizer",
    "DefaultTitleParser",
    "DefaultCandidateBlocker",
    "DefaultJobPostingMatcher",
]

