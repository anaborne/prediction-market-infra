"""Deciding when two venues' markets are the same tradeable claim.

This is the part that decides whether anything downstream means anything. An arbitrage between
two markets that are *nearly* the same is an unhedged directional
position taken by accident, at a size chosen on the belief that it was riskless. So this module
is built to say "no" cheaply and "yes" only on evidence, and to record why either way.

Matching runs in three stages, and they answer different questions:

1. Candidate generation, "which markets could possibly be about this?" A full cross is
about a billion pairs (95,206 Kalshi × 10,656 Polymarket US on the last whole-venue scan) and
almost all of them are absurd on their face. An inverted index over IDF-weighted tokens narrows
each Kalshi market to a handful of candidates that share rare wording with it. `"spanberger"` is
nearly conclusive; `"bitcoin"` is not, and IDF is what tells them apart.

2. Scoring, "how alike are they?" A weighted blend of lexical overlap, entity agreement, and
time proximity, in `[0, 1]`. This stage is fuzzy on purpose, and its output is never sufficient
on its own.

3. The settlement gate, "would they pay out identically?" This stage is not fuzzy. It
compares structured facts (comparator and threshold, units, scheduled event time, resolution
source) and any check that fails or cannot be performed becomes a *blocker*. A pair with
blockers can be `STRONG` (worth a human read) but never `IDENTICAL`, and only `IDENTICAL` may
carry a claimed arbitrage. This is the lesson of the falsified directional strategy applied to a
new one: the failure mode that costs money is a model that is confident where it has no
information, so the default is to withhold confidence rather than to fall back on the fuzzy
score.

Two details worth knowing before changing anything here:

- Which outcome corresponds to Kalshi's YES is resolved, never assumed. Polymarket's
  `outcomes[0]` is `"Yes"` on a proposition market and `"Tampa Bay Rays"` on a head-to-head. A
  matcher that assumed index 0 meant YES would invert half the sports book and report the
  inverted price as edge.
- `can_close_early` is recorded, not gated on. Polymarket resolves through UMA, which can
  settle before `endDate`, so every Polymarket market would fail an early-close equality check and
  nothing would ever be `IDENTICAL`. A gate that rejects everything is indistinguishable from a
  gate that is broken, so this one is an annotation on the pair instead.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from kalshi_bot.crossvenue.models import (
    Comparator,
    MatchConfidence,
    MatchedPair,
    NormalizedMarket,
)
from kalshi_bot.crossvenue.text import (
    inverse_document_frequency,
    teams_match,
    thresholds_equivalent,
    tokenize,
    weighted_overlap,
)

# Tokens rarer than this share of the corpus are used to seed candidate lookup. A token in half
# the markets retrieves half the corpus and is worthless as a key.
_MAX_INDEX_DOCUMENT_FRACTION: Final = 0.08

# How many of a market's rarest tokens seed its candidate lookup.
_SEED_TOKENS: Final = 6

# Wording that marks a market as a two-sided proposition rather than a head-to-head, so
# `outcomes[0] == "yes"` can be trusted to mean the affirmative.
_AFFIRMATIVE_LABELS: Final[frozenset[str]] = frozenset({"yes", "over", "true"})
_NEGATIVE_LABELS: Final[frozenset[str]] = frozenset({"no", "under", "false"})

# Tokens that change *which* claim is being made rather than how it is worded. When one venue's
# wording carries one of these and the other's does not, the two are different contracts however
# well the rest of the text overlaps, and the rest overlaps almost perfectly, which is exactly
# what makes this dangerous.
#
# The case that put this here, observed on a live scan: Kalshi's `KXVPRESNOMD-28-JPOL`
# ("2028 Democratic VP nominee", yes label "Jared Polis") scored 0.44 against Polymarket's "Will
# Jared Polis win the 2028 Democratic presidential nomination?", a different office, matching on
# every other word. Lexical scoring separated them by seven points, which is not a margin worth
# trusting. This rejects them outright.
# Sports whose contests can end without either named competitor winning. In these, quoting two
# competitors as the two outcomes leaves the draw unpriced, so the pair is not a complement pair.
#
# `kbo` and `npb` were added 2026-08-22 after the first Polymarket US scan: all 33 pairs it
# passed as `IDENTICAL` were Korean KBO or Japanese NPB baseball, and both leagues end a
# regular-season game in a tie once extra innings are exhausted. Kalshi words each side as "If
# {team} wins ... resolves to Yes", so a tie resolves NO on both of its markets, which means
# a tie does not merely leave the hedge unbalanced, it makes both legs pay zero. That is worse
# than the soccer case this constant was built for, and it was invisible because the check is
# wording-based and neither venue writes the word "draw" anywhere in a baseball game's rules.
#
# The general gap this exposes is recorded in `docs/GUIDE.md`: whether a competition can draw
# is a fact about the sport, not a string in the payload, and a token list can only ever catch the
# leagues someone has thought of. It is kept as a token list because being wrong in this direction
# costs a missed match and being wrong in the other direction costs the whole position.
_DRAW_CAPABLE_SPORTS: Final[frozenset[str]] = frozenset(
    {"soccer", "cricket", "chess", "football club", "test match", "draw", "kbo", "npb"}
)

_QUALIFIER_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "vp",
        "vice",
        "deputy",
        "runner",
        "loser",
        "not",
        "without",
        "excluding",
        "runoff",
        "consecutive",
        "combined",
        # Rank and superlative words. Kalshi's "#2 US Netflix Movie this week?" priced a 74c
        # "arbitrage" against Polymarket's "be the top US Netflix movie" for the same film: same
        # title, same week, different rank. Rejecting on a one-sided rank word costs some true
        # matches where the two venues word the same rank differently ("#1" against "top"), and
        # that is the right direction to err for a gate whose false positives become unhedged
        # positions.
        "top",
        "second",
        "third",
        "highest",
        "lowest",
        # Which sub-contest is being settled. Kalshi quotes "Will Chinggis Warriors win map 1 in
        # the ... match?" alongside a whole-match market; Polymarket quotes the whole match as
        # `moneyline` and each game as `child_moneyline`. Matching a map winner to a match
        # winner produced eight of the twenty "opportunities" on a live scan.
        "map",
        # Geographic scope. Kalshi's "Top Global Netflix Movie this week?" and Polymarket's "be
        # the top US Netflix movie" name the same film in the same week and settle off different
        # charts. "us" is deliberately *not* in this set: it appears in a large share of Kalshi's
        # wording and one-sided occurrences would reject many genuine matches, so the asymmetry
        # is accepted, since "global" catches this pair from the other side.
        "global",
        "worldwide",
    }
)

# Weights on the three scoring signals. They sum to 1.0, so a perfect match scores 1.0 and the
# thresholds in `MatcherConfig` mean what they look like they mean.
_LEXICAL_WEIGHT: Final = 0.45
_TIME_WEIGHT: Final = 0.20
_LABEL_WEIGHT: Final = 0.35


@dataclass(frozen=True, slots=True)
class MatcherConfig:
    """Thresholds governing how readily two markets are called the same.

    Attributes:
        min_score: Below this lexical/structural score a candidate is discarded entirely rather
            than recorded as `WEAK`. Keeps the dataset from filling with noise.
        strong_score: The score a pair must reach before the settlement gate is even consulted.
        scheduled_time_tolerance_seconds: How far two venues' stated start times for the same
            scheduled event may differ. Venues round differently and occasionally disagree by a
            few minutes on a first pitch; they never disagree by an hour about the same game.
        expiry_tolerance_seconds: How far two venues' settlement times may differ when there is
            no scheduled event time to compare. Deliberately loose, because the venues genuinely
            mean different things by their end dates, and a same-day agreement is the real signal.
        max_candidates_per_market: Cap on candidates scored per Kalshi market.
    """

    min_score: float = 0.42
    strong_score: float = 0.60
    scheduled_time_tolerance_seconds: int = 1800
    expiry_tolerance_seconds: int = 86_400
    max_candidates_per_market: int = 10


@dataclass(frozen=True, slots=True)
class MatchStats:
    """What one `match_all` call looked at, and what it managed not to look at.

    The narrowing *is* the design here. A full venue cross is a pair count with nine zeroes on
    it, and the reason this module is tractable is that the inverted index never scores almost
    any of them. A design whose whole justification is a reduction factor should report that
    factor rather than assert it, so `match_all` records one of these on the matcher and a
    benchmark (or a caller wanting the number in a log line) can read it back.

    `tokens_dropped_as_too_common` is the one worth watching over time. A token appearing in more
    than `_MAX_INDEX_DOCUMENT_FRACTION` of the corpus retrieves a slice of the corpus and narrows
    nothing, so it is left out of the index entirely; if that count ever collapses toward zero,
    the corpus has changed shape and candidate generation is quietly doing more work per market
    than it was designed to.

    Attributes:
        kalshi_markets: Size of the Kalshi side.
        other_markets: Size of the counterparty side.
        total_pairs: The full cross, every pair that exists, scored or not.
        indexed_tokens: Distinct tokens the inverted index actually holds.
        tokens_dropped_as_too_common: Tokens seen but excluded for appearing in too much of the
            corpus to discriminate.
        candidates_scored: Pairs that reached the scorer. The rest were never looked at.
    """

    kalshi_markets: int
    other_markets: int
    total_pairs: int
    indexed_tokens: int
    tokens_dropped_as_too_common: int
    candidates_scored: int

    @property
    def reduction_factor(self) -> float:
        """How many times smaller the scored set is than the full cross.

        Returns:
            `total_pairs / candidates_scored`, or `inf` when nothing was scored at all, which is
            a real outcome (two corpora sharing no rare wording) and not a division to guard by
            returning zero, because zero would read as "no reduction".
        """
        if self.candidates_scored == 0:
            return float("inf")
        return self.total_pairs / self.candidates_scored


@dataclass(frozen=True, slots=True)
class _Indexed:
    """A market with its tokens precomputed, so scoring does not re-tokenize per comparison."""

    market: NormalizedMarket
    tokens: tuple[str, ...]


class MarketMatcher:
    """Matches Kalshi markets against another venue's markets.

    Attributes:
        config: The thresholds this matcher applies.
        stats: What the most recent `match_all` call looked at, or `None` before the first call.
    """

    def __init__(self, config: MatcherConfig | None = None) -> None:
        """Build a matcher.

        Args:
            config: Thresholds to apply. Defaults are tuned to be conservative: they under-report
                matches rather than over-report them, which is the correct direction for a
                component whose false positives become unhedged positions.
        """
        self.config = config or MatcherConfig()
        self.stats: MatchStats | None = None

    def match_all(
        self,
        kalshi_markets: Sequence[NormalizedMarket],
        other_markets: Sequence[NormalizedMarket],
    ) -> list[MatchedPair]:
        """Match every Kalshi market against the other venue's markets.

        Args:
            kalshi_markets: Normalized Kalshi markets.
            other_markets: Normalized markets from the other venue.

        Returns:
            Every pair scoring at least `config.min_score`, including rejected ones, since the
            rejections carry their reasons and are the signal for improving this matcher. Sorted
            by confidence then score, strongest first.
        """
        kalshi_indexed = [
            _Indexed(market, tokenize(market.search_text)) for market in kalshi_markets
        ]
        other_indexed = [_Indexed(market, tokenize(market.search_text)) for market in other_markets]
        if not kalshi_indexed or not other_indexed:
            self.stats = MatchStats(
                kalshi_markets=len(kalshi_indexed),
                other_markets=len(other_indexed),
                total_pairs=0,
                indexed_tokens=0,
                tokens_dropped_as_too_common=0,
                candidates_scored=0,
            )
            return []

        weights = inverse_document_frequency(
            [entry.tokens for entry in kalshi_indexed] + [entry.tokens for entry in other_indexed]
        )
        index, dropped_tokens = self._build_index(other_indexed)

        pairs: list[MatchedPair] = []
        candidates_scored = 0
        for entry in kalshi_indexed:
            for candidate in self._candidates(entry, index, weights):
                candidates_scored += 1
                pair = self._evaluate(entry, candidate, weights)
                if pair is not None:
                    pairs.append(pair)

        self.stats = MatchStats(
            kalshi_markets=len(kalshi_indexed),
            other_markets=len(other_indexed),
            total_pairs=len(kalshi_indexed) * len(other_indexed),
            indexed_tokens=len(index),
            tokens_dropped_as_too_common=dropped_tokens,
            candidates_scored=candidates_scored,
        )

        confidence_rank = {
            MatchConfidence.IDENTICAL: 0,
            MatchConfidence.STRONG: 1,
            MatchConfidence.WEAK: 2,
            MatchConfidence.REJECTED: 3,
        }
        pairs.sort(key=lambda pair: (confidence_rank[pair.confidence], -pair.score))
        return pairs

    def _build_index(self, entries: Sequence[_Indexed]) -> tuple[dict[str, list[_Indexed]], int]:
        """Build an inverted token index, omitting tokens too common to narrow anything.

        Args:
            entries: The markets to index.

        Returns:
            A token-to-markets mapping, and how many tokens were dropped for being too common to
            discriminate.
        """
        postings: dict[str, list[_Indexed]] = defaultdict(list)
        for entry in entries:
            for token in set(entry.tokens):
                postings[token].append(entry)
        ceiling = max(1, int(len(entries) * _MAX_INDEX_DOCUMENT_FRACTION))
        index = {token: markets for token, markets in postings.items() if len(markets) <= ceiling}
        return index, len(postings) - len(index)

    def _candidates(
        self,
        entry: _Indexed,
        index: dict[str, list[_Indexed]],
        weights: dict[str, float],
    ) -> list[_Indexed]:
        """Retrieve the markets most likely to be about the same thing as `entry`.

        Args:
            entry: The Kalshi market being matched.
            index: The inverted token index over the other venue.
            weights: IDF weights.

        Returns:
            Up to `config.max_candidates_per_market` candidates, ordered by how much rare wording
            they share with `entry`.
        """
        seeds = sorted(set(entry.tokens), key=lambda token: -weights.get(token, 1.0))[:_SEED_TOKENS]
        hits: dict[str, tuple[_Indexed, float]] = {}
        for token in seeds:
            weight = weights.get(token, 1.0)
            for candidate in index.get(token, ()):
                key = candidate.market.market_id
                existing = hits.get(key)
                accumulated = (existing[1] if existing else 0.0) + weight
                hits[key] = (candidate, accumulated)
        ranked = sorted(hits.values(), key=lambda item: -item[1])
        return [candidate for candidate, _ in ranked[: self.config.max_candidates_per_market]]

    def _evaluate(
        self, kalshi: _Indexed, other: _Indexed, weights: dict[str, float]
    ) -> MatchedPair | None:
        """Score one candidate pair and run it through the settlement gate.

        Args:
            kalshi: The Kalshi market.
            other: The candidate from the other venue.
            weights: IDF weights.

        Returns:
            The evaluated pair, or `None` if it scores below `config.min_score`.
        """
        evidence: list[str] = []
        lexical = weighted_overlap(kalshi.tokens, other.tokens, weights)
        evidence.append(f"lexical overlap {lexical:.3f}")

        aligned, alignment_evidence = _resolve_alignment(kalshi.market, other.market)
        if alignment_evidence:
            evidence.append(alignment_evidence)

        time_score, time_evidence = _time_agreement(kalshi.market, other.market, self.config)
        evidence.append(time_evidence)

        matched_by_team = aligned is not None and alignment_evidence.startswith("teams")
        label_score, label_evidence = _label_agreement(
            kalshi.market, other, matched_by_team=matched_by_team
        )
        evidence.append(label_evidence)

        score = _LEXICAL_WEIGHT * lexical + _TIME_WEIGHT * time_score + _LABEL_WEIGHT * label_score

        if score < self.config.min_score:
            return None

        conflict = _qualifier_conflict(
            tokenize(kalshi.market.claim_text), tokenize(other.market.claim_text)
        )
        if conflict is not None:
            return MatchedPair(
                kalshi=kalshi.market,
                other=other.market,
                aligned=aligned if aligned is not None else True,
                confidence=MatchConfidence.REJECTED,
                score=score,
                evidence=tuple(evidence),
                blockers=(
                    f"only one side's wording carries {conflict!r}, which changes which claim "
                    "is being made",
                ),
            )

        if aligned is None:
            return MatchedPair(
                kalshi=kalshi.market,
                other=other.market,
                aligned=True,
                confidence=MatchConfidence.REJECTED,
                score=score,
                evidence=tuple(evidence),
                blockers=("could not determine which outcome corresponds to Kalshi YES",),
            )

        if score < self.config.strong_score:
            return MatchedPair(
                kalshi=kalshi.market,
                other=other.market,
                aligned=aligned,
                confidence=MatchConfidence.WEAK,
                score=score,
                evidence=tuple(evidence),
                blockers=("score below the settlement gate's threshold",),
            )

        blockers, gate_evidence = _settlement_blockers(kalshi.market, other.market, self.config)
        evidence.extend(gate_evidence)
        confidence = MatchConfidence.IDENTICAL if not blockers else MatchConfidence.STRONG
        return MatchedPair(
            kalshi=kalshi.market,
            other=other.market,
            aligned=aligned,
            confidence=confidence,
            score=score,
            evidence=tuple(evidence),
            blockers=tuple(blockers),
        )


def _resolve_alignment(
    kalshi: NormalizedMarket, other: NormalizedMarket
) -> tuple[bool | None, str]:
    """Decide which of the other venue's outcomes corresponds to Kalshi's YES.

    Args:
        kalshi: The Kalshi market.
        other: The other venue's market.

    Returns:
        A `(aligned, evidence)` pair. `aligned` is `True` when Kalshi YES corresponds to the other
        venue's first outcome, `False` when it corresponds to the second, and `None` when the
        correspondence cannot be established, which is a rejection and never a coin flip.
    """
    yes_label = other.yes_label.strip().lower()
    no_label = other.no_label.strip().lower()

    if yes_label in _AFFIRMATIVE_LABELS and no_label in _NEGATIVE_LABELS:
        return True, f"proposition outcomes ({other.yes_label}/{other.no_label})"
    if yes_label in _NEGATIVE_LABELS and no_label in _AFFIRMATIVE_LABELS:
        return False, f"inverted proposition outcomes ({other.yes_label}/{other.no_label})"

    # Head-to-head: Kalshi names one competitor and the other venue names both.
    if teams_match(kalshi.yes_label, other.yes_label):
        if teams_match(kalshi.yes_label, other.no_label):
            return None, "teams ambiguous: Kalshi label matches both outcomes"
        return True, f"teams matched ({kalshi.yes_label} -> {other.yes_label})"
    if teams_match(kalshi.yes_label, other.no_label):
        return False, f"teams matched ({kalshi.yes_label} -> {other.no_label})"
    return None, ""


def _label_agreement(
    kalshi: NormalizedMarket, other: _Indexed, *, matched_by_team: bool
) -> tuple[float, str]:
    """Score whether the specific thing Kalshi's YES names appears in the other venue's wording.

    This is the signal that separates "both markets mention the 2028 Democratic nomination" from
    "both markets are about Jared Polis winning it". Lexical overlap alone dilutes a decisive
    proper noun among a dozen shared boilerplate tokens, which is why every genuinely correct
    proposition match in the first live scan scored around 0.52, below the gate, while sharing
    the candidate's full name with its counterpart.

    Args:
        kalshi: The Kalshi market.
        other: The other venue's indexed market.
        matched_by_team: Whether alignment was already established by team names, which is a
            stronger form of the same evidence.

    Returns:
        A `(score, evidence)` pair, score in `[0, 1]`.
    """  # noqa: DOC201
    if matched_by_team:
        return 1.0, "label agreement 1.000 (teams)"
    label_tokens = set(tokenize(kalshi.yes_label))
    if not label_tokens:
        return 0.0, "label agreement 0.000 (Kalshi states no distinguishing label)"
    # Scored against the counterpart's *own* question, never its full search text. The search
    # text includes the event title, which for a head-to-head names both competitors, so
    # Kalshi's "Australia" scored a perfect label match against Polymarket's "Will Bangladesh
    # win?" purely because the shared event title said "Bangladesh vs Australia". That pair
    # priced out at 87c of phantom edge on the first live scan.
    question_tokens = set(tokenize(f"{other.market.question} {other.market.yes_label}"))
    present = label_tokens & question_tokens
    score = len(present) / len(label_tokens)
    return score, f"label agreement {score:.3f} ({sorted(present)} of {sorted(label_tokens)})"


def _qualifier_conflict(kalshi_tokens: Sequence[str], other_tokens: Sequence[str]) -> str | None:
    """Find a meaning-changing qualifier present on exactly one side.

    Args:
        kalshi_tokens: Tokens of the Kalshi market's *claim* text, not its rules.
        other_tokens: Tokens of the other venue's claim text.

    Returns:
        The conflicting token, or `None` when the two sides agree on all of them.
    """
    kalshi_qualifiers = _QUALIFIER_TOKENS & set(kalshi_tokens)
    other_qualifiers = _QUALIFIER_TOKENS & set(other_tokens)
    difference = kalshi_qualifiers ^ other_qualifiers
    return sorted(difference)[0] if difference else None


def _time_agreement(
    kalshi: NormalizedMarket, other: NormalizedMarket, config: MatcherConfig
) -> tuple[float, str]:
    """Score how closely two markets' timing agrees.

    Args:
        kalshi: The Kalshi market.
        other: The other venue's market.
        config: Tolerances.

    Returns:
        A `(score, evidence)` pair, score in `[0, 1]`.
    """
    kalshi_start = kalshi.terms.scheduled_event_time
    other_start = other.terms.scheduled_event_time
    if kalshi_start and other_start:
        delta = abs((kalshi_start - other_start).total_seconds())
        if delta <= config.scheduled_time_tolerance_seconds:
            return 1.0, f"scheduled start agrees within {delta:.0f}s"
        return 0.0, f"scheduled start differs by {delta:.0f}s"

    kalshi_expiry = kalshi.terms.expiry_time
    other_expiry = other.terms.expiry_time
    if kalshi_expiry and other_expiry:
        delta = abs((kalshi_expiry - other_expiry).total_seconds())
        if delta <= config.expiry_tolerance_seconds:
            return 0.6, f"expiry within {delta / 3600:.1f}h (no scheduled start on either side)"
        return 0.0, f"expiry differs by {delta / 3600:.1f}h"
    return 0.3, "no comparable timestamps on both sides"


def _settlement_blockers(
    kalshi: NormalizedMarket, other: NormalizedMarket, config: MatcherConfig
) -> tuple[list[str], list[str]]:
    """Run the settlement gate: every reason these two might not pay out identically.

    Args:
        kalshi: The Kalshi market.
        other: The other venue's market.
        config: Tolerances.

    Returns:
        A `(blockers, evidence)` pair. An empty blocker list is the only path to `IDENTICAL`.
    """
    blockers: list[str] = []
    evidence: list[str] = []

    blockers.extend(_draw_blockers(kalshi, other, evidence))
    blockers.extend(_threshold_blockers(kalshi, other, evidence))
    blockers.extend(_timing_blockers(kalshi, other, config, evidence))
    blockers.extend(_source_blockers(kalshi, other, evidence))

    if other.terms.can_close_early and not kalshi.terms.can_close_early:
        # Recorded, deliberately not a blocker: UMA can settle any Polymarket market before its
        # end date, so gating on this would reject every pair and hide the ones that matter.
        evidence.append("note: other venue may settle early (UMA); Kalshi may not")
    return blockers, evidence


def _draw_blockers(
    kalshi: NormalizedMarket, other: NormalizedMarket, evidence: list[str]
) -> list[str]:
    """Refuse a pair whose two outcomes may not be exhaustive.

    The whole trade rests on one contract and its complement summing to exactly $1. Kalshi's
    YES/NO always does. A counterpart quoting two *competitors* as its outcomes only does where
    the contest cannot end in a draw, and the first live scan found precisely the failure this
    prevents: Kalshi's "Australia wins" against Polymarket's "Will Bangladesh win?" on a cricket
    test match that also had a separate "Will the match end in a draw?" market. Neither
    contract is the other's complement, and buying both is an unhedged position, not a lock.

    Args:
        kalshi: The Kalshi market.
        other: The other venue's market.
        evidence: Appended to with what was checked.

    Returns:
        Blockers found.
    """
    competitor_outcomes = other.yes_label.strip().lower() not in _AFFIRMATIVE_LABELS
    if not competitor_outcomes:
        return []
    wording = f"{kalshi.terms.raw_rules} {kalshi.question} {other.question}".lower()
    drawable = sorted(token for token in _DRAW_CAPABLE_SPORTS if token in wording)
    if drawable:
        return [
            f"outcome set may not be exhaustive: wording mentions {drawable[0]!r}, where a draw "
            "means the two named results are not complements"
        ]
    evidence.append("two-competitor outcomes with no draw-capable sport in the wording")
    return []


def _threshold_blockers(
    kalshi: NormalizedMarket, other: NormalizedMarket, evidence: list[str]
) -> list[str]:
    """Check that two markets make the same numeric claim, if either makes one.

    Args:
        kalshi: The Kalshi market.
        other: The other venue's market.
        evidence: Appended to with what was checked.

    Returns:
        Blockers found.
    """
    kalshi_terms, other_terms = kalshi.terms, other.terms
    kalshi_has = kalshi_terms.threshold is not None
    other_has = other_terms.threshold is not None

    if not kalshi_has and not other_has:
        evidence.append("neither side makes a numeric claim")
        return []
    if kalshi_has != other_has:
        return [
            "one side is a level market and the other is not "
            f"(kalshi={kalshi_terms.threshold!r}, other={other_terms.threshold!r})"
        ]
    if (
        kalshi_terms.threshold_units
        and other_terms.threshold_units
        and kalshi_terms.threshold_units != other_terms.threshold_units
    ):
        return [
            f"threshold units differ ({kalshi_terms.threshold_units} vs "
            f"{other_terms.threshold_units})"
        ]
    is_range = Comparator.BETWEEN in (kalshi_terms.comparator, other_terms.comparator)
    if is_range:
        return ["range markets are not compared: a one-sided venue cannot hedge a two-sided range"]
    if not thresholds_equivalent(
        kalshi_terms.comparator.value,
        kalshi_terms.threshold,
        other_terms.comparator.value,
        other_terms.threshold,
        tick=0.01,
    ):
        return [
            f"thresholds differ ({kalshi_terms.comparator.value} {kalshi_terms.threshold} vs "
            f"{other_terms.comparator.value} {other_terms.threshold})"
        ]
    evidence.append(
        f"thresholds equivalent ({kalshi_terms.comparator.value} {kalshi_terms.threshold})"
    )
    return []


def _timing_blockers(
    kalshi: NormalizedMarket,
    other: NormalizedMarket,
    config: MatcherConfig,
    evidence: list[str],
) -> list[str]:
    """Check that two markets settle on the same occurrence.

    Args:
        kalshi: The Kalshi market.
        other: The other venue's market.
        config: Tolerances.
        evidence: Appended to with what was checked.

    Returns:
        Blockers found.
    """
    kalshi_start = kalshi.terms.scheduled_event_time
    other_start = other.terms.scheduled_event_time
    if kalshi_start and other_start:
        delta = abs((kalshi_start - other_start).total_seconds())
        if delta > config.scheduled_time_tolerance_seconds:
            return [f"scheduled event times differ by {delta / 60:.0f} minutes"]
        evidence.append("same scheduled event time")
        return []

    if not _same_day(kalshi.terms.expiry_time, other.terms.expiry_time):
        return ["settlement dates could not be shown to be the same day"]
    evidence.append("settlement dates agree to the day")
    return []


def _source_blockers(
    kalshi: NormalizedMarket, other: NormalizedMarket, evidence: list[str]
) -> list[str]:
    """Check that both venues name a compatible settlement source.

    Kalshi publishes structured `settlement_sources` (`{"name": "MLB", "url":
    "https://www.mlb.com/"}`); Polymarket publishes a bare `resolutionSource` URL or nothing at
    all. Where both are present, a shared token identifies them as the same authority. Where the
    other venue publishes nothing, the check *cannot be performed*, which is a blocker and not a
    pass, because "no stated source" is exactly the case where two venues quietly settle on
    different numbers.

    Args:
        kalshi: The Kalshi market.
        other: The other venue's market.
        evidence: Appended to with what was checked.

    Returns:
        Blockers found.
    """
    kalshi_sources = kalshi.terms.resolution_sources
    other_sources = other.terms.resolution_sources
    if not kalshi_sources or not other_sources:
        return ["settlement source unverifiable: at least one venue states none"]

    kalshi_tokens = _source_tokens(kalshi_sources)
    other_tokens = _source_tokens(other_sources)
    shared = kalshi_tokens & other_tokens
    if not shared:
        return [
            f"settlement sources share no term (kalshi={sorted(kalshi_tokens)[:4]}, "
            f"other={sorted(other_tokens)[:4]})"
        ]
    evidence.append(f"settlement sources share {sorted(shared)[:3]}")
    return []


def _source_tokens(sources: Iterable[str]) -> set[str]:
    """Reduce settlement-source strings to comparable tokens.

    Args:
        sources: Source strings, possibly containing URLs.

    Returns:
        Tokens, with URL scaffolding (`https`, `www`, `com`) removed so that
        `"https://www.mlb.com/"` and `"MLB"` share the token `mlb`.
    """
    noise = {"https", "http", "www", "com", "org", "net", "official", "website"}
    tokens: set[str] = set()
    for source in sources:
        cleaned = source.replace("/", " ").replace(".", " ").replace(":", " ")
        tokens |= {token for token in tokenize(cleaned) if token not in noise and len(token) > 2}
    return tokens


def _same_day(left: datetime | None, right: datetime | None) -> bool:
    """Whether two settlement timestamps fall on the same UTC date.

    Args:
        left: One timestamp.
        right: The other.

    Returns:
        `False` if either is missing, since an unverifiable date is not a matching one.
    """
    if left is None or right is None:
        return False
    return left.date() == right.date()
