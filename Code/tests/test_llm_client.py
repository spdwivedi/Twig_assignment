"""Comprehensive unit tests for prompts, LLM client engine, and cost accounting.

Covers:
- Prompt construction and presence of strict anti-hallucination rules.
- Mock LLM response generation with schema conformance and null guarantees.
- Markdown code fence JSON extraction and error handling.
- Token and cost accounting calculations across multiple providers.
- Evidence snippet verbatim verification.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from pipeline.extractor.llm_client import (
    JSONParsingError,
    LLMClient,
    ProviderAPIError,
    verify_evidence,
)
from pipeline.extractor.prompts import (
    FEW_SHOT_EXAMPLES,
    SYSTEM_PROMPT,
    build_extraction_prompt,
)


# ==============================================================================
# 1. PROMPT CONSTRUCTION & ANTI-HALLUCINATION GUARDRAILS
# ==============================================================================

class TestPromptConstruction:
    """Tests for prompt engineering, schema directives, and guardrail constraints."""

    def test_system_prompt_contains_core_persona(self):
        """Verify the specialized Data & ML Engineering parser persona is present."""
        assert "You are a specialized Data & ML Engineering job posting parser." in SYSTEM_PROMPT

    def test_strict_null_rule_in_system_prompt(self):
        """Verify the Strict Null Rule and anti-hallucination instruction is present."""
        assert "THE STRICT NULL RULE" in SYSTEM_PROMPT
        assert (
            "A field not explicitly stated in the posting must be null. "
            "NEVER guess, assume, or hallucinate missing values (e.g. do not guess salary, "
            "visa sponsorship, or seniority if unstated)."
        ) in SYSTEM_PROMPT

    def test_evidence_rule_in_system_prompt(self):
        """Verify the Evidence Rule requiring verbatim text snippets is present."""
        assert "EVIDENCE RULE" in SYSTEM_PROMPT
        assert (
            "For every extracted non-null field, include the EXACT VERBATIM text snippet "
            "from the posting in the `evidence` object."
        ) in SYSTEM_PROMPT

    def test_compensation_normalization_rules_in_system_prompt(self):
        """Verify compensation annualization instructions are detailed in the prompt."""
        assert "COMPENSATION NORMALIZATION RULE" in SYSTEM_PROMPT
        assert "2,080 hours" in SYSTEM_PROMPT or "2080" in SYSTEM_PROMPT
        assert "Annualized" in SYSTEM_PROMPT or "annualized" in SYSTEM_PROMPT

    def test_few_shot_examples_completeness(self):
        """Verify the required few-shot examples exist: Greenhouse, Lever, and HN."""
        descriptions = [ex["description"].lower() for ex in FEW_SHOT_EXAMPLES]

        # Greenhouse example with explicit salary
        assert any("greenhouse" in d for d in descriptions)
        # Lever example with missing salary
        assert any("lever" in d and "missing salary" in d for d in descriptions)
        # Hacker news shorthand example
        assert any("hacker news" in d or "hn" in d for d in descriptions)

        # Confirm lever example has null salary
        lever_ex = next(ex for ex in FEW_SHOT_EXAMPLES if "lever" in ex["description"].lower())
        assert lever_ex["output"]["salary_min"] is None
        assert lever_ex["output"]["salary_max"] is None
        assert lever_ex["output"]["currency"] is None

        # Confirm HN example has annualized salary and staff_plus seniority
        hn_ex = next(ex for ex in FEW_SHOT_EXAMPLES if "hacker news" in ex["description"].lower())
        assert hn_ex["output"]["salary_min"] == 220000.0
        assert hn_ex["output"]["salary_max"] == 280000.0
        assert hn_ex["output"]["seniority"] == "staff_plus"
        assert hn_ex["output"]["visa_sponsorship"] is False

    def test_build_extraction_prompt_injects_metadata_and_text(self):
        """Verify prompt properly interpolates metadata context and sanitized job text."""
        raw_text = "Looking for a Senior Data Engineer with dbt and Snowflake experience."
        metadata = {
            "source": "greenhouse",
            "board_token": "stripe",
            "url": "https://boards.greenhouse.io/stripe/jobs/12345",
        }
        prompt = build_extraction_prompt(raw_text, metadata)

        # Must include metadata entries
        assert "source: greenhouse" in prompt
        assert "board_token: stripe" in prompt
        assert "https://boards.greenhouse.io/stripe/jobs/12345" in prompt

        # Must include target text
        assert "<JOB_POSTING_TEXT>" in prompt
        assert raw_text in prompt
        assert "</JOB_POSTING_TEXT>" in prompt

        # Must include strict null and evidence reminders
        assert "THE STRICT NULL RULE" in prompt
        assert "EVIDENCE RULE" in prompt


# ==============================================================================
# 2. JSON EXTRACTION FROM MODEL OUTPUT
# ==============================================================================

class TestJSONParsing:
    """Tests for extracting and parsing JSON across varied markdown and text formats."""

    def test_extract_pure_json(self):
        """Verify pure JSON string without markdown is parsed properly."""
        payload = {"company": "Stripe", "title": "Data Engineer", "salary_min": 150000}
        text = json.dumps(payload)
        parsed = LLMClient.extract_json_from_response(text)
        assert parsed == payload

    def test_extract_markdown_json_fences(self):
        """Verify ```json ... ``` code fences are stripped and parsed."""
        payload = {"company": "Databricks", "title": "Platform Engineer", "skills": ["Spark"]}
        text = f"```json\n{json.dumps(payload, indent=2)}\n```"
        parsed = LLMClient.extract_json_from_response(text)
        assert parsed == payload

    def test_extract_markdown_generic_fences(self):
        """Verify generic ``` ... ``` code fences are parsed."""
        payload = {"company": "Figma", "title": "ML Engineer"}
        text = f"```\n{json.dumps(payload)}\n```"
        parsed = LLMClient.extract_json_from_response(text)
        assert parsed == payload

    def test_extract_json_with_conversational_chatter(self):
        """Verify extraction succeeds when LLM includes introductory and trailing chat."""
        payload = {"company": "Airbnb", "title": "Analytics Engineer", "salary_min": None}
        text = (
            "Here is the structured record you requested:\n\n"
            f"```json\n{json.dumps(payload)}\n```\n\n"
            "I verified that all missing fields are set to null."
        )
        parsed = LLMClient.extract_json_from_response(text)
        assert parsed == payload

    def test_extract_json_raw_braces_in_text(self):
        """Verify extraction succeeds when JSON is embedded without markdown code fences."""
        payload = {"company": "Plaid", "salary_max": 200000}
        text = f"Extracted output:\n{json.dumps(payload)}\nEnd of extraction."
        parsed = LLMClient.extract_json_from_response(text)
        assert parsed == payload

    def test_empty_response_raises_error(self):
        """Verify empty or whitespace-only response raises JSONParsingError."""
        with pytest.raises(JSONParsingError):
            LLMClient.extract_json_from_response("")

        with pytest.raises(JSONParsingError):
            LLMClient.extract_json_from_response("   \n\t  ")

    def test_malformed_json_raises_error(self):
        """Verify unparseable syntax raises JSONParsingError."""
        text = "```json\n{company: 'Stripe', unclosed_bracket: [\n```"
        with pytest.raises(JSONParsingError):
            LLMClient.extract_json_from_response(text)


# ==============================================================================
# 3. MOCK EXTRACTOR & SCHEMA CONFORMANCE
# ==============================================================================

class TestMockExtractor:
    """Tests for the zero-dependency intelligent offline extractor."""

    @pytest.fixture
    def client(self):
        return LLMClient(mock=True)

    def test_greenhouse_explicit_posting_extraction(self, client):
        """Test extraction on standard Greenhouse posting with explicit compensation and remote policy."""
        text = """Company: Databricks
