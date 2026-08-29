"""Wording normalization: turning two venues' prose into comparable facts.

The same contract is worded differently on every venue, and the differences are systematic rather
than random. Observed live 2026-08-22:

| the claim | Kalshi wording | Polymarket wording |
|---|---|---|
| White Sox beat the Mets | `"Chicago WS wins"` | outcome `"Chicago White Sox"` |
| Yankees beat the Astros | `"New York Y wins"` | outcome `"New York Yankees"` |
| BTC above a level | `floor_strike=86749.99`, `strike_type="greater"` | `"reach $100,000?"` |

Three mechanisms cover most of it, and each is a separate function here:

Team names are compositional, not arbitrary. Kalshi writes a city plus the initials of the
nickname when a city has two franchises (`Chicago WS`/`Chicago C`, `New York Y`/`New York M`,
`Los Angeles A`/`Los Angeles D`) and the bare city when it has one (`St. Louis`, `Texas`). That
is an algorithm: match on the city prefix, then disambiguate by checking the nickname's initials
against the suffix. `resolve_team_alias()` implements it, which is why this module carries a
five-row exception table rather than the ~124 rows a hand-maintained cross-league mapping would
need. Fewer rows is not the point; rows that cannot silently rot are.

Thresholds are numbers with comparators and units. `$100,000`, `100k`, `25 bps` and
`100000` are one value written four ways, and `>= 86750` and `> 86749.99` are one claim written
two ways. `extract_threshold()` returns both parts so `matching.py` can compare them as numbers.

Times are stated in prose on one side and as fields on the other. Kalshi puts a game's
scheduled start only in `rules_primary` ("originally scheduled for Aug 25, 2026 at 7:45 PM EDT");
Polymarket has a `gameStartTime` column. `parse_datetime_phrase()` recovers the first into the
shape of the second, because start time is the most discriminating single field for sports. Two
venues word a game unrecognizably and still agree on its start to the minute.

What this module deliberately does *not* do is decide whether two markets match. It produces
comparable facts; `matching.py` weighs them.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from functools import lru_cache
from typing import Final
from zoneinfo import ZoneInfo

# Wording that appears in nearly every market on one venue and never on the other, or in both so
# uniformly that it carries no signal. Removing it stops "will" and "market" from dominating a
# token overlap the way raw frequency otherwise lets them.
_BOILERPLATE: Final[frozenset[str]] = frozenset(
    {
        "will",
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "at",
        "to",
        "by",
        "for",
        "be",
        "is",
        "are",
        "this",
        "that",
        "market",
        "markets",
        "resolve",
        "resolves",
        "resolved",
        "resolution",
        "yes",
        "no",
        "then",
        "if",
        "and",
        "or",
        "vs",
        "versus",
        "game",
        "match",
        "professional",
        "originally",
        "scheduled",
    }
)

# Written forms that mean the same thing across venues. Applied on whole tokens after
# punctuation stripping, so "u.s." has already become "us" by the time this is consulted.
_SYNONYMS: Final[dict[str, str]] = {
    "btc": "bitcoin",
    "xbt": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "doge": "dogecoin",
    "fed": "federal reserve",
    "fomc": "federal reserve",
    "potus": "president",
    "gop": "republican",
    "dems": "democratic",
    "democrat": "democratic",
    "bps": "basis points",
    "bp": "basis points",
    "pct": "percent",
    "usa": "us",
    "united states": "us",
    "nyc": "new york city",
}

# Kalshi team labels whose relationship to the full name is not compositional. Every other team
# observed resolves through `resolve_team_alias()`'s city-plus-initials rule; these do not,
# because the franchise has no city in its common name or renamed itself.
_TEAM_EXCEPTIONS: Final[dict[str, str]] = {
    "a's": "athletics",
    "as": "athletics",
    "ath": "athletics",
    "oakland": "athletics",
    "washington f": "washington commanders",
}

# City words that are short enough to look like a nickname disambiguator but are not one.
_CITY_WORDS: Final[frozenset[str]] = frozenset({"bay", "city", "new", "san", "los", "las"})

_WORD_HYPHEN_RE: Final = re.compile(r"(?<=[a-z])-(?=[a-z])")
_WORD_PERIOD_RE: Final = re.compile(r"(?<![0-9])\.|\.(?![0-9])")
_PUNCT_RE: Final = re.compile(r"[^\w\s\.\$%\-\+]")
_WS_RE: Final = re.compile(r"\s+")

# "$100,000", "100k", "1.5M", "86749.99", "25%"
_NUMBER_RE: Final = re.compile(
    r"(?P<currency>\$)?\s*(?P<value>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    # The scale suffix must not be the first letter of a longer word: without the lookahead,
    # "25 bps" reads its "b" as "billion" and returns 25,000,000,000.
    r"\s*(?P<scale>[kKmMbB](?![a-zA-Z]))?\s*(?P<unit>%|bps|basis points|percent|dollars|usd)?"
)

_SCALES: Final[dict[str, float]] = {"k": 1e3, "m": 1e6, "b": 1e9}

# Comparator wording, longest first so "at least" wins over "least".
_COMPARATORS: Final[Sequence[tuple[str, str]]] = (
    ("greater than or equal to", "greater_equal"),
    ("less than or equal to", "less_equal"),
    ("at or above", "greater_equal"),
    ("at or below", "less_equal"),
    ("or above", "greater_equal"),
    ("or higher", "greater_equal"),
    ("or more", "greater_equal"),
    ("or below", "less_equal"),
    ("or lower", "less_equal"),
    ("or less", "less_equal"),
    ("greater than", "greater"),
    ("more than", "greater"),
    ("higher than", "greater"),
    ("at least", "greater_equal"),
    ("less than", "less"),
    ("lower than", "less"),
    ("fewer than", "less"),
    ("at most", "less_equal"),
    ("above", "greater"),
    ("below", "less"),
    ("exceed", "greater"),
    ("reach", "greater_equal"),
    ("hit", "greater_equal"),
    ("dip to", "less_equal"),
    (">=", "greater_equal"),
    ("<=", "less_equal"),
    (">", "greater"),
    ("<", "less"),
)

# US market timezones, by the abbreviation venues actually print. `ZoneInfo` resolves the DST
# offset from the date itself, so "EDT" and "EST" both map to the same zone without this module
# tracking transition dates.
_TZ_ABBREVIATIONS: Final[dict[str, str]] = {
    "ET": "America/New_York",
    "ET.": "America/New_York",
    "EDT": "America/New_York",
    "EST": "America/New_York",
    "CT": "America/Chicago",
    "CDT": "America/Chicago",
    "CST": "America/Chicago",
    "MT": "America/Denver",
    "MDT": "America/Denver",
    "MST": "America/Denver",
    "PT": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PST": "America/Los_Angeles",
    "UTC": "UTC",
    "GMT": "UTC",
    "Z": "UTC",
}

# "Aug 25, 2026 at 7:45 PM EDT" and the variants around it.
_DATETIME_RE: Final = re.compile(
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(?P<day>\d{1,2}),?\s+(?P<year>\d{4})"
    r"(?:\s*(?:at|,)?\s*(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>AM|PM|am|pm)?"
    r"\s*(?P<tz>[A-Z]{2,4})?)?",
    re.IGNORECASE,
)

_MONTHS: Final[dict[str, int]] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


@lru_cache(maxsize=1)
def _normalized_team_exceptions() -> dict[str, str]:
    """The exception table keyed by its own normalized form.

    `normalize_text` strips apostrophes, so a literal `"a's"` key would never be hit by a lookup
    of the normalized label `"a s"`. Normalizing the keys through the same function that
    normalizes the lookups is the only way the two stay in agreement as normalization changes.

    Returns:
        The exception table with normalized keys.
    """
    return {normalize_text(key): value for key, value in _TEAM_EXCEPTIONS.items()}


def strip_accents(text: str) -> str:
    """Fold accented characters to their ASCII base.

    Venues disagree on diacritics for the same name (`"KRÜ Esports"` / `"KRU Esports"`), and a
    codepoint difference must not become a matching difference.

    Args:
        text: Any string.

    Returns:
        `text` with combining marks removed.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_text(text: str) -> str:
    """Lowercase, de-accent, and strip punctuation that never carries meaning.

    Currency signs, decimal points, percent signs and hyphens survive, because `extract_threshold`
    runs on the output and `$86,750.00` must remain readable as a number.

    Args:
        text: Raw venue wording.

    Returns:
        A normalized single-spaced string.
    """
    folded = strip_accents(text).lower()
    folded = folded.replace("&", " and ").replace("/", " ")
    # Split hyphenated words ("runner-up" -> "runner up") but never a signed number: the hyphen
    # in a spread like "(-1.5)" is part of the value. Only a hyphen between two letters goes.
    folded = _WORD_HYPHEN_RE.sub(" ", folded)
    # Drop abbreviation periods ("St. Louis") while keeping decimal points ("86749.99").
    folded = _WORD_PERIOD_RE.sub(" ", folded)
    folded = _PUNCT_RE.sub(" ", folded)
    return _WS_RE.sub(" ", folded).strip()


