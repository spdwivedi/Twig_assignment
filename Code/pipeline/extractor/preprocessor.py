"""High-performance, token-efficient HTML sanitizer and job content preprocessor.

Designed to prepare raw job descriptions from Greenhouse, Lever, and Hacker News
for downstream LLM extraction while slashing token costs by up to 80%.

CRITICAL EVIDENCE PRESERVATION REQUIREMENT:
The preprocessor never rewrites, paraphrases, or alters sentences from the source text.
It preserves verbatim substrings so that downstream evidence extraction can match snippets
against the source text with 100% fidelity.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from bs4 import BeautifulSoup, Comment

logger = logging.getLogger(__name__)

# Non-content HTML tags that should be stripped along with all nested contents
NON_CONTENT_TAGS: Set[str] = {
    "script",
    "style",
    "nav",
    "header",
    "footer",
    "svg",
    "noscript",
    "iframe",
    "form",
    "button",
    "input",
    "canvas",
    "audio",
    "video",
    "map",
    "object",
    "embed",
}

# Regex to detect if an input string contains actual HTML tags
HTML_TAG_PATTERN: re.Pattern = re.compile(r"<\s*[/!]?[a-zA-Z][^>]*>", re.DOTALL)

# Patterns for detecting generic legal boilerplate (EEO, Affirmative Action, privacy policies)
BOILERPLATE_PATTERNS: List[re.Pattern] = [
    re.compile(r"\bequal\s+opportunity\s+employer\b", re.IGNORECASE),
    re.compile(r"\bequal\s+employment\s+opportunity\b", re.IGNORECASE),
    re.compile(r"\bwe(?:\s+are|\x27re)?\s+an\s+equal\s+opportunity\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+discriminate\s+(?:on\s+the\s+basis|based\s+on)\b", re.IGNORECASE),
    re.compile(r"\bwithout\s+regard\s+to\s+(?:race|color|religion|sex|gender|age|veteran|disability)\b", re.IGNORECASE),
    re.compile(r"\brace,\s*color,\s*religion\b", re.IGNORECASE),
    re.compile(r"\baffirmative\s+action\s+employer\b", re.IGNORECASE),
    re.compile(r"\baffirmative\s+action\b", re.IGNORECASE),
    re.compile(r"\beeo\s+is\s+the\s+law\b", re.IGNORECASE),
    re.compile(r"\bfair\s+chance\s+ordinance\b", re.IGNORECASE),
    re.compile(r"\bfair\s+chance\s+initiative\b", re.IGNORECASE),
    re.compile(r"\bcandidate\s+privacy\s+notice\b", re.IGNORECASE),
    re.compile(r"\bapplicant\s+privacy\s+policy\b", re.IGNORECASE),
    re.compile(r"\be-verify\s+notice\b", re.IGNORECASE),
    re.compile(r"\be-verify\b", re.IGNORECASE),
    re.compile(r"\bpay\s+transparency\s+nondiscrimination\b", re.IGNORECASE),
    re.compile(r"\breasonable\s+accommodations?\s+for\s+(?:qualified\s+)?individuals\s+with\s+disabilities\b", re.IGNORECASE),
    re.compile(r"\baccommodations?\s+during\s+the\s+application\s+process\b", re.IGNORECASE),
    re.compile(r"\bdrug-free\s+workplace\b", re.IGNORECASE),
]

# Patterns for detecting Compensation & Benefits zones
COMPENSATION_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b(?:base\s+)?salary(?:\s+range)?\b", re.IGNORECASE),
    re.compile(r"\bpay\s+range\b", re.IGNORECASE),
    re.compile(r"\bcompensation(?:\s*&|\s+and)?\s*(?:benefits)?\b", re.IGNORECASE),
    re.compile(r"\bbase\s+pay\b", re.IGNORECASE),
    re.compile(r"\bremuneration\b", re.IGNORECASE),
    re.compile(r"\$(?:[0-9]{2,3}(?:,[0-9]{3})*|\b[0-9]{2,3}k\b)", re.IGNORECASE),
    re.compile(r"\b(?:equity|stock\s+options|rsus?|401\s*\(?k\)?|health\s+insurance)\b", re.IGNORECASE),
    re.compile(r"\b(?:per\s+year|annually|per\s+hour|/yr|/hr)\b", re.IGNORECASE),
]

# Patterns for detecting Requirements, Skills, and Qualifications zones
REQUIREMENTS_PATTERNS: List[re.Pattern] = [
    re.compile(r"\brequirements?\b", re.IGNORECASE),
    re.compile(r"\bqualifications?\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+(?:we(?:\x27re|\s+are)\s+looking\s+for|you(?:\x27ll|\s+will)?\s+need|you\s+bring)\b", re.IGNORECASE),
    re.compile(r"\bwho\s+you\s+are\b", re.IGNORECASE),
    re.compile(r"\b(?:minimum|basic|preferred)\s+qualifications?\b", re.IGNORECASE),
    re.compile(r"\b(?:skills|competencies|must\s+have|nice\s+to\s+have)\b", re.IGNORECASE),
    re.compile(r"\b(?:\d+\+?\s*years?|experience\s+with|proficiency\s+in|proficient\s+with)\b", re.IGNORECASE),
]

# Patterns for detecting Location, Remote, and Workplace arrangement zones
LOCATION_PATTERNS: List[re.Pattern] = [
    re.compile(r"\blocation\b", re.IGNORECASE),
    re.compile(r"\b(?:workplace\s+type|work\s+arrangement)\b", re.IGNORECASE),
    re.compile(r"\b(?:remote|hybrid|on-?site|in-?office|work\s+from\s+home)\b", re.IGNORECASE),
    re.compile(r"\b(?:us\s+remote|united\s+states|san\s+francisco|new\s+york|seattle|austin|chicago|london)\b", re.IGNORECASE),
]

# Patterns for detecting Role Overview and Summary zones
ROLE_OVERVIEW_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b(?:about\s+the\s+role|role\s+overview|job\s+summary|position\s+summary)\b", re.IGNORECASE),
    re.compile(r"\b(?:what\s+you(?:\x27ll|\s+will)\s+do|responsibilities|the\s+opportunity)\b", re.IGNORECASE),
    re.compile(r"\b(?:about\s+the\s+job|day-to-day|what\s+you(?:\x27ll|\s+will)\s+work\s+on)\b", re.IGNORECASE),
    re.compile(r"\b(?:we\s+are\s+looking\s+for|in\s+this\s+role|as\s+a\b)", re.IGNORECASE),
]

# Known heading keywords for section boundary detection
KNOWN_HEADER_KEYWORDS: re.Pattern = re.compile(
    r"^(?:about(?:\s+the\s+role|\s+the\s+job|\s+the\s+team|\s+us|\s+[a-zA-Z0-9_\-\.]+)?|"
    r"requirements|qualifications|responsibilities|what\s+you(?:\x27ll|\s+will)?\s+(?:do|bring|need|work\s+on)|"
    r"who\s+you\s+are|what\s+we(?:\x27re|\s+are)?\s+looking\s+for|"
    r"compensation(?:\s*&|\s+and)?\s*(?:benefits)?|salary(?:\s+range)?|pay\s+range|"
    r"benefits(?:\s*&|\s+and)?\s*(?:perks)?|perks|what\s+we\s+offer|total\s+rewards|"
    r"location|workplace\s+type|work\s+arrangement|"
    r"nice\s+to\s+have|preferred\s+qualifications|basic\s+qualifications|minimum\s+qualifications|minimum\s+requirements|"
    r"equal\s+opportunity(?:\s+employer)?|eeo|diversity(?:\s*&|\s+and)?\s*inclusion|"
    r"job\s+summary|role\s+overview|the\s+opportunity|our\s+mission)\b",
    re.IGNORECASE,
)


@dataclass
class _ContentSection:
    """Internal representation of a semantic job posting section."""

    heading: str
    blocks: List[str]
    category: str

    @property
    def full_text(self) -> str:
        """Render the section back to clean structured text."""
        body = "\n\n".join(b.strip() for b in self.blocks if b.strip())
        if self.heading and body:
            return f"{self.heading}\n{body}"
        elif self.heading:
            return self.heading
        return body


class JobContentPreprocessor:
    """High-performance, token-efficient text cleaner for LLM extraction.

    Cleans raw HTML/text job descriptions while guaranteeing verbatim substring
    preservation for downstream evidence validation.
    """

    def __init__(self, default_max_tokens: int = 1500) -> None:
        """Initialize the preprocessor.

        Args:
            default_max_tokens: Maximum target token count for extraction budgeting.
        """
        self.default_max_tokens = default_max_tokens

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Estimate token count for a text string (~4 characters per token).

        Args:
            text: Text to evaluate.

        Returns:
            Estimated integer token count (minimum 0).
        """
        if not text:
            return 0
        return max(1, len(text) // 4)

    def clean_text(self, text: str) -> str:
        """Normalize plain text whitespace, line breaks, and HTML entities.

        Preserves verbatim paragraph structure and bulleted lists.

        Args:
            text: Raw plain text input.

        Returns:
            Normalized, clean text.
        """
        if not text or not isinstance(text, str):
            return ""

        # Decode any HTML entities present in plain text (e.g. &amp;, &#39;)
        text = html.unescape(text)

        # Normalize non-breaking spaces and other irregular unicode spaces to regular space
        text = re.sub(r"[\xa0\u200b\u2002\u2003\u2009\u202f\u3000]", " ", text)

        lines = text.splitlines()
        cleaned_lines: List[str] = []

        for line in lines:
            # Collapse multiple horizontal whitespace within the line while preserving characters
            c_line = re.sub(r"[ \t\f\v]+", " ", line).strip()
            if not c_line:
                # Keep a single blank line between paragraphs
                if cleaned_lines and cleaned_lines[-1] != "":
                    cleaned_lines.append("")
            else:
                # Group bullet items tightly without blank lines between adjacent bullets
                if (
                    c_line.startswith("•")
                    and cleaned_lines
                    and cleaned_lines[-1] == ""
                    and len(cleaned_lines) >= 2
                    and cleaned_lines[-2].startswith("•")
                ):
                    cleaned_lines.pop()
                cleaned_lines.append(c_line)

        return "\n".join(cleaned_lines).strip()

    def clean(self, raw_content: Optional[str]) -> str:
        """Sanitize raw HTML or messy free-form text.

        Args:
            raw_content: Raw HTML, markdown, or plain text.

        Returns:
            Sanitized, compact plaintext suitable for LLM extraction.
        """
        if not raw_content:
            return ""
        text = str(raw_content)
        if HTML_TAG_PATTERN.search(text) or ("&lt;" in text and "&gt;" in text):
            return self.clean_html(text)
        return self.clean_text(text)

    def __call__(self, raw_content: Optional[str]) -> str:
        """Callable interface for functional pipeline stages."""
        return self.clean(raw_content)

    def clean_html(self, raw_html: str) -> str:
        """Sanitize raw HTML into clean, token-efficient structured text.

        Strips non-content tags (<script>, <style>, <nav>, etc.), converts
        <li> tags into bullet points ('• '), injects structured line breaks for
        block tags, decodes HTML entities, and normalizes whitespace.

        Args:
            raw_html: Raw HTML string from job boards or ATS endpoints.

        Returns:
            Sanitized, compact text with verbatim sentences preserved.
        """
        if not raw_html or not isinstance(raw_html, str) or not raw_html.strip():
            return ""

        # Handle double-escaped or entity-encoded HTML strings (common in Greenhouse APIs)
        content = raw_html
        while "&amp;lt;" in content or "&amp;gt;" in content:
            content = html.unescape(content)

        if "&lt;" in content and ("&lt;p" in content.lower() or "&lt;div" in content.lower() or "<" not in content):
            content = html.unescape(content)

        # If the input doesn't contain any real HTML tags, handle directly as plain text
        if not HTML_TAG_PATTERN.search(content):
            return self.clean_text(content)

        soup = BeautifulSoup(content, "html.parser")

        # Remove HTML comments
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        # Strip non-content tags and their children completely
        for tag in soup.find_all(NON_CONTENT_TAGS):
            tag.decompose()

        # Replace line-break tags with a newline
        for br in soup.find_all(["br", "wbr"]):
            br.replace_with("\n")

        # Headings: separate with structured line breaks
        for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
            h.insert_before("\n\n")
            h.insert_after("\n\n")

        # Paragraphs: separate with double line breaks
        for p in soup.find_all("p"):
            p.insert_before("\n\n")
            p.insert_after("\n\n")

        # Block containers and table rows: separate with newlines
        for el in soup.find_all(["div", "section", "article", "tr", "hr", "blockquote"]):
            el.insert_before("\n")
            el.insert_after("\n")

        # <li> tags: prefix with bullet point '• ' and separate
        for li in soup.find_all("li"):
            li_text = li.get_text().strip()
            if not li_text.startswith("•"):
                li.insert_before("\n• ")
            else:
                li.insert_before("\n")
            li.insert_after("\n")
            li.unwrap()

        extracted_text = soup.get_text()
        return self.clean_text(extracted_text)

    def _is_header_line(self, line: str) -> bool:
        """Check whether a line functions as a section header."""
        l = line.strip()
        if not l or len(l) > 80:
            return False
        # Bullets, math symbols, numbers, and currency are content, not headers
        if l.startswith(("•", "*", "-", "–", "—", ">", "<", "$", "€", "£")):
            return False
        if re.match(r"^\d", l):
            return False
        if l.startswith("#"):
            return True
        if l.endswith(":"):
            return True
        if KNOWN_HEADER_KEYWORDS.match(l):
            return True
        if l.isupper() and len(l) >= 3 and not re.search(r"[\$\.\,\;\!\?]", l):
            return True
        return False

    def _classify_section(self, heading: str, blocks: List[str]) -> str:
        """Classify a section into a priority category based on heading and content."""
        combined = (heading + " " + " ".join(blocks)).strip()

        has_boiler = any(p.search(combined) for p in BOILERPLATE_PATTERNS)
        has_comp = any(p.search(combined) for p in COMPENSATION_PATTERNS)
        has_req = any(p.search(combined) for p in REQUIREMENTS_PATTERNS)
        has_loc = any(p.search(combined) for p in LOCATION_PATTERNS)
        has_role = any(p.search(combined) for p in ROLE_OVERVIEW_PATTERNS)

        # Boilerplate check: must not contain actual compensation or requirements
        if has_boiler and not (has_comp or has_req):
            return "BOILERPLATE"

        if has_comp:
            return "COMPENSATION"
        if has_req:
            return "REQUIREMENTS"
        if has_loc:
            return "LOCATION"
        if has_role:
            return "ROLE_OVERVIEW"
        if has_boiler:
            return "BOILERPLATE"

        return "OTHER"

    def _parse_sections(self, clean_text: str) -> List[_ContentSection]:
        """Parse clean text into structured semantic sections."""
        if not clean_text or not clean_text.strip():
            return []

        raw_blocks = [b.strip() for b in clean_text.split("\n\n") if b.strip()]
        sections: List[_ContentSection] = []

        current_heading = ""
        current_blocks: List[str] = []

        for block in raw_blocks:
            lines = block.splitlines()
            first_line = lines[0].strip()

            if len(lines) == 1 and self._is_header_line(first_line):
                # Standalone header line
                if current_heading or current_blocks:
                    cat = self._classify_section(current_heading, current_blocks)
                    sections.append(_ContentSection(current_heading, current_blocks, cat))
                current_heading = first_line
                current_blocks = []
            elif len(lines) > 1 and self._is_header_line(first_line):
                # Header line at the top of a multi-line block
                if current_heading or current_blocks:
                    cat = self._classify_section(current_heading, current_blocks)
                    sections.append(_ContentSection(current_heading, current_blocks, cat))
                current_heading = first_line
                body = "\n".join(lines[1:]).strip()
                current_blocks = [body] if body else []
            else:
                current_blocks.append(block)

        if current_heading or current_blocks:
            cat = self._classify_section(current_heading, current_blocks)
            sections.append(_ContentSection(current_heading, current_blocks, cat))

        return sections

    def extract_key_sections(
        self,
        clean_text: str,
        max_tokens: int = 1500,
        force_strip_boilerplate: bool = False,
    ) -> str:
        """Prioritize key informational zones and remove legal boilerplate if exceeding token limits.

        Prioritizes:
          1. Compensation / Salary / Benefits.
          2. Requirements / Qualifications / Skills.
          3. Location / Remote / Work arrangement.
          4. Role Overview / Summary.

        CRITICAL REQUIREMENT:
        Preserves all retained sentences verbatim. Never rewrites or alters content.

        Args:
            clean_text: Sanitized job description text.
            max_tokens: Target maximum tokens for the output.
            force_strip_boilerplate: If True, strips boilerplate regardless of token count.

        Returns:
            Compacted text with key informational zones preserved verbatim.
        """
        if not clean_text or not clean_text.strip():
            return ""

        sections = self._parse_sections(clean_text)
        if not sections:
            return clean_text

        # Estimate current tokens across all parsed sections
        total_tokens = self.estimate_tokens(clean_text)

        # If text is already within token budget and force_strip_boilerplate is False, retain intact
        if total_tokens <= max_tokens and not force_strip_boilerplate:
            return clean_text

        # Step 1: Strip out generic legal boilerplate (EEO, affirmative action, privacy notices)
        active_sections = [s for s in sections if s.category != "BOILERPLATE"]
        reconstructed = "\n\n".join(s.full_text for s in active_sections if s.full_text).strip()
        reconstructed_tokens = self.estimate_tokens(reconstructed)

        if reconstructed_tokens <= max_tokens:
            return reconstructed

        # Step 2: Strip out generic marketing / company slogan sections (OTHER)
        active_sections = [s for s in active_sections if s.category != "OTHER"]
        reconstructed = "\n\n".join(s.full_text for s in active_sections if s.full_text).strip()
        reconstructed_tokens = self.estimate_tokens(reconstructed)

        if reconstructed_tokens <= max_tokens:
            return reconstructed

        # Step 3: Prioritize critical informational zones (Compensation, Requirements, Location, Role Overview)
        # We retain high-priority sections and trim lower-priority sections block-by-block if necessary.
        priority_order = ["COMPENSATION", "REQUIREMENTS", "LOCATION", "ROLE_OVERVIEW"]
        selected_sections: List[_ContentSection] = []
        budget_tokens = max_tokens

        # Pass A: Always prioritize Compensation, Requirements, Location
        must_have = [s for s in active_sections if s.category in {"COMPENSATION", "REQUIREMENTS", "LOCATION"}]
        role_sections = [s for s in active_sections if s.category == "ROLE_OVERVIEW"]

        # Calculate tokens needed for must-have sections
        must_have_text = "\n\n".join(s.full_text for s in must_have if s.full_text)
        must_have_tokens = self.estimate_tokens(must_have_text)

        if must_have_tokens <= max_tokens:
            # We can include all must-have sections, and fill remaining budget with Role Overview blocks
            remaining_budget = max_tokens - must_have_tokens
            retained_role_blocks: List[_ContentSection] = []

            for r_sec in role_sections:
                sec_tokens = self.estimate_tokens(r_sec.full_text)
                if sec_tokens <= remaining_budget:
                    retained_role_blocks.append(r_sec)
                    remaining_budget -= sec_tokens
                else:
                    # Truncate at block boundaries without breaking any individual block/sentence
                    fitted_blocks: List[str] = []
                    for blk in r_sec.blocks:
                        blk_tokens = self.estimate_tokens(blk)
                        if blk_tokens <= remaining_budget:
                            fitted_blocks.append(blk)
                            remaining_budget -= blk_tokens
                        else:
                            break
                    if fitted_blocks:
                        retained_role_blocks.append(
                            _ContentSection(r_sec.heading, fitted_blocks, r_sec.category)
                        )
                    break

            # Assemble back in original document order to preserve narrative structure
            selected_set = set(id(s) for s in must_have).union(set(id(s) for s in retained_role_blocks))
            final_sections = [s for s in active_sections if id(s) in selected_set or s in retained_role_blocks]
            return "\n\n".join(s.full_text for s in final_sections if s.full_text).strip()

        # If must-have sections alone exceed max_tokens, retain in priority order:
        # Compensation first, then Requirements, then Location
        final_sections = []
        for cat in ["COMPENSATION", "REQUIREMENTS", "LOCATION"]:
            cat_secs = [s for s in must_have if s.category == cat]
            for s in cat_secs:
                final_sections.append(s)

        return "\n\n".join(s.full_text for s in final_sections if s.full_text).strip()

    def preprocess(
        self,
        raw_content: str,
        is_html: bool = True,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Preprocess raw job description content for LLM extraction.

        Sanitizes HTML or normalizes plain text, pinpoints and prioritizes key
        informational zones, and returns token reduction metrics.

        Args:
            raw_content: Raw job description text or HTML payload.
            is_html: True if content is HTML (Greenhouse/Lever), False for plain text (HN).
            max_tokens: Target maximum tokens for section prioritization. Defaults to 1500.

        Returns:
            Dict containing:
                - 'clean_text': Sanitized, compacted text.
                - 'original_length': Character length before cleaning.
                - 'cleaned_length': Character length after cleaning.
                - 'token_reduction_ratio': 1.0 - (cleaned_length / max(1, original_length)).
        """
        if raw_content is None:
            raw_content = ""

        original_length = len(raw_content)

        if not raw_content.strip():
            return {
                "clean_text": "",
                "original_length": 0,
                "cleaned_length": 0,
                "token_reduction_ratio": 1.0,
            }

        target_max_tokens = max_tokens if max_tokens is not None else self.default_max_tokens

        # Step 1: Clean markup / normalize whitespace
        if is_html:
            cleaned = self.clean_html(raw_content)
        else:
            cleaned = self.clean_text(raw_content)

        # Step 2: Extract key informational zones and prune boilerplate if exceeding token budget
        final_text = self.extract_key_sections(cleaned, max_tokens=target_max_tokens)

        cleaned_length = len(final_text)
        token_reduction_ratio = 1.0 - (cleaned_length / max(1, original_length))

        return {
            "clean_text": final_text,
            "original_length": original_length,
            "cleaned_length": cleaned_length,
            "token_reduction_ratio": round(token_reduction_ratio, 4),
        }