Title: Senior Data Engineer, Platform
Location: San Francisco, CA (Hybrid - 3 days in office)
Employment Type: Full-time
About the role:
Databricks is seeking a Senior Data Engineer to design high-throughput Spark and Delta Lake pipelines using Python and SQL.
Compensation:
The estimated base salary range for this role is $180,000 - $225,000 USD per year plus equity.
Work Authorization:
We provide visa sponsorship for qualified international candidates."""

        record, metrics = client.extract_job_data(text)

        assert record["company"] == "Databricks"
        assert record["title"] == "Senior Data Engineer, Platform"
        assert record["seniority"] == "senior"
        assert record["city"] == "San Francisco"
        assert record["country"] == "US"
        assert record["remote_policy"] == "hybrid"
        assert record["salary_min"] == 180000.0
        assert record["salary_max"] == 225000.0
        assert record["currency"] == "USD"
        assert record["employment_type"] == "full_time"
        assert "Spark" in record["skills"]
        assert "Python" in record["skills"]
        assert record["visa_sponsorship"] is True

        # Verify evidence snippets are literal substrings of source text
        verifications = verify_evidence(text, record["evidence"])
        assert all(verifications.values()), f"Evidence check failed: {verifications}"

    def test_lever_posting_missing_salary_null_guarantee(self, client):
        """Test The Strict Null Rule: missing compensation must be null, never guessed."""
        text = """Company: Palantir