def tokenize(text: str, *, drop_boilerplate: bool = True) -> tuple[str, ...]:
    """Split normalized wording into comparable tokens, expanding known synonyms.

    Args:
        text: Raw or normalized wording.
        drop_boilerplate: Whether to remove tokens that appear in nearly every market and so
            carry no discriminating signal.

    Returns:
        Tokens in order of appearance, with synonyms expanded and trailing possessives removed.
    """
    normalized = normalize_text(text)
    tokens: list[str] = []
    for raw in normalized.split():
        token = raw.strip(".-")
        if not token:
            continue
        expanded = _SYNONYMS.get(token, token)
        for part in expanded.split():
            if drop_boilerplate and part in _BOILERPLATE:
                continue
            tokens.append(part)
    return tuple(tokens)


def parse_number(text: str) -> float | None:
    """Read the first numeric quantity out of a string, honouring scale suffixes.

    Handles `$100,000`, `100k`, `1.5M`, `86749.99` and `25%` identically to the value each
    denotes.

    Args:
        text: Any string.

    Returns:
        The value as a float, or `None` if the string contains no number.
    """
    match = _NUMBER_RE.search(text)
    if match is None:
        return None
    value = float(match.group("value").replace(",", ""))
    scale = match.group("scale")
    if scale:
        value *= _SCALES[scale.lower()]
    return value


