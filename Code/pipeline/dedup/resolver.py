"""Deduplication Engine and Entity Resolution Orchestrator.

Orchestrates company normalization, title parsing, candidate blocking,
pairwise matching, and golden record merging. Utilizes Disjoint Set Union (DSU)
to identify connected components (transitive clusters) and persists canonical
job entities into SQLite storage.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from rapidfuzz import fuzz

from pipeline.config import DB_PATH
from pipeline.dedup.merger import RecordMerger
from pipeline.models.canonical import CanonicalJobPosting
from pipeline.models.schema import (
    EmploymentTypeEnum,
    JobPostingRecord,
    RemotePolicyEnum,
    SeniorityEnum,
    StatusEnum,
)

logger = logging.getLogger(__name__)


# ==============================================================================
# Disjoint Set Union (DSU / Union-Find)
# ==============================================================================


class DisjointSetUnion:
    """Disjoint Set Union (Union-Find) with path compression and union by rank.

    Guarantees nearly linear O(N * alpha(N)) time complexity for connected
    component clustering and transitive entity resolution.
    """

    def __init__(self) -> None:
        """Initialize empty DSU structure."""
        self.parent: Dict[str, str] = {}
        self.rank: Dict[str, int] = {}

    def find(self, item: str) -> str:
        """Find the representative root of an item with path compression."""
        if item not in self.parent:
            self.parent[item] = item
            self.rank[item] = 0
            return item

        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, item1: str, item2: str) -> bool:
        """Unify the components containing item1 and item2 using rank heuristic.

        Returns:
            True if two disjoint sets were merged, False if already connected.
        """
        root1 = self.find(item1)
        root2 = self.find(item2)

        if root1 == root2:
            return False

        # Union by rank
        if self.rank[root1] < self.rank[root2]:
            self.parent[root1] = root2
        elif self.rank[root1] > self.rank[root2]:
            self.parent[root2] = root1
        else:
            self.parent[root2] = root1
            self.rank[root1] += 1
        return True

    def get_clusters(self, items: List[str]) -> List[List[str]]:
        """Groups items into their respective connected component clusters."""
        clusters: Dict[str, List[str]] = {}
        for item in items:
            root = self.find(item)
            clusters.setdefault(root, []).append(item)
        return list(clusters.values())


# ==============================================================================
# Resilient Fallback Components
# ==============================================================================


class DefaultCompanyNormalizer:
    """Robust built-in company name normalizer with legal suffix stripping."""

    KNOWN_ALIASES: Dict[str, str] = {
        "stripe, inc.": "Stripe",
        "stripe inc": "Stripe",
        "stripe": "Stripe",
        "databricks inc.": "Databricks",
        "databricks inc": "Databricks",
        "databricks": "Databricks",
        "palantir technologies": "Palantir",
        "palantir technologies inc": "Palantir",
        "palantir": "Palantir",
        "airbnb, inc.": "Airbnb",
        "airbnb inc": "Airbnb",
        "airbnb": "Airbnb",
        "figma inc": "Figma",
        "figma, inc.": "Figma",
        "figma": "Figma",
        "plaid inc": "Plaid",
        "plaid": "Plaid",
        "cloudflare, inc.": "Cloudflare",
        "cloudflare inc": "Cloudflare",
        "cloudflare": "Cloudflare",
        "gitlab, inc.": "GitLab",
        "gitlab inc": "GitLab",
        "gitlab": "GitLab",
    }

    LEGAL_SUFFIXES: Tuple[str, ...] = (
        "inc.", "inc", "llc.", "llc", "corp.", "corp", "corporation",
        "ltd.", "ltd", "limited", "technologies", "tech", "labs",
        "holdings", "co.", "co", "group", "gmbh", "pvt", "plc",
    )

    def normalize(self, company: Optional[str]) -> str:
        """Normalize company name to canonical representation."""
        if not company:
            return "Unknown"

        raw = company.strip()
        lower = raw.lower()

        # Check known dictionary mapping
        if lower in self.KNOWN_ALIASES:
            return self.KNOWN_ALIASES[lower]

        # Strip legal suffixes
        words = re.findall(r"\b[\w\-]+\b", lower)
        cleaned_words = []
        for w in words:
            if w in self.LEGAL_SUFFIXES:
                continue
            cleaned_words.append(w)

        if not cleaned_words:
            cleaned_words = words

        normalized = " ".join(cleaned_words).title()
        return normalized or raw.title()


class DefaultTitleParser:
    """Standardizes job titles and extracts seniority levels."""

    TAG_PATTERN = re.compile(
        r"(\[.*?\]|\(.*?\)|(?:\b(remote|hybrid|onsite|full[-\s]time|contract|us|emea|apac)\b))",
        re.IGNORECASE,
    )

    SENIORITY_KEYWORDS: Dict[str, SeniorityEnum] = {
        "junior": SeniorityEnum.junior,
        "entry": SeniorityEnum.junior,
        "associate": SeniorityEnum.junior,
        "mid": SeniorityEnum.mid,
        "senior": SeniorityEnum.senior,
        "sr": SeniorityEnum.senior,
        "lead": SeniorityEnum.staff_plus,
        "principal": SeniorityEnum.staff_plus,
        "staff": SeniorityEnum.staff_plus,
        "head": SeniorityEnum.staff_plus,
        "director": SeniorityEnum.staff_plus,
        "vp": SeniorityEnum.staff_plus,
    }

    def parse(self, title: Optional[str]) -> Dict[str, Any]:
        """Parses title string to extract clean canonical title and seniority."""
        if not title:
            return {"title": "Untitled Role", "seniority": None}

        t_clean = title.strip()
        # Detect seniority
        lower_title = t_clean.lower()
        detected_seniority: Optional[SeniorityEnum] = None
        for kw, sen in self.SENIORITY_KEYWORDS.items():
            if re.search(rf"\b{re.escape(kw)}\b", lower_title):
                detected_seniority = sen
                break

        # Strip bracketed metadata or trailing location tags
        stripped = self.TAG_PATTERN.sub(" ", t_clean)
        # Remove multiple dashes or pipes
        stripped = re.sub(r"[\-|–]\s*(remote|us|hybrid|emea|apac|hybrid|full\s*time).*", "", stripped, flags=re.IGNORECASE)
        clean_title = re.sub(r"\s+", " ", stripped).strip(" ,-|–/()[]")

        return {
            "title": clean_title or t_clean,
            "seniority": detected_seniority,
        }


class DefaultCandidateBlocker:
    """Partitions job postings into candidate blocks to avoid O(N^2) comparisons."""

    def __init__(self, normalizer: Optional[Any] = None) -> None:
        self.normalizer = normalizer or DefaultCompanyNormalizer()

    def block(self, postings: List[JobPostingRecord]) -> Dict[str, List[JobPostingRecord]]:
        """Groups postings by normalized company blocking key."""
        blocks: Dict[str, List[JobPostingRecord]] = {}
        for p in postings:
            comp_norm = self.normalizer.normalize(p.company).lower()
            blocks.setdefault(comp_norm, []).append(p)
        return blocks


class DefaultJobPostingMatcher:
    """Pairwise matcher evaluating similarity between job postings within a block."""

    def __init__(
        self,
        title_threshold: float = 85.0,
        normalizer: Optional[Any] = None,
        title_parser: Optional[Any] = None,
    ) -> None:
        self.title_threshold = title_threshold
        self.normalizer = normalizer or DefaultCompanyNormalizer()
        self.title_parser = title_parser or DefaultTitleParser()

    def is_match(self, rec1: JobPostingRecord, rec2: JobPostingRecord) -> bool:
        """Determines whether two job posting records represent the exact same opening."""
        # 1. Company check
        c1 = self.normalizer.normalize(rec1.company).lower()
        c2 = self.normalizer.normalize(rec2.company).lower()
        if c1 != c2:
            return False

        # 2. URL Cross-Reference Match
        urls1 = set(rec1.source_urls or [])
        urls2 = set(rec2.source_urls or [])
        if urls1 and urls2 and urls1.intersection(urls2):
            return True

        # 3. Seniority Guardrail
        # Differing explicit seniorities must never merge (e.g. senior != staff_plus)
        if rec1.seniority and rec2.seniority and rec1.seniority != rec2.seniority:
            return False

        # 4. Parse Titles
        parsed1 = self.title_parser.parse(rec1.title)
        parsed2 = self.title_parser.parse(rec2.title)
        t1 = parsed1["title"].lower()
        t2 = parsed2["title"].lower()

        # If parsed seniorities disagree
        s1 = parsed1.get("seniority") or rec1.seniority
        s2 = parsed2.get("seniority") or rec2.seniority
        if s1 and s2 and s1 != s2:
            return False

        # 5. Distinct Role Differentiation
        # Prevent merging fundamentally different roles (e.g. ML Eng vs Data Eng, or Sales vs Eng)
        eng1 = "engineer" in t1 or "developer" in t1
        eng2 = "engineer" in t2 or "developer" in t2
        if eng1 != eng2:
            return False

        ml1 = "machine learning" in t1 or "ml" in t1 or "ai" in t1
        ml2 = "machine learning" in t2 or "ml" in t2 or "ai" in t2
        if ml1 != ml2:
            # e.g. "Data Engineer" vs "Machine Learning Engineer"
            if ("data engineer" in t1 and ml2) or ("data engineer" in t2 and ml1):
                return False

        # 6. Title Similarity Scoring
        if t1 == t2:
            title_score = 100.0
        else:
            title_score = float(fuzz.token_set_ratio(t1, t2))

        if title_score < self.title_threshold:
            return False

        # 7. Location Compatibility
        # If both explicitly specify different non-empty cities and neither is remote, do not merge
        if rec1.city and rec2.city:
            city1 = rec1.city.strip().lower()
            city2 = rec2.city.strip().lower()
            if city1 != city2:
                # If neither is remote, separate location
                is_remote1 = rec1.remote_policy == RemotePolicyEnum.remote
                is_remote2 = rec2.remote_policy == RemotePolicyEnum.remote
                if not (is_remote1 or is_remote2):
                    return False

        return True


# ==============================================================================
# Deduplication Engine
# ==============================================================================


class DeduplicationEngine:
    """Orchestrates candidate blocking, pairwise matching, and golden record merging.

    Resolves cross-source duplicates using Disjoint Set Union (DSU) to form connected
    components and persists unified CanonicalJobPosting records to SQLite.
    """

    def __init__(
        self,
        company_normalizer: Optional[Any] = None,
        title_parser: Optional[Any] = None,
        candidate_blocker: Optional[Any] = None,
        job_posting_matcher: Optional[Any] = None,
        record_merger: Optional[RecordMerger] = None,
        db_path: Optional[str] = None,
    ) -> None:
        """Initialize DeduplicationEngine with dependencies or robust defaults.

        Args:
            company_normalizer: Optional normalizer for company names.
            title_parser: Optional title parsing and seniority extractor.
            candidate_blocker: Optional candidate blocking strategy.
            job_posting_matcher: Optional pairwise job record similarity matcher.
            record_merger: Optional record merger for conflict resolution.
            db_path: Optional SQLite database file path.
        """
        # Resolve Company Normalizer
        if company_normalizer is not None:
            self.company_normalizer = company_normalizer
        else:
            try:
                from pipeline.dedup.normalizer import CompanyNormalizer
                self.company_normalizer = CompanyNormalizer()
            except Exception:
                self.company_normalizer = DefaultCompanyNormalizer()

        # Resolve Title Parser
        if title_parser is not None:
            self.title_parser = title_parser
        else:
            try:
                from pipeline.dedup.title_parser import TitleParser
                self.title_parser = TitleParser()
            except Exception:
                self.title_parser = DefaultTitleParser()

        # Resolve Candidate Blocker
        if candidate_blocker is not None:
            self.candidate_blocker = candidate_blocker
        else:
            try:
                from pipeline.dedup.blocker import CandidateBlocker
                self.candidate_blocker = CandidateBlocker(normalizer=self.company_normalizer)
            except Exception:
                self.candidate_blocker = DefaultCandidateBlocker(normalizer=self.company_normalizer)

        # Resolve Matcher
        if job_posting_matcher is not None:
            self.job_posting_matcher = job_posting_matcher
        else:
            try:
                from pipeline.dedup.matcher import JobPostingMatcher
                self.job_posting_matcher = JobPostingMatcher()
            except Exception:
                self.job_posting_matcher = DefaultJobPostingMatcher(
                    normalizer=self.company_normalizer,
                    title_parser=self.title_parser,
                )

        # Resolve Record Merger
        self.record_merger = record_merger or RecordMerger()
        self.db_path = str(db_path or DB_PATH)

    def resolve(self, postings: List[JobPostingRecord]) -> List[CanonicalJobPosting]:
        """Resolves duplicates across candidate postings and produces canonical records.

        Uses Disjoint Set Union (DSU) to identify connected components:
        if A matches B, and B matches C, then {A, B, C} are unified into one canonical entity.

        Args:
            postings: List of JobPostingRecord instances.

        Returns:
            List of merged CanonicalJobPosting entities, sorted deterministically.
        """
        if not postings:
            logger.info("No postings provided to DeduplicationEngine.resolve. Returning empty list.")
            return []

        logger.info("Starting deduplication for %d input postings...", len(postings))

        # 1. Sort input postings deterministically by posting_id
        ordered_postings = sorted(postings, key=lambda p: str(p.posting_id))
        postings_by_id: Dict[str, JobPostingRecord] = {
            p.posting_id: p for p in ordered_postings
        }

        # 2. Initialize DSU
        dsu = DisjointSetUnion()
        for p in ordered_postings:
            dsu.find(p.posting_id)

        # 3. Candidate Blocking
        candidate_pairs: Optional[Set[Tuple[str, str]]] = None
        blocks: Optional[Dict[str, List[JobPostingRecord]]] = None

        if hasattr(self.candidate_blocker, "get_candidate_pairs"):
            candidate_pairs = self.candidate_blocker.get_candidate_pairs(ordered_postings)
            logger.info("Generated %d candidate pairs from blocker.", len(candidate_pairs))
        elif hasattr(self.candidate_blocker, "block"):
            blocks = self.candidate_blocker.block(ordered_postings)
        elif hasattr(self.candidate_blocker, "build_blocks"):
            blocks = self.candidate_blocker.build_blocks(ordered_postings)
        elif hasattr(self.candidate_blocker, "index_postings"):
            blocks = self.candidate_blocker.index_postings(ordered_postings)
        else:
            # Fallback grouping by normalized company
            blocks = DefaultCandidateBlocker(self.company_normalizer).block(ordered_postings)

        # 4. Pairwise similarity matching
        pairwise_comparisons = 0
        matches_found = 0

        def _evaluate_match(rec_a: JobPostingRecord, rec_b: JobPostingRecord) -> bool:
            if hasattr(self.job_posting_matcher, "compare"):
                res = self.job_posting_matcher.compare(rec_a, rec_b)
                return getattr(res, "is_match", bool(res))
            if hasattr(self.job_posting_matcher, "is_match"):
                return bool(self.job_posting_matcher.is_match(rec_a, rec_b))
            if hasattr(self.job_posting_matcher, "match"):
                return bool(self.job_posting_matcher.match(rec_a, rec_b))
            if hasattr(self.job_posting_matcher, "score"):
                return self.job_posting_matcher.score(rec_a, rec_b) >= 85.0
            return False

        if candidate_pairs is not None:
            for id_a, id_b in candidate_pairs:
                if id_a not in postings_by_id or id_b not in postings_by_id:
                    continue
                pairwise_comparisons += 1
                rec_a = postings_by_id[id_a]
                rec_b = postings_by_id[id_b]

                if _evaluate_match(rec_a, rec_b):
                    matches_found += 1
                    dsu.union(id_a, id_b)
        elif blocks is not None:
            logger.info("Partitioned %d postings into %d candidate blocks.", len(ordered_postings), len(blocks))
            for block_key, block_postings in blocks.items():
                n = len(block_postings)
                if n <= 1:
                    continue

                for i in range(n):
                    for j in range(i + 1, n):
                        pairwise_comparisons += 1
                        rec_a = block_postings[i]
                        rec_b = block_postings[j]

                        if _evaluate_match(rec_a, rec_b):
                            matches_found += 1
                            dsu.union(rec_a.posting_id, rec_b.posting_id)

        logger.info(
            "Completed %d pairwise comparisons. Identified %d matching pairs.",
            pairwise_comparisons,
            matches_found,
        )

        # 5. Form clusters from DSU connected components
        clusters_map: Dict[str, List[JobPostingRecord]] = {}
        for p in ordered_postings:
            root = dsu.find(p.posting_id)
            clusters_map.setdefault(root, []).append(p)

        logger.info("Formed %d connected component clusters.", len(clusters_map))

        # 6. Merge each cluster into a CanonicalJobPosting
        canonical_postings: List[CanonicalJobPosting] = []
        for root_id, cluster in clusters_map.items():
            canonical_rec = self.record_merger.merge(cluster)
            canonical_postings.append(canonical_rec)

        # 7. Sort deterministically: company, title, canonical_id
        canonical_postings.sort(
            key=lambda c: (c.company.lower(), c.title.lower(), c.canonical_id)
        )

        logger.info(
            "Deduplication complete: %d raw postings -> %d canonical records (merged %d duplicates).",
            len(postings),
            len(canonical_postings),
            len(postings) - len(canonical_postings),
        )

        return canonical_postings

    def save_to_db(
        self,
        canonical_postings: List[CanonicalJobPosting],
        db_path: Optional[str] = None,
        table_name: str = "canonical_job_postings",
    ) -> int:
        """Persists canonical job postings into SQLite table canonical_job_postings.

        Args:
            canonical_postings: List of CanonicalJobPosting instances.
            db_path: Target SQLite database file path.
            table_name: Table name (default 'canonical_job_postings').

        Returns:
            Total rows successfully inserted or updated.
        """
        if not canonical_postings:
            logger.info("No canonical postings to persist.")
            return 0

        target_db = db_path or self.db_path
        is_memory = target_db == ":memory:"

        if not is_memory:
            Path(target_db).parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(target_db, timeout=30.0)
        try:
            with conn:
                # 1. Create table schema
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table_name} (
                        canonical_id TEXT PRIMARY KEY,
                        company TEXT NOT NULL,
                        title TEXT NOT NULL,
                        seniority TEXT,
                        city TEXT,
                        country TEXT,
                        remote_policy TEXT,
                        salary_min REAL,
                        salary_max REAL,
                        currency TEXT,
                        employment_type TEXT,
                        skills TEXT NOT NULL,
                        visa_sponsorship INTEGER,
                        sources TEXT NOT NULL,
                        source_urls TEXT NOT NULL,
                        source_posting_ids TEXT NOT NULL,
                        evidence TEXT NOT NULL,
                        first_seen TIMESTAMP NOT NULL,
                        last_seen TIMESTAMP NOT NULL,
                        duplicate_count INTEGER NOT NULL,
                        status TEXT DEFAULT 'open',
                        posted_date TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_company ON {table_name} (company)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_title ON {table_name} (title)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table_name}_status ON {table_name} (status)"
                )

                # 2. Prepare rows
                rows: List[Tuple[Any, ...]] = []
                for rec in canonical_postings:
                    data = rec.model_dump()

                    # Convert enums to string
                    seniority_val = data.get("seniority")
                    if hasattr(seniority_val, "value"):
                        seniority_val = seniority_val.value

                    remote_val = data.get("remote_policy")
                    if hasattr(remote_val, "value"):
                        remote_val = remote_val.value

                    emp_val = data.get("employment_type")
                    if hasattr(emp_val, "value"):
                        emp_val = emp_val.value

                    status_val = data.get("status")
                    if hasattr(status_val, "value"):
                        status_val = status_val.value
                    elif status_val is None:
                        status_val = "open"

                    # Convert visa_sponsorship to integer
                    visa_val = data.get("visa_sponsorship")
                    if visa_val is True:
                        visa_int = 1
                    elif visa_val is False:
                        visa_int = 0
                    else:
                        visa_int = None

                    # Format dates/timestamps
                    posted_date_val = data.get("posted_date")
                    if hasattr(posted_date_val, "isoformat"):
                        posted_date_str = posted_date_val.isoformat()
                    elif posted_date_val:
                        posted_date_str = str(posted_date_val)
                    else:
                        posted_date_str = None

                    first_seen_val = data.get("first_seen")
                    first_seen_str = (
                        first_seen_val.isoformat()
                        if hasattr(first_seen_val, "isoformat")
                        else str(first_seen_val)
                    )

                    last_seen_val = data.get("last_seen")
                    last_seen_str = (
                        last_seen_val.isoformat()
                        if hasattr(last_seen_val, "isoformat")
                        else str(last_seen_val)
                    )

                    # JSON encode collections
                    skills_json = json.dumps(data.get("skills") or [], ensure_ascii=False)
                    sources_json = json.dumps(data.get("sources") or [], ensure_ascii=False)
                    urls_json = json.dumps(data.get("source_urls") or [], ensure_ascii=False)
                    pids_json = json.dumps(data.get("source_posting_ids") or [], ensure_ascii=False)
                    evidence_json = json.dumps(data.get("evidence") or {}, ensure_ascii=False)

                    rows.append((
                        data.get("canonical_id"),
                        data.get("company"),
                        data.get("title"),
                        seniority_val,
                        data.get("city"),
                        data.get("country"),
                        remote_val,
                        data.get("salary_min"),
                        data.get("salary_max"),
                        data.get("currency"),
                        emp_val,
                        skills_json,
                        visa_int,
                        sources_json,
                        urls_json,
                        pids_json,
                        evidence_json,
                        first_seen_str,
                        last_seen_str,
                        int(data.get("duplicate_count", 1)),
                        status_val,
                        posted_date_str,
                    ))

                # 3. Batch insert or replace
                conn.executemany(
                    f"""
                    INSERT OR REPLACE INTO {table_name} (
                        canonical_id, company, title, seniority, city, country,
                        remote_policy, salary_min, salary_max, currency,
                        employment_type, skills, visa_sponsorship, sources,
                        source_urls, source_posting_ids, evidence, first_seen,
                        last_seen, duplicate_count, status, posted_date, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP
                    )
                    """,
                    rows,
                )

            logger.info("Successfully persisted %d canonical postings into '%s'", len(rows), table_name)
            return len(rows)
        finally:
            conn.close()

    def load_from_db(
        self,
        db_path: Optional[str] = None,
        table_name: str = "canonical_job_postings",
        limit: Optional[int] = None,
    ) -> List[CanonicalJobPosting]:
        """Loads canonical postings from the SQLite database.

        Args:
            db_path: Target SQLite database file path.
            table_name: Table name.
            limit: Optional limit on records retrieved.

        Returns:
            List of strongly-typed CanonicalJobPosting instances.
        """
        target_db = db_path or self.db_path
        if not Path(target_db).exists() and target_db != ":memory:":
            logger.warning("Database at '%s' does not exist.", target_db)
            return []

        conn = sqlite3.connect(target_db, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            query = f"SELECT * FROM {table_name}"
            if limit:
                query += f" LIMIT {int(limit)}"

            cursor = conn.execute(query)
            rows = cursor.fetchall()

            postings: List[CanonicalJobPosting] = []
            for row in rows:
                r_dict = dict(row)

                # Parse JSON fields
                skills = json.loads(r_dict.get("skills") or "[]")
                sources = json.loads(r_dict.get("sources") or "[]")
                source_urls = json.loads(r_dict.get("source_urls") or "[]")
                source_posting_ids = json.loads(r_dict.get("source_posting_ids") or "[]")
                evidence = json.loads(r_dict.get("evidence") or "{}")

                # Parse boolean visa
                visa_raw = r_dict.get("visa_sponsorship")
                visa_val = bool(visa_raw) if visa_raw is not None else None

                posting = CanonicalJobPosting(
                    canonical_id=r_dict["canonical_id"],
                    company=r_dict["company"],
                    title=r_dict["title"],
                    seniority=r_dict.get("seniority"),
                    city=r_dict.get("city"),
                    country=r_dict.get("country"),
                    remote_policy=r_dict.get("remote_policy"),
                    salary_min=r_dict.get("salary_min"),
                    salary_max=r_dict.get("salary_max"),
                    currency=r_dict.get("currency"),
                    employment_type=r_dict.get("employment_type"),
                    skills=skills,
                    visa_sponsorship=visa_val,
                    sources=sources,
                    source_urls=source_urls,
                    source_posting_ids=source_posting_ids,
                    evidence=evidence,
                    first_seen=r_dict["first_seen"],
                    last_seen=r_dict["last_seen"],
                    duplicate_count=int(r_dict.get("duplicate_count", 1)),
                    status=r_dict.get("status") or StatusEnum.open,
                    posted_date=r_dict.get("posted_date"),
                )
                postings.append(posting)

            logger.info("Loaded %d canonical postings from '%s'.", len(postings), table_name)
            return postings
        finally:
            conn.close()

    def resolve_and_save(
        self,
        postings: List[JobPostingRecord],
        db_path: Optional[str] = None,
        table_name: str = "canonical_job_postings",
    ) -> List[CanonicalJobPosting]:
        """Resolves duplicates and persists canonical entities in a single call."""
        canonical_postings = self.resolve(postings)
        if canonical_postings:
            self.save_to_db(canonical_postings, db_path=db_path, table_name=table_name)
        return canonical_postings

    @staticmethod
    def load_extracted_postings_from_db(
        db_path: Optional[str] = None,
        table_name: str = "extracted_postings",
        limit: Optional[int] = None,
    ) -> List[JobPostingRecord]:
        """Loads extracted JobPostingRecord items from SQLite for deduplication.

        Args:
            db_path: SQLite database path.
            table_name: Table name (default 'extracted_postings').
            limit: Optional maximum records to retrieve.

        Returns:
            List of JobPostingRecord instances.
        """
        target_db = db_path or str(DB_PATH)
        if not Path(target_db).exists() and target_db != ":memory:":
            logger.warning("Database at '%s' does not exist.", target_db)
            return []

        conn = sqlite3.connect(target_db, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            # Check if table exists
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            if not table_check:
                logger.warning("Table '%s' does not exist in '%s'.", table_name, target_db)
                return []

            query = f"SELECT * FROM {table_name}"
            if limit:
                query += f" LIMIT {int(limit)}"

            rows = conn.execute(query).fetchall()
            postings: List[JobPostingRecord] = []

            for row in rows:
                r = dict(row)
                skills = json.loads(r.get("skills") or "[]")
                source_urls = json.loads(r.get("source_urls") or "[]")
                evidence = json.loads(r.get("evidence") or "{}")

                visa_raw = r.get("visa_sponsorship")
                visa_val = bool(visa_raw) if visa_raw is not None else None

                rec = JobPostingRecord(
                    posting_id=r["posting_id"],
                    company=r["company"],
                    title=r["title"],
                    seniority=r.get("seniority"),
                    city=r.get("city"),
                    country=r.get("country"),
                    remote_policy=r.get("remote_policy"),
                    salary_min=r.get("salary_min"),
                    salary_max=r.get("salary_max"),
                    currency=r.get("currency"),
                    employment_type=r.get("employment_type"),
                    skills=skills,
                    visa_sponsorship=visa_val,
                    posted_date=r.get("posted_date"),
                    status=r.get("status") or StatusEnum.open,
                    source_urls=source_urls,
                    evidence=evidence,
                    first_seen=r["first_seen"],
                    last_seen=r["last_seen"],
                )
                postings.append(rec)

            logger.info("Loaded %d extracted postings from '%s'.", len(postings), table_name)
            return postings
        finally:
            conn.close()