Role: Machine Learning Engineer, Forward Deployed
Location: New York, NY
Workplace Type: On-site
Job Description:
Join Palantir to deploy cutting-edge ML models into production. Requirements include 3+ years experience with PyTorch, Kubernetes, and C++. We offer competitive equity and standard health benefits.
Notice: Applicants must be legally authorized to work in the US without requiring sponsorship now or in the future."""

        record, metrics = client.extract_job_data(text)

        assert record["company"] == "Palantir"
        assert record["title"] == "Machine Learning Engineer, Forward Deployed"
        assert record["remote_policy"] == "onsite"
        assert record["city"] == "New York"
        assert record["country"] == "US"
        assert record["salary_min"] is None
        assert record["salary_max"] is None
        assert record["currency"] is None
        assert record["employment_type"] is None
        assert record["visa_sponsorship"] is False
        assert "PyTorch" in record["skills"]
        assert "Kubernetes" in record["skills"]

        verifications = verify_evidence(text, record["evidence"])
        assert all(verifications.values()), f"Evidence check failed: {verifications}"

    def test_hacker_news_shorthand_extraction(self, client):
        """Test extraction on unstructured shorthand Hacker News comment."""
        text = """Stripe | US Remote | Staff Data Eng | $220k-$280k | Full-time | Visa: No
Looking for a Staff Data Engineer to lead our real-time streaming payments pipeline. Heavy use of Kafka, Flink, Java, and Python. Contact: jobs@stripe.com"""

        record, metrics = client.extract_job_data(text)

        assert record["company"] == "Stripe"
        assert record["title"] == "Staff Data Eng"
        assert record["seniority"] == "staff_plus"
        assert record["remote_policy"] == "remote"
        assert record["country"] == "US"
        assert record["salary_min"] == 220000.0
        assert record["salary_max"] == 280000.0
        assert record["currency"] == "USD"
        assert record["employment_type"] == "full_time"
        assert record["visa_sponsorship"] is False
        assert "Kafka" in record["skills"]
        assert "Python" in record["skills"]

        verifications = verify_evidence(text, record["evidence"])
        assert all(verifications.values()), f"Evidence check failed: {verifications}"

    def test_hourly_compensation_annualization(self, client):
        """Test compensation normalization: $80/hr normalized to 80 * 2080 = $166,400."""
        text = """Company: Airbnb
Title: Contract Data Engineer
Location: Remote
Rate: $80 / hr
Working with SQL and Airflow."""

        record, metrics = client.extract_job_data(text)

        assert record["salary_min"] == 166400.0
        assert record["salary_max"] == 166400.0
        assert record["currency"] == "USD"
        assert record["remote_policy"] == "remote"

    def test_unstated_fields_strictly_null(self, client):
        """Test that unmentioned fields strictly evaluate to None."""
        text = "Looking for someone to help clean data tables with SQL."

        record, metrics = client.extract_job_data(text)

        assert record["salary_min"] is None
        assert record["salary_max"] is None
        assert record["currency"] is None
        assert record["visa_sponsorship"] is None
        assert record["remote_policy"] is None
        assert record["seniority"] is None
        assert record["employment_type"] is None

    def test_end_to_end_pydantic_schema_validation(self, client):
        """Test seamless conversion of extracted dict into target Pydantic JobPostingRecord."""
        from datetime import datetime, timezone
        from pipeline.models import JobPostingRecord, RawExtractionOutput, generate_deterministic_id

        text = """Stripe | US Remote | Staff Data Eng | $220k-$280k | Full-time | Visa: No