def extract_units(text: str) -> str | None:
    """Identify the unit a threshold is quoted in.

    Args:
        text: Any string.

    Returns:
        `"usd"`, `"percent"`, `"bps"`, or `None` when no unit is stated. Two markets quoting the
        same number in different units are not the same market, so an unstated unit is reported
        as unknown rather than guessed.
    """
    lowered = text.lower()
    if "bps" in lowered or "basis point" in lowered:
        return "bps"
    if "%" in lowered or "percent" in lowered:
        return "percent"
    if "$" in lowered or "usd" in lowered or "dollar" in lowered:
        return "usd"
    return None


def extract_comparator(text: str) -> str:
    """Identify how a threshold is compared to the settlement value.

    Args:
        text: Any string, typically a question or a strike subtitle.

    Returns:
        A `Comparator` value as a string, or `"none"` when the wording states no comparison.
        Matched longest-phrase-first so `"at least"` is not shadowed by `"least"`.
    """
    lowered = text.lower()
    for phrase, comparator in _COMPARATORS:
        if phrase in lowered:
            return comparator
    return "none"


def extract_threshold(text: str) -> tuple[str, float | None, str | None]:
    """Pull a market's numeric claim apart into comparator, level, and units.

    Args:
        text: A question or subtitle, e.g. `"Will Bitcoin reach $100,000 in August?"`.

    Returns:
        A `(comparator, threshold, units)` triple. Any element may be absent, so `("none", None,
        None)` for an outcome market with no numeric claim at all.
    """
    return extract_comparator(text), parse_number(text), extract_units(text)


