"""Prompt templates and system instructions for LLM job extraction.

This module provides system instructions, schema specifications, and few-shot
examples enforcing strict anti-hallucination rules (The Strict Null Rule and
Evidence Requirement) for parsing Data & ML Engineering job postings.
"""

import json
from typing import Any, Dict, Optional

# Core system prompt establishing persona, schema constraints, and anti-hallucination rules
SYSTEM_PROMPT = """You are a specialized Data & ML Engineering job posting parser.
Your task is to extract structured job posting records from raw text and output strictly valid JSON conforming to the target schema.

Target JSON Schema:
{
  "company": string,            // Normalized company name (e.g. "Stripe", "Databricks")
  "title": string,              // Canonical job title (e.g. "Staff Data Engineer", "Machine Learning Engineer")
  "seniority": string | null,   // Must be one of: "junior", "mid", "senior", "staff_plus", or null
  "city": string | null,        // Primary city (e.g. "San Francisco", "New York") or null
  "country": string | null,     // Country name or code (e.g. "US") or null
  "remote_policy": string | null,// Must be one of: "onsite", "hybrid", "remote", or null
  "salary_min": number | null,  // Annualized minimum base salary (number), or null if not stated
  "salary_max": number | null,  // Annualized maximum base salary (number), or null if not stated
  "currency": string | null,    // ISO currency code (e.g. "USD", "EUR", "CAD") or null
  "employment_type": string | null, // Must be one of: "full_time", "contract", "intern", or null
  "skills": string[],           // List of explicit tools/technologies mentioned (e.g. ["Python", "Spark", "dbt"])
  "visa_sponsorship": boolean | null, // true if explicitly offered, false if explicitly unavailable, null if unstated
  "posted_date": string | null, // Date in YYYY-MM-DD format if explicitly stated, else null
  "evidence": {                 // Object mapping extracted field names to EXACT verbatim snippets from the raw posting
    "title": string,
    "seniority": string,
    "remote_policy": string,
    "salary": string,
    "skills": string,
    "visa_sponsorship": string,
    ...
  }
}

CORE EXTRACTION RULES (MANDATORY):
1. THE STRICT NULL RULE:
   A field not explicitly stated in the posting must be null. NEVER guess, assume, or hallucinate missing values (e.g. do not guess salary, visa sponsorship, or seniority if unstated).
   - If compensation is not explicitly mentioned with numbers, "salary_min", "salary_max", and "currency" MUST be null. Never infer salary from seniority, location, or company prestige.
   - If visa sponsorship is not explicitly stated, "visa_sponsorship" MUST be null.
   - If seniority is not indicated in the title or text, "seniority" MUST be null.
   - If location or remote policy is not explicitly stated, return null.

2. EVIDENCE RULE:
   For every extracted non-null field, include the EXACT VERBATIM text snippet from the posting in the `evidence` object.
   Every snippet must be a literal, verbatim quote from the text. If you cannot point to an exact quote, set the field to null.

3. COMPENSATION NORMALIZATION RULE:
   - All compensation numbers MUST be annualized.
   - Hourly rates: Multiply by 2,080 hours (e.g. $80/hr -> 166400).
   - Monthly rates: Multiply by 12 months (e.g. $10,000/mo -> 120000).
   - Shorthand: "$220k" -> 220000, "$220k-$280k" -> min: 220000, max: 280000.
   - Currency: Normalize "$" to "USD", "€" to "EUR", "£" to "GBP", "CAD$" to "CAD".

4. ENUM CONSTRAINTS:
   - "seniority":
     * "junior": Junior, Associate, Entry-level, L3, Engineer I
     * "mid": Mid-level, Engineer II, Intermediate (or standard title with 2-4 years experience)
     * "senior": Senior, Sr., Lead, L5/Senior Engineer
     * "staff_plus": Staff, Principal, Distinguished, Director, Head of
     * null: If ambiguous or unstated
   - "remote_policy":
     * "onsite": Must work in-office 5 days/week or "in office"
     * "hybrid": Combination of home and office (e.g. "2 days in office", "hybrid")
     * "remote": Fully remote / work from home / distributed
     * null: If unstated
   - "employment_type":
     * "full_time", "contract", "intern", or null
"""

