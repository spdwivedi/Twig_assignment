"""Role and location filtering for the Twig Ingestion Pipeline.

Filters job postings to the assignment's exact target scope:
1. Target roles: Data Engineering, ML Engineering, Analytics Engineering,
   Data Platform Engineering, and Data Science.
2. Target geography: US or US-Remote.
"""

import re
from typing import Any, Dict, List, Optional, Set


class RoleFilter:
    """Filters job postings based on target role titles and US/remote eligibility."""

    # Explicit role titles and patterns (supporting IC, Lead, and plural formulations)
    _TARGET_TITLE_PATTERN = re.compile(
        r"""
        \b(
            # Prefix role formulations: data engineer(s), ml scientist(s), etc.
            (?:data|machine\s*learning|ml|analytics|platform|mlops|ai/ml|ai\s*and\s*ml|big\s*data|data\s*infra(?:structure)?|business\s*intelligence|bi)\s*(?:software\s+)?(?:engineer(?:ing|s)?|scientist(?:s)?|developer(?:s)?|architect(?:s)?)
            |
            # Compound roles: data platform engineer(s), ml infrastructure engineer(s)
            (?:data|ml|machine\s*learning)\s+(?:platform|infra(?:structure)?|ops|systems)\s+(?:engineer(?:ing|s)?|architect(?:s)?|developer(?:s)?)
            |
            # Reverse / specialization formulations: software engineer (data), swe - machine learning
            (?:software\s+engineer(?:s)?|swe|staff\s+engineer|principal\s+engineer|lead\s+engineer)\s*[-–—,/|:\(\[]\s*(?:.*?\b)?(?:data|machine\s*learning|ml|analytics|platform|mlops|ai/ml|data\s*platform|data\s*infra(?:structure)?)
            |
            # Standalone dedicated titles
            data\s+engineer(?:ing|s)?
            |
            machine\s+learning\s+engineer(?:ing|s)?
            |
            ml\s+engineer(?:ing|s)?
            |
            analytics\s+engineer(?:ing|s)?
            |
            platform\s+engineer(?:ing|s)?
            |
            data\s+scientist(?:s)?
            |
            machine\s+learning\s+scientist(?:s)?
            |
            ml\s+scientist(?:s)?
            |
            mlops\s+engineer(?:ing|s)?
            |
            data\s+architect(?:s)?
            |
            database\s+engineer(?:ing|s)?
            |
            etl\s+developer(?:s)?
        )\b
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Keywords indicating non-engineering / sales / recruiting roles to strictly exclude
    _EXCLUDE_TITLE_PATTERN = re.compile(
        r"""
        \b(
            recruiter|recruiting|sourcer|talent\s+acquisition
            |
            account\s+executive|sales\s+rep(?:resentative)?|sales\s+manager|business\s+development
            |
            marketing|growth\s+lead|content\s+writer|copywriter
            |
            product\s+manager|program\s+manager|project\s+manager|scrum\s+master
            |
            legal|counsel|paralegal|compliance\s+officer
            |
            customer\s+support|customer\s+success|tech\s+support
            |
            frontend|front-end|ui/ux|ux\s+designer|graphic\s+designer
            |
            ios\s+developer|android\s+developer|mobile\s+developer
            |
            salesforce\s+admin(?:istrator)?|data\s+entry
        )\b
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # US State names
    _US_STATES: Set[str] = {
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
        "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
        "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
        "maine", "maryland", "massachusetts", "michigan", "minnesota",
        "mississippi", "missouri", "montana", "nebraska", "nevada",
        "new hampshire", "new jersey", "new mexico", "new york",
        "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
        "pennsylvania", "rhode island", "south carolina", "south dakota",
        "tennessee", "texas", "utah", "vermont", "virginia", "washington",
        "west virginia", "wisconsin", "wyoming", "district of columbia"
    }

    # Major US metro areas and tech hubs
    _US_CITIES_PATTERN = re.compile(
        r"""
        \b(
            san\s+francisco|sf|bay\s+area|silicon\s+valley|san\s+jose|mountain\s+view|sunnyvale|palo\s+alto|menlo\s+park|redwood\s+city|oakland|san\s+mateo|santa\s+clara|cupertino|berkeley
            |
            new\s+york|nyc|manhattan|brooklyn
            |
            seattle|bellevue|redmond
            |
            austin|dallas|houston|san\s+antonio
            |
            boston|cambridge
            |
            chicago
            |
            los\s+angeles|la|santa\s+monica|culver\s+city|irvine|san\s+diego
            |
            denver|boulder
            |
            atlanta
            |
            portland
            |
            raleigh|durham|research\s+triangle
            |
            pittsburgh
            |
            salt\s+lake\s+city|lehi
            |
            minneapolis|st\.\s*paul
            |
            washington\s*d\.?c\.?|northern\s+virginia|nova
            |
            miami|tampa|orlando
            |
            phoenix|scottsdale
        )\b
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # Explicit US indicators
    _US_EXPLICIT_PATTERN = re.compile(
        r"\b(united\s+states(?:\s+of\s+america)?|usa|u\.s\.a?|us(?:\s+only|\s+remote|\s+citizen)?)\b",
        re.IGNORECASE,
    )

    # Remote indicator keywords
    _REMOTE_PATTERN = re.compile(
        r"\b(remote|work\s+from\s+home|wfh|telecommute|anywhere|distributed|virtual)\b",
        re.IGNORECASE,
    )

    # Explicit non-US countries or regions (must NOT be matched unless US is also explicitly present)
    _NON_US_PATTERN = re.compile(
        r"""
        \b(
            uk|united\s+kingdom|london|england|scotland
            |
            canada|toronto|vancouver|montreal|ottawa
            |
            germany|berlin|munich|frankfurt|hamburg
            |
            france|paris|emea|europe|apac|latam
            |
            australia|sydney|melbourne|brisbane
            |
            india|bangalore|bengaluru|hyderabad|mumbai|delhi|pune
            |
            singapore|japan|tokyo|brazil|sao\s+paulo
            |
            ireland|dublin|netherlands|amsterdam|sweden|stockholm
            |
            spain|madrid|barcelona|switzerland|zurich|poland|warsaw
        )\b
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    # 2-letter US state postal abbreviations
    _US_STATE_CODES_UPPER = re.compile(
        r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b"
    )

    # State postal codes preceded by comma, slash, or dash (e.g. ", ca" or "- ny")
    _US_STATE_CODES_SUFFIX = re.compile(
        r"[,/\-\(]\s*(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc)\b",
        re.IGNORECASE,
    )

    # Keywords for free-text Hacker News comment filtering (including plurals and variants)
    _HN_ROLE_KEYWORDS_PATTERN = re.compile(
        r"\b(data\s+engineer(?:ing|s)?|machine\s+learning|ml\s+engineer(?:ing|s)?|analytics\s+engineer(?:ing|s)?|data\s+platform|platform\s+engineer(?:ing|s)?|data\s+scientist(?:s)?|mlops|data\s+infra(?:structure)?|ai/ml)\b",
        re.IGNORECASE,
    )

    def is_target_title(self, title: str) -> bool:
        """Evaluate if the job title matches the target Data/ML engineering scope.

        Args:
            title: Job title string.

        Returns:
            True if within target role scope, False otherwise.
        """
        if not title or not isinstance(title, str):
            return False

        clean_title = title.strip()
        if not clean_title:
            return False

        # Exclude obvious non-target titles first (recruiting, sales, marketing, frontend, etc.)
        if self._EXCLUDE_TITLE_PATTERN.search(clean_title):
            return False

        return self._TARGET_TITLE_PATTERN.search(clean_title) is not None

    def is_us_or_remote(self, location: str, description: str = "") -> bool:
        """Evaluate if the location qualifies as US or US-Remote.

        Args:
            location: Location string (e.g. 'San Francisco, CA', 'US Remote').
            description: Optional job description text for supplemental location context.

        Returns:
            True if location is US or Remote, False if strictly international.
        """
        loc_str = (location or "").strip()
        desc_snippet = (description or "")[:500].strip()

        # Build full search space for location signals
        target_str = f"{loc_str} {desc_snippet}".strip() if loc_str else desc_snippet
        if not target_str:
            return False

        target_lower = target_str.lower()

        # Check explicit US keywords
        has_us_explicit = bool(self._US_EXPLICIT_PATTERN.search(target_str))
        has_us_cities = bool(self._US_CITIES_PATTERN.search(target_str))
        has_us_states = any(state in target_lower for state in self._US_STATES)
        has_us_codes = bool(self._US_STATE_CODES_UPPER.search(target_str)) or bool(
            self._US_STATE_CODES_SUFFIX.search(target_str)
        )
        is_us = has_us_explicit or has_us_cities or has_us_states or has_us_codes

        # Check remote keywords
        is_remote = bool(self._REMOTE_PATTERN.search(target_str))

        # Check explicit non-US geography
        is_non_us = bool(self._NON_US_PATTERN.search(target_str))

        # If it explicitly names a foreign location without specifying US / US-Remote, reject
        if is_non_us and not (has_us_explicit or is_us):
            return False

        # If it is US or Remote, it's accepted
        return is_us or is_remote

    def filter_postings(self, postings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter a list of raw job postings to only matching candidates.

        - For Greenhouse and Lever postings: checks title and location.
        - For Hacker News postings: checks if the comment body mentions both
          a target role keyword AND a US/remote keyword.

        Args:
            postings: List of raw posting dictionaries.

        Returns:
            Filtered list of matching candidate postings.
        """
        candidates: List[Dict[str, Any]] = []

        for p in postings:
            source = str(p.get("source", "")).strip().lower()

            if source in ("hn", "hackernews"):
                # Free-text Hacker News comment filtering
                text_to_check = (
                    f"{p.get('title', '')} {p.get('location', '')} {p.get('location_raw', '')} "
                    f"{p.get('text', '')} {p.get('content', '')} {p.get('description', '')} "
                    f"{p.get('description_raw', '')} {p.get('body', '')}"
                )
                has_role = bool(self._HN_ROLE_KEYWORDS_PATTERN.search(text_to_check))
                has_location = self.is_us_or_remote(
                    location=str(p.get("location") or p.get("location_raw") or ""),
                    description=text_to_check,
                )
                if has_role and has_location:
                    candidates.append(p)
            else:
                # Structured ATS feed (Greenhouse, Lever) filtering
                title = str(p.get("title") or p.get("text") or "").strip()

                raw_loc = p.get("location") if p.get("location") is not None else p.get("location_raw", "")
                if isinstance(raw_loc, dict):
                    location = str(raw_loc.get("name") or raw_loc.get("location") or "").strip()
                else:
                    location = str(raw_loc or "").strip()

                description = str(
                    p.get("description_raw")
                    or p.get("description")
                    or p.get("content")
                    or p.get("descriptionPlain")
                    or p.get("text")
                    or ""
                )

                if self.is_target_title(title) and self.is_us_or_remote(location, description):
                    candidates.append(p)

        return candidates
