"""Company and Location normalization utilities for Phase 3 Deduplication.

Provides:
- CompanyNormalizer: Normalizes company names, strips corporate legal suffixes,
  resolves known aliases to canonical brand casing, and preserves clean casing.
- LocationNormalizer: Extracts structured geographic entities (city, 2-letter US
  state code, country, is_remote flag, raw text) from diverse, messy ATS
  location strings.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ==============================================================================
# Canonical Company Aliases & Suffix Specifications
# ==============================================================================

# Comprehensive mapping of known brand alias variations to canonical names.
# Keys are lowercase with normalized whitespace and stripped edge punctuation.
CANONICAL_COMPANY_ALIASES: Dict[str, str] = {
    # Stripe
    "stripe": "Stripe",
    "stripe inc": "Stripe",
    "stripe inc.": "Stripe",
    "stripe, inc.": "Stripe",
    "stripe, inc": "Stripe",
    "stripe llc": "Stripe",
    "stripe payments": "Stripe",
    # Airbnb
    "airbnb": "Airbnb",
    "airbnb inc": "Airbnb",
    "airbnb inc.": "Airbnb",
    "airbnb, inc.": "Airbnb",
    "airbnb, inc": "Airbnb",
    # Databricks
    "databricks": "Databricks",
    "databricks inc": "Databricks",
    "databricks inc.": "Databricks",
    "databricks, inc.": "Databricks",
    "databricks software": "Databricks",
    # Palantir
    "palantir": "Palantir",
    "palantir technologies": "Palantir",
    "palantir tech": "Palantir",
    "palantir tech inc": "Palantir",
    "palantir tech inc.": "Palantir",
    "palantir technologies inc": "Palantir",
    "palantir technologies, inc.": "Palantir",
    # Plaid
    "plaid": "Plaid",
    "plaid inc": "Plaid",
    "plaid inc.": "Plaid",
    "plaid, inc.": "Plaid",
    "plaid technologies": "Plaid",
    # Figma
    "figma": "Figma",
    "figma inc": "Figma",
    "figma inc.": "Figma",
    "figma, inc.": "Figma",
    "figma, inc": "Figma",
    # GitLab
    "gitlab": "GitLab",
    "gitlab inc": "GitLab",
    "gitlab inc.": "GitLab",
    "gitlab, inc.": "GitLab",
    "gitlab bv": "GitLab",
    "gitlab b.v.": "GitLab",
    # Cloudflare
    "cloudflare": "Cloudflare",
    "cloudflare inc": "Cloudflare",
    "cloudflare inc.": "Cloudflare",
    "cloudflare, inc.": "Cloudflare",
    "cloudflare net": "Cloudflare",
    # Additional common tech brand aliases
    "google": "Google",
    "google llc": "Google",
    "alphabet": "Google",
    "alphabet inc": "Google",
    "meta": "Meta",
    "meta platforms": "Meta",
    "meta platforms inc": "Meta",
    "facebook": "Meta",
    "amazon": "Amazon",
    "amazon.com": "Amazon",
    "amazon com": "Amazon",
    "amazon web services": "AWS",
    "aws": "AWS",
    "apple": "Apple",
    "apple inc": "Apple",
    "apple inc.": "Apple",
    "microsoft": "Microsoft",
    "microsoft corp": "Microsoft",
    "microsoft corp.": "Microsoft",
    "microsoft corporation": "Microsoft",
    "netflix": "Netflix",
    "netflix inc": "Netflix",
    "netflix inc.": "Netflix",
    "uber": "Uber",
    "uber technologies": "Uber",
    "lyft": "Lyft",
    "lyft inc": "Lyft",
    "snowflake": "Snowflake",
    "snowflake inc": "Snowflake",
    "openai": "OpenAI",
    "openai llc": "OpenAI",
    "anthropic": "Anthropic",
    "anthropic pbc": "Anthropic",
    "github": "GitHub",
    "github inc": "GitHub",
    "reddit": "Reddit",
    "reddit inc": "Reddit",
    "spotify": "Spotify",
    "spotify ab": "Spotify",
    "doordash": "DoorDash",
    "doordash inc": "DoorDash",
    "instacart": "Instacart",
    "maplebear": "Instacart",
    "coinbase": "Coinbase",
    "coinbase inc": "Coinbase",
    "robinhood": "Robinhood",
    "pinterest": "Pinterest",
}

# Corporate legal suffix patterns to strip (ordered by specificity)
LEGAL_SUFFIXES: List[str] = [
    r"co\.,?\s*ltd\.?",
    r"l\.?l\.?c\.?",
    r"p\.?l\.?c\.?",
    r"p\.?c\.?",
    r"p\.?b\.?c\.?",
    r"l\.?p\.?",
    r"b\.?v\.?",
    r"s\.?a\.?",
    r"inc\.?",
    r"corp\.?",
    r"co\.?",
    r"ltd\.?",
    r"pvt\.?",
    r"llc\.?",
    r"llp\.?",
    r"plc\.?",
    r"corporation",
    r"technologies",
    r"technology",
    r"laboratories",
    r"solutions",
    r"holdings",
    r"software",
    r"systems",
    r"company",
    r"limited",
    r"global",
    r"group",
    r"labs",
    r"tech",
    r"gmbh",
    r"pbc",
]

_SUFFIX_REGEX = re.compile(
    r"(?:[,\s\-_/|]+|\s*[(\[])(?:(?:"
    + "|".join(LEGAL_SUFFIXES)
    + r")[)\]]?\.?)$",
    re.IGNORECASE,
)


class CompanyNormalizer:
    """Normalizes corporate entity names and resolves canonical brand identities."""

    def __init__(
        self,
        aliases: Optional[Dict[str, str]] = None,
        extra_suffixes: Optional[List[str]] = None,
    ) -> None:
        """Initializes the normalizer with default or customized alias and suffix sets."""
        self.aliases: Dict[str, str] = dict(CANONICAL_COMPANY_ALIASES)
        if aliases:
            for k, v in aliases.items():
                self.aliases[k.strip().lower()] = v

        if extra_suffixes:
            all_suffixes = list(extra_suffixes) + LEGAL_SUFFIXES
            self._suffix_pattern = re.compile(
                r"(?:[,\s\-_/|]+|\s*[(\[])(?:(?:"
                + "|".join(all_suffixes)
                + r")[)\]]?\.?)$",
                re.IGNORECASE,
            )
        else:
            self._suffix_pattern = _SUFFIX_REGEX

    def normalize_company(self, name: Optional[str], default: str = "") -> str:
        """Normalizes a company name string.

        - Handles None, empty, or whitespace-only inputs safely (returns default or empty string).
        - Strips extraneous punctuation, leading/trailing symbols, and extra whitespace.
        - Iteratively strips corporate legal suffixes (e.g. Inc, LLC, Corp, Technologies, Labs).
        - Maps known alias variations to standard canonical brand names.
        - Preserves clean brand casing (e.g. Title Case, brand casing like GitLab, acronyms like IBM).

        Args:
            name: Raw company name string (e.g. "Stripe, Inc.", "palantir tech inc").
            default: Fallback string if input is None or blank (defaults to "").

        Returns:
            Normalized canonical company name string.
        """
        if name is None:
            return default

        cleaned = re.sub(r"\s+", " ", name).strip()
        if not cleaned:
            return default

        # Clean leading/trailing punctuation and quotation marks
        cleaned = re.sub(r"^[,\s\-_/|.'\"`#]+|[,\s\-_/|.'\"`#]+$", "", cleaned).strip()
        if not cleaned:
            return default

        # 1. Direct canonical alias check on original cleaned string
        lookup_key = cleaned.lower()
        if lookup_key in self.aliases:
            return self.aliases[lookup_key]

        # Strip punctuation-only trailing chars for alias lookup
        key_no_punct = re.sub(r"[,\-_/.]+$", "", lookup_key).strip()
        if key_no_punct in self.aliases:
            return self.aliases[key_no_punct]

        # 2. Iteratively strip trailing legal suffixes
        current = cleaned
        while True:
            m = self._suffix_pattern.search(current)
            if not m:
                break
            stripped = current[: m.start()].strip(" ,.-_/|()[]{}+&#")
            # Safety guard: Never strip if it would leave the name completely empty
            if not stripped:
                break
            current = stripped

        current = re.sub(r"^[,\s\-_/|.'\"`#&]+|[,\s\-_/|.'\"`#&]+$", "", current).strip()

        # 3. Check alias dictionary on suffix-stripped string
        stripped_key = current.lower()
        if stripped_key in self.aliases:
            return self.aliases[stripped_key]

        # 4. Clean formatting and preserve casing
        return self._format_casing(current)

    @classmethod
    def normalize(cls, name: Optional[str], default: str = "") -> str:
        """Classmethod helper for one-off normalization without explicit instantiation."""
        return cls().normalize_company(name, default=default)

    @staticmethod
    def _format_casing(name: str) -> str:
        """Applies clean brand casing rules to an unaliased company name."""
        if not name:
            return ""

        tokens = name.split()
        formatted_tokens = []

        for token in tokens:
            # Preserve special acronyms (e.g. IBM, AWS, AMD, HP, AI)
            if token.isupper() and len(token) <= 4:
                formatted_tokens.append(token)
            # If all lowercase, title-case it
            elif token.islower():
                formatted_tokens.append(token.capitalize())
            # If all uppercase but longer than 4 chars, title-case (e.g. MICROSOFT -> Microsoft)
            elif token.isupper() and len(token) > 4:
                formatted_tokens.append(token.capitalize())
            # Mixed brand casing (e.g. eBay, OpenAI, MacDonald, FedEx, DataDog)
            else:
                formatted_tokens.append(token)

        return " ".join(formatted_tokens)


# ==============================================================================
# Location Normalizer Data & Heuristics
# ==============================================================================

# 50 US States + DC mapping from lowercase full name to 2-letter postal code
US_STATES: Dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    # DC variants
    "washington dc": "DC",
    "washington d.c.": "DC",
    "washington d.c": "DC",
    "d.c.": "DC",
    "dc": "DC",
    "washington state": "WA",
    "new york state": "NY",
    # US Territories
    "puerto rico": "PR",
    "guam": "GU",
    "virgin islands": "VI",
}

US_STATE_CODES: Set[str] = set(US_STATES.values())

# Prominent US tech hubs with deterministic city -> (canonical_city, state)
KNOWN_US_CITIES: Dict[str, Tuple[str, str]] = {
    "san francisco": ("San Francisco", "CA"),
    "sf": ("San Francisco", "CA"),
    "new york": ("New York", "NY"),
    "new york city": ("New York", "NY"),
    "nyc": ("New York", "NY"),
    "seattle": ("Seattle", "WA"),
    "austin": ("Austin", "TX"),
    "chicago": ("Chicago", "IL"),
    "boston": ("Boston", "MA"),
    "los angeles": ("Los Angeles", "CA"),
    "la": ("Los Angeles", "CA"),
    "denver": ("Denver", "CO"),
    "atlanta": ("Atlanta", "GA"),
    "atl": ("Atlanta", "GA"),
    "dallas": ("Dallas", "TX"),
    "houston": ("Houston", "TX"),
    "philadelphia": ("Philadelphia", "PA"),
    "phoenix": ("Phoenix", "AZ"),
    "portland": ("Portland", "OR"),
    "miami": ("Miami", "FL"),
    "san jose": ("San Jose", "CA"),
    "san diego": ("San Diego", "CA"),
    "salt lake city": ("Salt Lake City", "UT"),
    "minneapolis": ("Minneapolis", "MN"),
    "pittsburgh": ("Pittsburgh", "PA"),
    "raleigh": ("Raleigh", "NC"),
    "boulder": ("Boulder", "CO"),
    "cambridge": ("Cambridge", "MA"),
    "sunnyvale": ("Sunnyvale", "CA"),
    "mountain view": ("Mountain View", "CA"),
    "palo alto": ("Palo Alto", "CA"),
    "menlo park": ("Menlo Park", "CA"),
    "redwood city": ("Redwood City", "CA"),
    "oakland": ("Oakland", "CA"),
    "culver city": ("Culver City", "CA"),
    "santa monica": ("Santa Monica", "CA"),
    "bellevue": ("Bellevue", "WA"),
    "kirkland": ("Kirkland", "WA"),
    "jersey city": ("Jersey City", "NJ"),
    "hoboken": ("Hoboken", "NJ"),
    "washington dc": ("Washington", "DC"),
    "washington d.c.": ("Washington", "DC"),
    "washington d.c": ("Washington", "DC"),
    "washington": ("Washington", "DC"),
}

# City-states where city and country coincide
CITY_STATES: Dict[str, Tuple[str, str]] = {
    "singapore": ("Singapore", "SG"),
    "monaco": ("Monaco", "MC"),
    "hong kong": ("Hong Kong", "HK"),
    "vatican city": ("Vatican City", "VA"),
}

# International and domestic country mapping
COUNTRY_MAP: Dict[str, str] = {
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "u.s.a.": "US",
    "u.s.": "US",
    "us": "US",
    "united kingdom": "UK",
    "great britain": "UK",
    "uk": "UK",
    "england": "UK",
    "scotland": "UK",
    "wales": "UK",
    "canada": "CA",
    "germany": "DE",
    "deutschland": "DE",
    "france": "FR",
    "japan": "JP",
    "australia": "AU",
    "india": "IN",
    "ireland": "IE",
    "netherlands": "NL",
    "sweden": "SE",
    "switzerland": "CH",
    "israel": "IL",
    "spain": "ES",
    "italy": "IT",
    "brazil": "BR",
    "mexico": "MX",
    "singapore": "SG",
    "poland": "PL",
    "romania": "RO",
    "belgium": "BE",
    "new zealand": "NZ",
}

# Regex for remote policy detection
REMOTE_PATTERN = re.compile(
    r"\b(remote|us-remote|remote\s*[-–—/]\s*us[a]?|work\s+from\s+anywhere|anywhere\s+in\s+(?:the\s+)?us[a]?|hybrid|wfh|telecommute|virtual|distributed)\b",
    re.IGNORECASE,
)


class LocationNormalizer:
    """Parses ATS location strings into standardized geographic entities and remote flags."""

    def __init__(self) -> None:
        """Initializes the location normalizer."""
        self.us_states = US_STATES
        self.us_state_codes = US_STATE_CODES
        self.known_cities = KNOWN_US_CITIES
        self.country_map = COUNTRY_MAP

    def normalize_location(self, location_str: Optional[str]) -> Dict[str, Any]:
        """Normalizes a raw location string into a structured entity dictionary.

        Output format:
            {"city": Optional[str], "state": Optional[str], "country": str, "is_remote": bool, "raw": str}

        Args:
            location_str: Raw location string (e.g. "San Francisco, CA", "Remote, United States").

        Returns:
            Dictionary matching the target entity resolution schema.
        """
        if location_str is None or not location_str.strip():
            return {
                "city": None,
                "state": None,
                "country": "",
                "is_remote": False,
                "raw": "" if location_str is None else location_str,
            }

        raw = location_str
        text = re.sub(r"\s+", " ", location_str.strip())

        # 1. Detect remote flag across diverse ATS phrasing
        is_remote = bool(REMOTE_PATTERN.search(text))

        # 2. Check for US- prefix commonly emitted by Greenhouse (e.g. "US-San Francisco")
        forced_us = False
        m_us_pref = re.match(r"^us\s*[-–—]\s*(.*)$", text, re.IGNORECASE)
        if m_us_pref:
            forced_us = True
            text = m_us_pref.group(1).strip()

        # Handle multi-location listings separated by semicolons (e.g. "SF, CA; NYC, NY")
        if ";" in text:
            text = text.split(";")[0].strip()

        # 3. Clean remote descriptors out of geographic text
        cleaned = re.sub(
            r"\s*\(\s*(?:remote|hybrid|wfh|virtual|onsite)[^)]*\)",
            "",
            text,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\s*[/|;–—-]\s*(?:remote|hybrid|wfh|virtual)(?:\s*[-–—]\s*(?:us|usa|united states))?\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"^\s*(?:remote|hybrid|wfh|virtual)\s*[/|;–—,-]\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"[,\s]+(?:remote|hybrid)\s*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.strip(" ,.-_/|")

        city: Optional[str] = None
        state: Optional[str] = None
        country: str = "US" if forced_us else ""

        lower_clean = cleaned.lower()

        # Check if the remaining string is empty or purely remote / country text
        if not cleaned or lower_clean in (
            "remote",
            "hybrid",
            "work from anywhere",
            "anywhere in us",
            "us-remote",
            "anywhere",
        ):
            if forced_us or any(
                u in raw.lower()
                for u in ("united states", "usa", "us-remote", "remote - us", "anywhere in us", "us")
            ):
                country = "US"
            return {
                "city": None,
                "state": None,
                "country": country,
                "is_remote": is_remote,
                "raw": raw,
            }

        # Check if text is a known city-state (e.g. Singapore, Monaco)
        if lower_clean in CITY_STATES:
            c_name, c_code = CITY_STATES[lower_clean]
            return {
                "city": c_name,
                "state": None,
                "country": c_code,
                "is_remote": is_remote,
                "raw": raw,
            }

        # Check if text is purely a country name
        if lower_clean in self.country_map:
            return {
                "city": None,
                "state": None,
                "country": self.country_map[lower_clean],
                "is_remote": is_remote,
                "raw": raw,
            }

        # Split into comma-delimited segments
        parts = [p.strip(" .") for p in cleaned.split(",") if p.strip(" .")]
        # Filter out purely numeric postal codes (e.g. "94105", "10001-1234")
        parts = [p for p in parts if not re.match(r"^\d{5}(?:-\d{4})?$", p)]

        # --- Case A: 3+ parts (e.g. ["San Francisco", "California", "United States"]) ---
        if len(parts) >= 3:
            p_last = parts[-1].lower()
            if p_last in self.country_map:
                country = self.country_map[p_last]
                p_state = parts[-2].lower()
                if p_state in self.us_states:
                    state = self.us_states[p_state]
                    country = "US"
                elif p_state.upper() in self.us_state_codes:
                    state = p_state.upper()
                    country = "US"
                city = parts[0]
            else:
                city = parts[0]
                p_state = parts[1].lower()
                if p_state in self.us_states:
                    state = self.us_states[p_state]
                    country = "US"
                elif p_state.upper() in self.us_state_codes:
                    state = p_state.upper()
                    country = "US"

        # --- Case B: 2 parts (e.g. ["San Francisco", "CA"] or ["London", "United Kingdom"]) ---
        elif len(parts) == 2:
            p0, p1 = parts[0], parts[1]
            # Strip zip codes appended to state (e.g. "CA 94105" -> "CA")
            p1_clean = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", p1).strip()
            p1_lower = p1_clean.lower()
            p1_upper = p1_clean.upper()

            if p1_upper in self.us_state_codes:
                state = p1_upper
                city = p0
                country = "US"
            elif p1_lower in self.us_states:
                state = self.us_states[p1_lower]
                city = p0
                country = "US"
            elif p1_lower in self.country_map:
                country = self.country_map[p1_lower]
                city = p0
            else:
                city = p0
                if forced_us:
                    country = "US"

        # --- Case C: 1 part (e.g. ["San Francisco CA"] or ["Chicago"] or ["California"]) ---
        elif len(parts) == 1:
            p0 = parts[0]
            p0_lower = p0.lower()
            p0_upper = p0.upper()

            # Known standalone tech hub first (e.g. "San Francisco", "New York", "Seattle")
            if p0_lower in self.known_cities:
                c_name, s_code = self.known_cities[p0_lower]
                city = c_name
                state = s_code
                country = "US"
            # Standalone state name (e.g. "California", "Texas")
            elif p0_lower in self.us_states:
                state = self.us_states[p0_lower]
                city = None
                country = "US"
            # Standalone 2-letter state code (e.g. "CA", "TX")
            elif p0_upper in self.us_state_codes:
                state = p0_upper
                city = None
                country = "US"
            else:
                # Check "City ST" regex without comma (e.g. "San Francisco CA", "Austin TX")
                m_city_st = re.match(r"^([A-Za-z\s.]+?)\s+([A-Z]{2})$", p0)
                if m_city_st and m_city_st.group(2) in self.us_state_codes:
                    city = m_city_st.group(1).strip()
                    state = m_city_st.group(2)
                    country = "US"
                else:
                    city = p0
                    if forced_us:
                        country = "US"

        # Canonicalize city name and deduce state if known tech hub
        if city:
            c_key = city.lower().strip()
            if c_key in self.known_cities:
                c_name, s_code = self.known_cities[c_key]
                city = c_name
                if not state:
                    state = s_code
                if not country:
                    country = "US"
            else:
                # Title-case generic city names if all lowercase
                if city.islower():
                    city = city.title()

        return {
            "city": city,
            "state": state,
            "country": country,
            "is_remote": is_remote,
            "raw": raw,
        }

    @classmethod
    def normalize(cls, location_str: Optional[str]) -> Dict[str, Any]:
        """Classmethod helper for one-off normalization without explicit instantiation."""
        return cls().normalize_location(location_str)


# ==============================================================================
# Module-Level Convenience Functions
# ==============================================================================

_default_company_normalizer = CompanyNormalizer()
_default_location_normalizer = LocationNormalizer()


def normalize_company(name: Optional[str], default: str = "") -> str:
    """Convenience module function to normalize a company name."""
    return _default_company_normalizer.normalize_company(name, default=default)


def normalize_location(location_str: Optional[str]) -> Dict[str, Any]:
    """Convenience module function to normalize a location string."""
    return _default_location_normalizer.normalize_location(location_str)