# Few-shot examples illustrating varied formats and strict null handling
FEW_SHOT_EXAMPLES = [
    {
        "description": "Standard Greenhouse posting with explicit salary and remote policy",
        "input": """Company: Databricks
Title: Senior Data Engineer, Platform
Location: San Francisco, CA (Hybrid - 3 days in office)
About the role:
Databricks is seeking a Senior Data Engineer to design high-throughput Spark and Delta Lake pipelines using Python and SQL.
Compensation:
The estimated base salary range for this role is $180,000 - $225,000 USD per year plus equity.
Work Authorization:
We provide visa sponsorship for qualified international candidates.""",
        "output": {
            "company": "Databricks",
            "title": "Senior Data Engineer, Platform",
            "seniority": "senior",
            "city": "San Francisco",
            "country": "US",
            "remote_policy": "hybrid",
            "salary_min": 180000.0,
            "salary_max": 225000.0,
            "currency": "USD",
            "employment_type": "full_time",
            "skills": ["Spark", "Delta Lake", "Python", "SQL"],
            "visa_sponsorship": True,
            "posted_date": None,
            "evidence": {
                "company": "Company: Databricks",
                "title": "Senior Data Engineer, Platform",
                "seniority": "Senior Data Engineer",
                "city": "San Francisco, CA",
                "country": "San Francisco, CA",
                "remote_policy": "Hybrid - 3 days in office",
                "salary": "$180,000 - $225,000 USD per year",
                "skills": "Spark and Delta Lake pipelines using Python and SQL",
                "visa_sponsorship": "We provide visa sponsorship for qualified international candidates"
            }
        }
    },
    {
        "description": "Lever posting with missing salary (evaluates to null)",
        "input": """Company: Palantir
Role: Machine Learning Engineer, Forward Deployed
Location: New York, NY
Workplace Type: On-site
Job Description:
Join Palantir to deploy cutting-edge ML models into production. Requirements include 3+ years experience with PyTorch, Kubernetes, and C++. We offer competitive equity and standard health benefits.
Notice: Applicants must be legally authorized to work in the US without requiring sponsorship now or in the future.""",
        "output": {
            "company": "Palantir",
            "title": "Machine Learning Engineer, Forward Deployed",
            "seniority": "mid",
            "city": "New York",
            "country": "US",
            "remote_policy": "onsite",
            "salary_min": None,
            "salary_max": None,
            "currency": None,
            "employment_type": "full_time",
            "skills": ["PyTorch", "Kubernetes", "C++"],
            "visa_sponsorship": False,
            "posted_date": None,
            "evidence": {
                "company": "Company: Palantir",
                "title": "Machine Learning Engineer, Forward Deployed",
                "seniority": "Machine Learning Engineer",
                "city": "New York, NY",
                "country": "New York, NY",
                "remote_policy": "Workplace Type: On-site",
                "skills": "PyTorch, Kubernetes, and C++",
                "visa_sponsorship": "Applicants must be legally authorized to work in the US without requiring sponsorship now or in the future"
            }
        }
    },
    {
        "description": "Hacker News comment with unstructured shorthand",
        "input": """Stripe | US Remote | Staff Data Eng | $220k-$280k | Visa: No
Looking for a Staff Data Engineer to lead our real-time streaming payments pipeline. Heavy use of Kafka, Flink, Java, and Python. Contact: jobs@stripe.com""",
        "output": {
            "company": "Stripe",
            "title": "Staff Data Eng",
            "seniority": "staff_plus",
            "city": None,
            "country": "US",
            "remote_policy": "remote",
            "salary_min": 220000.0,
            "salary_max": 280000.0,
            "currency": "USD",
            "employment_type": "full_time",
            "skills": ["Kafka", "Flink", "Java", "Python"],
            "visa_sponsorship": False,
            "posted_date": None,
            "evidence": {
                "company": "Stripe",
                "title": "Staff Data Eng",
                "seniority": "Staff Data Eng",
                "country": "US Remote",
                "remote_policy": "US Remote",
                "salary": "$220k-$280k",
                "skills": "Kafka, Flink, Java, and Python",
                "visa_sponsorship": "Visa: No"
            }
        }
    }
]


def format_few_shot_examples() -> str:
    """Format few-shot examples into readable prompt text."""
    blocks = []
    for i, ex in enumerate(FEW_SHOT_EXAMPLES, 1):
        block = (
            f"--- EXAMPLE {i}: {ex['description']} ---\n"
            f"[INPUT TEXT]\n{ex['input']}\n\n"
            f"[OUTPUT JSON]\n{json.dumps(ex['output'], indent=2)}"
        )
        blocks.append(block)
    return "\n\n".join(blocks)


def build_extraction_prompt(
    clean_job_text: str,
    source_metadata: Optional[Dict[str, Any]] = None
) -> str:
    """Build the complete extraction prompt for an LLM call.

    Args:
        clean_job_text: Sanitized, boilerplate-free raw text of the job posting.
        source_metadata: Optional dictionary with source information (e.g. source, url, board_token).

    Returns:
        Structured string containing system instructions, schema, few-shot examples,
        metadata context, and the target text to parse.
    """
    metadata_lines = []
    if source_metadata:
        for k, v in sorted(source_metadata.items()):
            if v is not None:
                metadata_lines.append(f"  - {k}: {v}")

    metadata_block = ""
    if metadata_lines:
        metadata_block = (
            "Source Context & Metadata:\n"
            + "\n".join(metadata_lines)
            + "\n\n"
        )

    few_shots_text = format_few_shot_examples()

    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"FEW-SHOT EXAMPLES:\n"
        f"{few_shots_text}\n\n"
        f"--- TARGET JOB POSTING TO EXTRACT ---\n"
        f"{metadata_block}"
        f"<JOB_POSTING_TEXT>\n"
        f"{clean_job_text.strip()}\n"
        f"</JOB_POSTING_TEXT>\n\n"
        f"INSTRUCTION: Extract the structured record conforming to the schema and rules above.\n"
        f"Remember THE STRICT NULL RULE: If a field is not stated in the text, it must be null.\n"
        f"Remember the EVIDENCE RULE: Include verbatim snippets in the evidence object.\n"
        f"Output ONLY the JSON object (wrapped in ```json ... ``` or as raw JSON)."
    )
    return prompt