def thresholds_equivalent(
    left_comparator: str,
    left_threshold: float | None,
    right_comparator: str,
    right_threshold: float | None,
    *,
    tick: float = 0.01,
) -> bool:
    """Whether two comparator/threshold pairs express the same claim.

    `> 86749.99` and `>= 86750` are the same contract when the underlying moves in cents, and
    Kalshi genuinely quotes the former where another venue quotes the latter. Comparing the
    numbers alone would call those different; comparing the strings alone would too.

    Args:
        left_comparator: The first market's comparator.
        left_threshold: The first market's level.
        right_comparator: The second market's comparator.
        right_threshold: The second market's level.
        tick: The smallest increment the underlying can move. Thresholds within one tick of each
            other are treated as the same boundary when the comparators differ only in strictness.

    Returns:
        `True` if the two express the same claim.
    """
    if left_threshold is None or right_threshold is None:
        return left_threshold is None and right_threshold is None
    same_direction = {
        "greater": "up",
        "greater_equal": "up",
        "less": "down",
        "less_equal": "down",
    }
    left_direction = same_direction.get(left_comparator)
    right_direction = same_direction.get(right_comparator)
    if left_direction is None or right_direction is None:
        return left_comparator == right_comparator and math.isclose(
            left_threshold, right_threshold, rel_tol=1e-9, abs_tol=tick / 2
        )
    if left_direction != right_direction:
        return False
    if math.isclose(left_threshold, right_threshold, rel_tol=1e-9, abs_tol=1e-9):
        return left_comparator == right_comparator
    # Strictness differs: `> x` equals `>= x + tick` only when the gap is exactly one tick.
    gap = right_threshold - left_threshold
    if left_direction == "up":
        if left_comparator == "greater" and right_comparator == "greater_equal":
            return math.isclose(gap, tick, rel_tol=1e-6, abs_tol=tick / 100)
        if left_comparator == "greater_equal" and right_comparator == "greater":
            return math.isclose(-gap, tick, rel_tol=1e-6, abs_tol=tick / 100)
        return False
    if left_comparator == "less" and right_comparator == "less_equal":
        return math.isclose(-gap, tick, rel_tol=1e-6, abs_tol=tick / 100)
    if left_comparator == "less_equal" and right_comparator == "less":
        return math.isclose(gap, tick, rel_tol=1e-6, abs_tol=tick / 100)
    return False


