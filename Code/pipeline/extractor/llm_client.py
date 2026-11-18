"""LLM Client Engine for structured job extraction with multi-provider support.

Provides robust structured JSON extraction, multi-provider integration (Gemini, OpenAI,
and zero-dependency offline Mock mode), token and cost accounting, and strict evidence
verification.
"""

import json
import logging
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

import requests

from pipeline.extractor.prompts import (
    SYSTEM_PROMPT,
    build_extraction_prompt,
)

logger = logging.getLogger(__name__)

# Standard model pricing per 1,000,000 tokens (USD)
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "gemini-1.5-flash": {"prompt": 0.075, "completion": 0.30},
    "gemini-1.5-pro": {"prompt": 1.25, "completion": 5.00},
    "gemini-2.0-flash": {"prompt": 0.10, "completion": 0.40},
    "gemini-2.0-flash-exp": {"prompt": 0.10, "completion": 0.40},
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.60},
    "gpt-4o": {"prompt": 2.50, "completion": 10.00},
    "mock-extractor": {"prompt": 0.075, "completion": 0.30},
}


class LLMClientError(Exception):
    """Base exception for LLM client failures."""
    pass


class ProviderAPIError(LLMClientError):
    """Exception raised when an LLM provider API returns an HTTP error or times out."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_text: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class JSONParsingError(LLMClientError):
    """Exception raised when raw LLM response cannot be parsed into target JSON."""
    def __init__(self, message: str, raw_response: Optional[str] = None):
        super().__init__(message)
        self.raw_response = raw_response


class LLMClient:
    """Multi-provider client for LLM-powered job extraction and token/cost accounting."""

    def __init__(
        self,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        mock: bool = False,
        temperature: float = 0.0,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_factor: float = 1.5,
    ):
        """Initialize the LLM Client.

        Args:
            provider: 'gemini', 'openai', or 'mock'. If None, auto-detected from env vars.
            api_key: Optional explicit API key. If omitted, read from GEMINI_API_KEY / OPENAI_API_KEY.
            model: Model name. Defaults to provider standard (e.g. 'gemini-1.5-flash').
            mock: If True, forces intelligent offline mock extraction with 0 API dependencies.
            temperature: Sampling temperature (default 0.0 for deterministic extraction).
            timeout: Request timeout in seconds.
            max_retries: Maximum retry attempts for transient API errors.
            backoff_factor: Multiplier for exponential backoff between retries.
        """
        self.mock = mock
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

        # Provider and key resolution
        if mock:
            self.provider = "mock"
            self.api_key = None
            self.model = model or "mock-extractor"
        elif provider:
            self.provider = provider.lower()
            if self.provider == "gemini":
                self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
                self.model = model or "gemini-1.5-flash"
            elif self.provider == "openai":
                self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
                self.model = model or "gpt-4o-mini"
            elif self.provider == "mock":
                self.api_key = None
                self.model = model or "mock-extractor"
            else:
                raise ValueError(f"Unsupported provider: '{provider}'. Supported: gemini, openai, mock")
        else:
            # Auto-detect from environment
            gemini_key = api_key or os.environ.get("GEMINI_API_KEY")
            openai_key = api_key or os.environ.get("OPENAI_API_KEY")

            if gemini_key:
                self.provider = "gemini"
                self.api_key = gemini_key
                self.model = model or "gemini-1.5-flash"
            elif openai_key:
                self.provider = "openai"
                self.api_key = openai_key
                self.model = model or "gpt-4o-mini"
            else:
                logger.info("No LLM API keys found in environment. Initializing in offline Mock mode.")
                self.provider = "mock"
                self.api_key = None
                self.model = model or "mock-extractor"

        # Session-level token and cost tracking
        self.session_prompt_tokens: int = 0
        self.session_completion_tokens: int = 0
        self.session_total_tokens: int = 0
        self.session_cost_usd: float = 0.0
        self.session_call_count: int = 0

    def calculate_cost(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        model: Optional[str] = None
    ) -> float:
        """Calculate dollar cost of an extraction call based on token usage.

        Args:
            prompt_tokens: Number of prompt/input tokens.
            completion_tokens: Number of completion/output tokens.
            model: Model name for rate lookup. Defaults to self.model.

        Returns:
            Calculated cost in USD (rounded to 8 decimal places).
        """
        target_model = model or self.model
        pricing = MODEL_PRICING.get(target_model)

        if not pricing:
            # Fallback search by model prefix
            for k, rates in MODEL_PRICING.items():
                if target_model.startswith(k) or k.startswith(target_model):
                    pricing = rates
                    break

        if not pricing:
            pricing = MODEL_PRICING["gemini-1.5-flash"]

        prompt_cost = (prompt_tokens * pricing["prompt"]) / 1_000_000.0
        completion_cost = (completion_tokens * pricing["completion"]) / 1_000_000.0
        total_cost = prompt_cost + completion_cost
        return round(total_cost, 8)

    def get_session_stats(self) -> Dict[str, Any]:
        """Return cumulative token and dollar cost metrics for the current session."""
        avg_cost = (
            self.session_cost_usd / self.session_call_count
            if self.session_call_count > 0
            else 0.0
        )
        return {
            "provider": self.provider,
            "model": self.model,
            "total_prompt_tokens": self.session_prompt_tokens,
            "total_completion_tokens": self.session_completion_tokens,
            "total_tokens": self.session_total_tokens,
            "total_cost_usd": round(self.session_cost_usd, 6),
            "call_count": self.session_call_count,
            "avg_cost_per_posting": round(avg_cost, 6),
        }

    def reset_session_stats(self) -> None:
        """Reset cumulative session statistics to zero."""
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_cost_usd = 0.0
        self.session_call_count = 0

    @staticmethod
    def extract_json_from_response(response_text: str) -> Dict[str, Any]:
        """Extract and parse structured JSON from raw model output.

        Handles plain JSON, markdown ```json fences, mixed preamble text, and whitespace.

        Args:
            response_text: Raw string returned by the LLM.

        Returns:
            Parsed dictionary adhering to the job schema.

        Raises:
            JSONParsingError: If no valid JSON dictionary can be parsed.
        """
        if not response_text or not response_text.strip():
            raise JSONParsingError("Empty response received from LLM", raw_response=response_text)

        cleaned = response_text.strip()

        # Check for ```json ... ``` or ``` ... ``` blocks
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
        if fence_match:
            candidate = fence_match.group(1).strip()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        # Try parsing whole text as JSON
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # Search for first '{' and last '}'
        start_idx = cleaned.find("{")
        end_idx = cleaned.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            candidate = cleaned[start_idx : end_idx + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError as err:
                raise JSONParsingError(
                    f"Failed to decode JSON substring: {err}",
                    raw_response=response_text
                ) from err

        raise JSONParsingError(
            "Could not locate valid JSON object in response text",
            raw_response=response_text
        )

    def extract(
        self,
        clean_job_text_or_prompt: str,
        source_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Extract structured job posting JSON from text or prompt.

        Args:
            clean_job_text_or_prompt: Clean job description text or prompt.
            source_metadata: Optional dictionary with source metadata.

        Returns:
            Structured job dictionary conforming to the assignment schema.
        """
        record, _ = self.extract_job_data(clean_job_text_or_prompt, source_metadata)
        return record

    def extract_job_data(
        self,
        clean_job_text: str,
        source_metadata: Optional[Dict[str, Any]] = None
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Perform end-to-end extraction on a single job posting.

        Args:
            clean_job_text: Cleaned text of the job description.
            source_metadata: Optional dict containing source name, URL, raw title, etc.

        Returns:
            Tuple of:
              - extracted_record: Dict conforming to the assignment schema.
              - call_metrics: Dict with token counts, cost in USD, and provider metadata.
        """
        prompt = build_extraction_prompt(clean_job_text, source_metadata)

        raw_response, prompt_tokens, completion_tokens = self._dispatch_with_retry(
            prompt=prompt,
            clean_job_text=clean_job_text,
            source_metadata=source_metadata
        )

        record = self.extract_json_from_response(raw_response)

        # Token and cost calculation
        cost_usd = self.calculate_cost(prompt_tokens, completion_tokens)

        # Update session counters
        self.session_prompt_tokens += prompt_tokens
        self.session_completion_tokens += completion_tokens
        self.session_total_tokens += (prompt_tokens + completion_tokens)
        self.session_cost_usd += cost_usd
        self.session_call_count += 1

        call_metrics = {
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost_usd": cost_usd,
        }

        return record, call_metrics

    def _dispatch_with_retry(
        self,
        prompt: str,
        clean_job_text: str,
        source_metadata: Optional[Dict[str, Any]] = None
    ) -> Tuple[str, int, int]:
        """Dispatch API request with exponential backoff on transient errors."""
        last_error = None
        current_delay = 1.0

        for attempt in range(1, self.max_retries + 1):
            try:
                if self.provider == "gemini":
                    return self._call_gemini(prompt)
                elif self.provider == "openai":
                    return self._call_openai(prompt)
                elif self.provider == "mock":
                    return self._call_mock(clean_job_text, source_metadata)
                else:
                    raise LLMClientError(f"Unsupported provider: {self.provider}")
            except (requests.RequestException, ProviderAPIError) as err:
                last_error = err
                if attempt == self.max_retries:
                    raise ProviderAPIError(
                        f"LLM API call failed after {self.max_retries} attempts: {err}"
                    ) from err
                logger.warning(
                    f"LLM API transient error on attempt {attempt}/{self.max_retries}. "
                    f"Retrying in {current_delay:.2f}s... Error: {err}"
                )
                time.sleep(current_delay)
                current_delay *= self.backoff_factor

        raise ProviderAPIError(f"LLM call failed: {last_error}")

    def _call_gemini(self, prompt: str) -> Tuple[str, int, int]:
        """Invoke Google Gemini REST API using standard requests."""
        if not self.api_key:
            raise ProviderAPIError("Missing GEMINI_API_KEY for Gemini provider")

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "generationConfig": {
                "temperature": self.temperature,
                "responseMimeType": "application/json",
            },
        }

        response = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout
        )

        if response.status_code != 200:
            raise ProviderAPIError(
                f"Gemini API returned HTTP {response.status_code}: {response.text}",
                status_code=response.status_code,
                response_text=response.text
            )

        data = response.json()
        try:
            candidate = data["candidates"][0]
            text = candidate["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as err:
            raise ProviderAPIError(
                f"Unexpected Gemini API response structure: {data}"
            ) from err

        usage = data.get("usageMetadata", {})
        prompt_tokens = usage.get("promptTokenCount", max(1, len(prompt.split()) * 4 // 3))
        completion_tokens = usage.get("candidatesTokenCount", max(1, len(text.split()) * 4 // 3))

        return text, prompt_tokens, completion_tokens

    def _call_openai(self, prompt: str) -> Tuple[str, int, int]:
        """Invoke OpenAI REST API using standard requests."""
        if not self.api_key:
            raise ProviderAPIError("Missing OPENAI_API_KEY for OpenAI provider")

        url = "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=self.timeout
        )

        if response.status_code != 200:
            raise ProviderAPIError(
                f"OpenAI API returned HTTP {response.status_code}: {response.text}",
                status_code=response.status_code,
                response_text=response.text
            )

        data = response.json()
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError) as err:
            raise ProviderAPIError(
                f"Unexpected OpenAI API response structure: {data}"
            ) from err

        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", max(1, len(prompt.split()) * 4 // 3))
        completion_tokens = usage.get("completion_tokens", max(1, len(text.split()) * 4 // 3))

        return text, prompt_tokens, completion_tokens

    def _call_mock(
        self,
        clean_job_text: str,
        source_metadata: Optional[Dict[str, Any]] = None
    ) -> Tuple[str, int, int]:
        """Intelligent offline mock extractor with zero API dependencies.

        Adheres strictly to The Strict Null Rule and Evidence Requirement.
        """
        metadata = source_metadata or {}
        text = clean_job_text.strip()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        first_line = lines[0] if lines else ""

        evidence: Dict[str, str] = {}

        # 1. Company
        company = None
        if "company" in metadata and metadata["company"]:
            company = str(metadata["company"]).strip().title()
        elif "board_token" in metadata and metadata["board_token"]:
            company = str(metadata["board_token"]).strip().title()
        elif "site" in metadata and metadata["site"]:
            company = str(metadata["site"]).strip().title()

        if not company:
            # Check prefix line e.g. "Company: Stripe"
            comp_match = re.search(r"^[^\n]*Company:\s*([^\n\r|]+)", text, re.MULTILINE | re.IGNORECASE)
            if comp_match:
                company = comp_match.group(1).strip()
                evidence["company"] = comp_match.group(0).strip()
            elif "|" in first_line:
                # Hacker News style: "Stripe | US Remote | Staff Data Eng ..."
                parts = [p.strip() for p in first_line.split("|")]
                if parts:
                    company = parts[0]
                    evidence["company"] = parts[0]

        if not company:
            company = "Unknown"
        elif "company" not in evidence:
            # Find literal presence of company in text for evidence
            for line in lines:
                if company.lower() in line.lower():
                    evidence["company"] = line[:100]
                    break

        # 2. Title & Seniority
        title = metadata.get("title") or metadata.get("raw_title")
        if not title:
            title_match = re.search(r"^(?:Title|Role):\s*([^\n]+)", text, re.MULTILINE | re.IGNORECASE)
            if title_match:
                title = title_match.group(1).strip()
                evidence["title"] = title_match.group(0).strip()
            elif "|" in first_line:
                parts = [p.strip() for p in first_line.split("|")]
                for part in parts[1:]:
                    if any(kw in part.lower() for kw in ["engineer", "eng", "developer", "architect", "lead"]):
                        title = part
                        evidence["title"] = part
                        break

        if not title:
            # Look for common engineering title in first few lines
            for line in lines[:5]:
                if any(kw in line.lower() for kw in ["data engineer", "ml engineer", "machine learning engineer", "analytics engineer", "platform engineer"]):
                    title = line
                    evidence["title"] = line
                    break

        if not title:
            title = "Data Engineer"

        if "title" not in evidence:
            for line in lines:
                if title.lower() in line.lower():
                    evidence["title"] = line[:100]
                    break

        # Seniority extraction (STRICT: only if stated in title or text)
        title_lower = title.lower()
        seniority = None
        if any(w in title_lower for w in ["staff", "principal", "distinguished", "architect", "director", "head of"]):
            seniority = "staff_plus"
            for w in ["staff", "principal", "distinguished", "architect", "director", "head of"]:
                if w in title_lower:
                    evidence["seniority"] = title
                    break
        elif any(w in title_lower for w in ["senior", "sr.", "sr ", "lead"]):
            seniority = "senior"
            evidence["seniority"] = title
        elif any(w in title_lower for w in ["junior", "jr.", "jr ", "entry", "associate"]):
            seniority = "junior"
            evidence["seniority"] = title
        elif any(w in title_lower for w in ["mid", " ii", " 2"]):
            seniority = "mid"
            evidence["seniority"] = title
        else:
            # Check body text for explicit seniority declaration
            sen_match = re.search(r"\b(senior|staff|principal|junior|mid-level)\s+(?:data|ml|software)\s+engineer\b", text, re.IGNORECASE)
            if sen_match:
                word = sen_match.group(1).lower()
                evidence["seniority"] = sen_match.group(0)
                if word in ["staff", "principal"]:
                    seniority = "staff_plus"
                elif word == "senior":
                    seniority = "senior"
                elif word in ["junior", "entry"]:
                    seniority = "junior"
                elif word in ["mid-level", "mid"]:
                    seniority = "mid"

        # 3. Remote Policy & Location
        remote_policy = None
        text_lower = text.lower()

        if re.search(r"\b(hybrid|days in office|hybrid\s*-\s*\d+)\b", text_lower):
            remote_policy = "hybrid"
            m = re.search(r"[^\n]*(?:hybrid|days in office)[^\n]*", text, re.IGNORECASE)
            if m:
                evidence["remote_policy"] = m.group(0).strip()
        elif re.search(r"\b(remote|us remote|work from home|distributed team)\b", text_lower):
            remote_policy = "remote"
            m = re.search(r"[^\n]*(?:remote|us remote|work from home)[^\n]*", text, re.IGNORECASE)
            if m:
                evidence["remote_policy"] = m.group(0).strip()
        elif re.search(r"\b(on-site|onsite|in office|in-office)\b", text_lower):
            remote_policy = "onsite"
            m = re.search(r"[^\n]*(?:on-site|onsite|in office|in-office)[^\n]*", text, re.IGNORECASE)
            if m:
                evidence["remote_policy"] = m.group(0).strip()

        # City / Country extraction
        city = None
        country = None
        city_match = re.search(r"\b(San Francisco|New York|Seattle|Austin|Boston|Chicago|Denver|Los Angeles|Atlanta|Sunnyvale|Mountain View)\b", text, re.IGNORECASE)
        if city_match:
            city = city_match.group(1)
            country = "US"
            evidence["city"] = city_match.group(0)
        elif "US Remote" in text or "US" in text:
            country = "US"
            country_m = re.search(r"\b(US Remote|United States|USA|\bUS\b)", text)
            if country_m:
                evidence["country"] = country_m.group(0)

        # 4. Salary Normalization (STRICT NULL: null if no explicit compensation numbers)
        salary_min = None
        salary_max = None
        currency = None

        # Pattern: Hourly rate e.g. $80/hr, $80 / hr, $75 per hour
        hourly_match = re.search(r"\$([0-9]{2,3}(?:\.[0-9]+)?)\s*(?:/\s*|\s+per\s+)(?:hr|hour)", text, re.IGNORECASE)
        if hourly_match:
            hr_rate = float(hourly_match.group(1))
            salary_min = round(hr_rate * 2080, 2)
            salary_max = round(hr_rate * 2080, 2)
            currency = "USD"
            evidence["salary"] = hourly_match.group(0)
        else:
            # Pattern: Ranges like $180,000 - $225,000 or $220k-$280k or $150k - $180k
            range_match = re.search(
                r"\$([0-9]{2,3}(?:,[0-9]{3})*|[0-9]{2,3})\s*(k)?\s*[-–—to]+\s*\$?([0-9]{2,3}(?:,[0-9]{3})*|[0-9]{2,3})\s*(k)?",
                text,
                re.IGNORECASE
            )
            if range_match:
                raw_min = range_match.group(1).replace(",", "")
                k_min = range_match.group(2)
                raw_max = range_match.group(3).replace(",", "")
                k_max = range_match.group(4) or k_min

                val_min = float(raw_min)
                val_max = float(raw_max)

                if k_min or (val_min < 1000 and (k_max or val_max < 1000)):
                    val_min *= 1000
                if k_max or val_max < 1000:
                    val_max *= 1000

                salary_min = val_min
                salary_max = val_max
                currency = "USD"
                evidence["salary"] = range_match.group(0)
            else:
                # Single salary figure e.g. $160,000 per year or $160k
                single_match = re.search(r"\$([0-9]{2,3}(?:,[0-9]{3})+|[0-9]{2,3}\s*k)", text, re.IGNORECASE)
                if single_match:
                    raw_s = single_match.group(1).lower().replace(",", "").replace("k", "").strip()
                    val = float(raw_s)
                    if "k" in single_match.group(1).lower() or val < 1000:
                        val *= 1000
                    salary_min = val
                    salary_max = val
                    currency = "USD"
                    evidence["salary"] = single_match.group(0)

        # 5. Employment Type (STRICT NULL: null if unstated in text)
        employment_type = None
        if re.search(r"\b(full-time|full_time|full time)\b", text_lower):
            employment_type = "full_time"
            m = re.search(r"\b(full-time|full_time|full time)\b", text, re.IGNORECASE)
            if m:
                evidence["employment_type"] = m.group(0)
        elif re.search(r"\b(contract|contractor)\b", text_lower):
            employment_type = "contract"
            m = re.search(r"\b(contract|contractor)\b", text, re.IGNORECASE)
            if m:
                evidence["employment_type"] = m.group(0)
        elif re.search(r"\b(intern|internship)\b", text_lower):
            employment_type = "intern"
            m = re.search(r"\b(intern|internship)\b", text, re.IGNORECASE)
            if m:
                evidence["employment_type"] = m.group(0)
        else:
            employment_type = None

        # 6. Skills (STRICT: only include technologies explicitly mentioned in text)
        catalog = [
            "Python", "SQL", "Spark", "dbt", "Kafka", "Flink", "PyTorch", "TensorFlow",
            "Kubernetes", "Docker", "Delta Lake", "Snowflake", "BigQuery", "Databricks",
            "Airflow", "AWS", "GCP", "Azure", "Java", "Scala", "Go", "C++", "Postgres",
            "Scikit-learn", "Hadoop", "Pandas"
        ]
        skills = []
        for skill in catalog:
            escaped = re.escape(skill)
            if re.search(rf"\b{escaped}\b", text, re.IGNORECASE):
                skills.append(skill)

        if skills:
            # Find the line in the posting that mentions the skills for verbatim evidence
            for line in lines:
                if any(re.search(rf"\b{re.escape(s)}\b", line, re.IGNORECASE) for s in skills):
                    evidence["skills"] = line[:200]
                    break

        # 7. Visa Sponsorship (STRICT NULL: null if unstated)
        visa_sponsorship = None
        no_visa_match = re.search(
            r"(?:no\s+visa|not\s+(?:provide|offer|sponsor)\s+visa|unable\s+to\s+sponsor|cannot\s+sponsor|visa:\s*no|without\s+requiring\s+sponsorship|no\s+sponsorship)",
            text,
            re.IGNORECASE
        )
        yes_visa_match = re.search(
            r"(?:visa\s+sponsorship\s+(?:available|provided|offered)|we\s+(?:provide|offer|sponsor)\s+visa|visa:\s*yes|sponsorship\s+available)",
            text,
            re.IGNORECASE
        )

        if no_visa_match:
            visa_sponsorship = False
            evidence["visa_sponsorship"] = no_visa_match.group(0)
        elif yes_visa_match:
            visa_sponsorship = True
            evidence["visa_sponsorship"] = yes_visa_match.group(0)

        record = {
            "company": company,
            "title": title,
            "seniority": seniority,
            "city": city,
            "country": country,
            "remote_policy": remote_policy,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "currency": currency,
            "employment_type": employment_type,
            "skills": skills,
            "visa_sponsorship": visa_sponsorship,
            "posted_date": None,
            "evidence": evidence,
        }

        json_output = json.dumps(record, indent=2)

        # Realistic token accounting
        prompt_tokens = max(50, len(text.split()) * 4 // 3 + 450)
        completion_tokens = max(30, len(json_output.split()) * 4 // 3)

        return json_output, prompt_tokens, completion_tokens


def verify_evidence(clean_job_text: str, evidence: Dict[str, str], case_sensitive: bool = False) -> Dict[str, bool]:
    """Verify that every non-null evidence snippet actually appears in the job text.

    Args:
        clean_job_text: The source job text.
        evidence: Dictionary mapping field name to verbatim quote.
        case_sensitive: Whether substring matching should be case-sensitive.

    Returns:
        Dict mapping each field name to a boolean indicating verification success.
    """
    results: Dict[str, bool] = {}
    target_text = clean_job_text if case_sensitive else clean_job_text.lower()

    for field, snippet in evidence.items():
        if not snippet:
            results[field] = True
            continue
        cleaned_snippet = snippet.strip()
        query = cleaned_snippet if case_sensitive else cleaned_snippet.lower()
        results[field] = query in target_text

    return results