Looking for a Staff Data Engineer to lead our real-time streaming payments pipeline. Heavy use of Kafka, Flink, Java, and Python. Contact: jobs@stripe.com"""

        record, metrics = client.extract_job_data(text)
        raw_output = RawExtractionOutput(**record)

        now = datetime.now(timezone.utc)
        posting_id = generate_deterministic_id("Stripe", "Staff Data Eng", "US-remote")
        canonical_record = raw_output.to_job_posting_record(
            posting_id=posting_id,
            first_seen=now,
            last_seen=now,
            source_urls=["https://stripe.com/jobs/staff-data-eng"]
        )

        assert isinstance(canonical_record, JobPostingRecord)
        assert canonical_record.posting_id == posting_id
        assert canonical_record.company == "Stripe"
        assert canonical_record.salary_min == 220000.0
        assert canonical_record.salary_max == 280000.0
        assert canonical_record.evidence["salary"] == "$220k-$280k"
        assert len(canonical_record.skills) >= 2


# ==============================================================================
# 4. TOKEN & COST ACCOUNTING
# ==============================================================================

class TestCostAccounting:
    """Tests for token usage tracking and dollar cost calculations."""

    def test_gemini_flash_cost_calculation(self):
        """Verify Gemini 1.5 Flash pricing: $0.075/1M input, $0.30/1M output."""
        client = LLMClient(mock=True, model="gemini-1.5-flash")

        # 1,000 prompt tokens -> $0.000075
        # 1,000 completion tokens -> $0.000300
        # Total -> $0.000375
        cost = client.calculate_cost(prompt_tokens=1000, completion_tokens=1000)
        assert pytest.approx(cost, rel=1e-5) == 0.000375

    def test_openai_mini_cost_calculation(self):
        """Verify GPT-4o-mini pricing: $0.15/1M input, $0.60/1M output."""
        client = LLMClient(mock=True, model="gpt-4o-mini")

        # 2,000 prompt tokens -> $0.00030
        # 1,000 completion tokens -> $0.00060
        # Total -> $0.00090
        cost = client.calculate_cost(prompt_tokens=2000, completion_tokens=1000)
        assert pytest.approx(cost, rel=1e-5) == 0.00090

    def test_session_statistics_accumulation_and_reset(self):
        """Verify cumulative session stats accurately sum calls and can be reset."""
        client = LLMClient(mock=True)

        text = "Stripe | US Remote | Staff Data Eng | $220k-$280k | Visa: No"

        # Make 3 consecutive extraction calls
        client.extract_job_data(text)
        client.extract_job_data(text)
        client.extract_job_data(text)

        stats = client.get_session_stats()
        assert stats["call_count"] == 3
        assert stats["total_prompt_tokens"] > 0
        assert stats["total_completion_tokens"] > 0
        assert stats["total_tokens"] == stats["total_prompt_tokens"] + stats["total_completion_tokens"]
        assert stats["total_cost_usd"] > 0.0
        assert stats["avg_cost_per_posting"] > 0.0

        # Reset session
        client.reset_session_stats()
        reset_stats = client.get_session_stats()
        assert reset_stats["call_count"] == 0
        assert reset_stats["total_tokens"] == 0
        assert reset_stats["total_cost_usd"] == 0.0
        assert reset_stats["avg_cost_per_posting"] == 0.0


# ==============================================================================
# 5. MULTI-PROVIDER CONFIGURATION & ERROR HANDLING
# ==============================================================================

class TestProviderConfiguration:
    """Tests for provider detection, explicit overrides, and error resilience."""

    def test_auto_detect_mock_when_no_keys(self, monkeypatch):
        """Verify mock provider is chosen when no environment keys exist."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        client = LLMClient()
        assert client.provider == "mock"

    def test_gemini_auto_detection_with_key(self, monkeypatch):
        """Verify Gemini provider is selected when GEMINI_API_KEY is in environment."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        client = LLMClient()
        assert client.provider == "gemini"
        assert client.api_key == "test-gemini-key"
        assert client.model == "gemini-1.5-flash"

    def test_openai_auto_detection_with_key(self, monkeypatch):
        """Verify OpenAI provider is selected when OPENAI_API_KEY is in environment."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

        client = LLMClient()
        assert client.provider == "openai"
        assert client.api_key == "test-openai-key"
        assert client.model == "gpt-4o-mini"

    def test_forced_mock_overrides_environment_keys(self, monkeypatch):
        """Verify mock=True takes precedence even when API keys are configured."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        client = LLMClient(mock=True)
        assert client.provider == "mock"
        assert client.api_key is None

    def test_unsupported_provider_raises_value_error(self):
        """Verify initializing with an unknown provider raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported provider"):
            LLMClient(provider="unsupported_llm_service")

    @patch("requests.post")
    def test_gemini_api_call_success(self, mock_post):
        """Verify Gemini REST request payload and response parsing."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": json.dumps({"company": "Stripe", "title": "Staff Engineer"})}
                        ]
                    }
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 250,
                "candidatesTokenCount": 50,
            }
        }
        mock_post.return_value = mock_resp

        client = LLMClient(provider="gemini", api_key="fake-key", model="gemini-1.5-flash")
        record, metrics = client.extract_job_data("Stripe job text")

        assert record["company"] == "Stripe"
        assert record["title"] == "Staff Engineer"
        assert metrics["prompt_tokens"] == 250
        assert metrics["completion_tokens"] == 50
        assert metrics["provider"] == "gemini"

    @patch("requests.post")
    def test_retry_on_transient_api_failure(self, mock_post):
        """Verify transient 500 error triggers retry and succeeds on next attempt."""
        fail_resp = MagicMock()
        fail_resp.status_code = 500
        fail_resp.text = "Internal Server Error"

        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.json.return_value = {
            "choices": [
                {"message": {"content": json.dumps({"company": "Plaid", "title": "Data Engineer"})}}
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40}
        }

        # First call fails with 500, second call succeeds
        mock_post.side_effect = [
            ProviderAPIError("HTTP 500", status_code=500),
            success_resp
        ]

        client = LLMClient(provider="openai", api_key="fake-key", max_retries=2, backoff_factor=0.01)
        record, metrics = client.extract_job_data("Plaid job text")

        assert record["company"] == "Plaid"
        assert metrics["call_count"] == 1 if "call_count" in metrics else metrics["prompt_tokens"] == 100