def parse_datetime_phrase(text: str) -> datetime | None:
    """Recover a UTC datetime from prose like `"Aug 25, 2026 at 7:45 PM EDT"`.

    Kalshi states a scheduled event's start time only inside `rules_primary`, while Polymarket
    publishes it as a column. Matching sports without this means matching on team names alone,
    which cannot distinguish the two games of a doubleheader or a rematch later in the week.

    Args:
        text: Prose that may contain a date, optionally with a time and timezone abbreviation.

    Returns:
        A timezone-aware UTC datetime, or `None` if no date is present. A date with no time
        returns midnight in the stated zone (UTC when none is stated), so callers must treat a
        missing time as low-precision rather than as a real instant.
    """
    match = _DATETIME_RE.search(text)
    if match is None:
        return None
    month = _MONTHS.get(match.group("month")[:3].lower())
    if month is None:
        return None

    hour = int(match.group("hour")) if match.group("hour") else 0
    minute = int(match.group("minute")) if match.group("minute") else 0
    meridiem = (match.group("meridiem") or "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not 0 <= hour <= 23:
        return None

    zone_name = _TZ_ABBREVIATIONS.get((match.group("tz") or "UTC").upper())
    zone = ZoneInfo(zone_name) if zone_name else UTC
    try:
        local = datetime(
            int(match.group("year")), month, int(match.group("day")), hour, minute, tzinfo=zone
        )
    except ValueError:
        return None
    return local.astimezone(UTC)


def resolve_team_alias(label: str) -> tuple[str, str | None]:
    """Split a venue's team label into a city and an optional nickname disambiguator.

    Kalshi writes the bare city when a city has one franchise (`"St. Louis"`, `"Texas"`) and
    appends the nickname's initials when it has two (`"Chicago WS"`, `"New York Y"`,
    `"Los Angeles A"`). Polymarket writes the full name. Rather than enumerate every franchise in
    every league, this returns the two parts and lets `teams_match()` test them against the full
    name, which is what makes the rule survive a franchise being added or renamed.

    Args:
        label: A team label from either venue.

    Returns:
        A `(city, initials)` pair, both lowercased, with `initials` `None` when the label carries
        no disambiguator. Names in the exception table return their canonical form as the city
        with no initials.
    """
    normalized = normalize_text(label)
    exception = _normalized_team_exceptions().get(normalized)
    if exception is not None:
        return exception, None
    parts = normalized.split()
    # A short trailing token is a disambiguator only if it is not itself a city word; "bay" in
    # "tampa bay" is three letters and is part of the city, not a nickname initial.
    if (
        len(parts) >= 2
        and len(parts[-1]) <= 3
        and parts[-1].isalpha()
        and parts[-1] not in _CITY_WORDS
    ):
        return " ".join(parts[:-1]), parts[-1]
    return normalized, None


def teams_match(kalshi_label: str, other_label: str) -> bool:
    """Whether two venues' team labels name the same franchise.

    Args:
        kalshi_label: Kalshi's label, e.g. `"Chicago WS"`.
        other_label: The other venue's label, e.g. `"Chicago White Sox"`.

    Returns:
        `True` when the city matches and, where Kalshi supplied a disambiguator, the remaining
        words of the other label begin with those letters in order, with `"WS"` selecting
        `"White Sox"` over `"Cubs"`.
    """
    city, initials = resolve_team_alias(kalshi_label)
    other = normalize_text(other_label)
    if city in _TEAM_EXCEPTIONS.values() and city in other:
        return True
    # Fall back to containment: "athletics" inside "oakland athletics".
    if not other.startswith(city) and city not in other:
        return False
    if initials is None:
        return True
    remainder = other[len(city) :].strip() if other.startswith(city) else other
    nickname_words = [word for word in remainder.split() if word]
    if not nickname_words:
        return False
    # Either the initials spell out the nickname's word-initials ("ws" -> "white sox"), or they
    # prefix its single word ("y" -> "yankees").
    word_initials = "".join(word[0] for word in nickname_words)
    return word_initials.startswith(initials) or nickname_words[0].startswith(initials)


def inverse_document_frequency(documents: Iterable[Sequence[str]]) -> dict[str, float]:
    """Weight tokens by how rare they are across a corpus of markets.

    Overlap on `"bitcoin"` means little when a thousand markets mention bitcoin; overlap on
    `"spanberger"` is nearly conclusive. Plain Jaccard cannot tell those apart, so candidate
    scoring weights each shared token by this.

    Args:
        documents: Token sequences, one per market.

    Returns:
        A token-to-weight mapping, `log(N / (1 + df))` floored at zero. Tokens absent from the
        mapping are unseen and should be treated as maximally rare by the caller.
    """
    document_count = 0
    frequencies: dict[str, int] = {}
    for document in documents:
        document_count += 1
        for token in set(document):
            frequencies[token] = frequencies.get(token, 0) + 1
    if document_count == 0:
        return {}
    return {
        token: max(0.0, math.log(document_count / (1 + frequency)))
        for token, frequency in frequencies.items()
    }


def weighted_overlap(
    left: Sequence[str], right: Sequence[str], weights: dict[str, float], *, default: float = 1.0
) -> float:
    """Score two token sequences by IDF-weighted Jaccard similarity.

    Args:
        left: One market's tokens.
        right: The other market's tokens.
        weights: Token weights from `inverse_document_frequency`.
        default: Weight for tokens absent from `weights`, which are unseen and therefore rare.

    Returns:
        A similarity in `[0, 1]`; `0.0` when either side has no tokens. Falls back to unweighted
        Jaccard when every shared token carries zero weight, which is the small-corpus case.
    """
    left_set, right_set = set(left), set(right)
    if not left_set or not right_set:
        return 0.0
    union = left_set | right_set
    intersection = left_set & right_set
    union_weight = sum(weights.get(token, default) for token in union)
    if union_weight <= 0.0:
        # Every token in the union is common enough that IDF floors it at zero, which happens
        # whenever the corpus is small or uniform. With two documents, `log(2 / 3)` is negative
        # for every shared token. Returning 0.0 there would report two identically worded
        # markets as completely dissimilar. Fall back to unweighted Jaccard, which is what IDF
        # reduces to when it has no frequency information to add.
        return len(intersection) / len(union)
    intersection_weight = sum(weights.get(token, default) for token in intersection)
    return intersection_weight / union_weight
