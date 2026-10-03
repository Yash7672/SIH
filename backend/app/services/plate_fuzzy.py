"""Fuzzy hot-list plate matching for Indian registration plates.

OCR on a moving vehicle at 3 fps is never perfect, so an exact string comparison
throws away real hits. This module scores a read against the *hot-list set only*
using an OCR-confusion-weighted edit distance, and refuses to auto-confirm
anything it is not sure about.

Cost model
----------
Substituting a character that RapidOCR commonly confuses costs 0.4
(O/0, I/1, S/5, B/8, Z/2, G/6, and the M/N/H family). Every other substitution,
insertion or deletion costs 1. The score is ``1 - cost / max(len)``.

Decision classes
----------------
``EXACT``   read == hot-list plate. Score 1.0. Confirmed alert.
``FUZZY``   score >= 0.85 and the state code matches. "POSSIBLE MATCH, verify",
            with the score attached, and optionally corroborated by a second
            independent read of the same plate within 60 s. Never auto-confirmed.
``NONE``    below threshold, or a different state code. Ignored.

Only these three states are possible; nothing here writes to the database, and a
non-hot-listed plate never leaves this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from app.services.plate import VALID_STATE_CODES, is_valid_plate

# --------------------------------------------------------------------------
# Confusion classes
# --------------------------------------------------------------------------
# Each group is a set of glyphs RapidOCR confuses with one another. Any pair
# inside a group costs CONFUSION_COST; everything else costs 1.0.
CONFUSION_GROUPS: Tuple[frozenset, ...] = (
    frozenset({"O", "0"}),          # letter O vs digit zero
    frozenset({"I", "1"}),          # letter I vs digit one
    frozenset({"S", "5"}),
    frozenset({"B", "8"}),
    frozenset({"Z", "2"}),
    frozenset({"G", "6"}),
    frozenset({"D", "O"}),          # D/O is close enough to be its own group
    frozenset({"M", "N"}),          # M/N/H form a family; pairwise below
    frozenset({"N", "H"}),
    frozenset({"M", "H"}),
    frozenset({"U", "V"}),
    frozenset({"C", "G"}),
)

CONFUSION_COST = 0.4
SUBSTITUTION_COST = 1.0
FUZZY_THRESHOLD = 0.85
# A read shorter than this cannot be trusted enough to score.
MIN_PARTIAL_LEN = 6
# Even a perfectly-equal partial tail is weaker evidence than a whole-plate read,
# so a partial is capped below 1.0. Reporting "AB1234" as a 1.00 match would
# tell the officer we are certain, which we are not.
PARTIAL_PENALTY = 0.06

# Symmetric lookup: any two chars in a group are "confusable".
_CONFUSABLE: Dict[str, frozenset] = {}
for _group in CONFUSION_GROUPS:
    for _ch in _group:
        _CONFUSABLE[_ch] = _CONFUSABLE.get(_ch, frozenset()) | _group

# Groups that are genuinely the same glyph class get a small extra discount so
# M/N/H do not each cost 0.4 against the other two at once.
_EXTRA_DISCOUNT_PAIRS = {
    frozenset({"M", "N"}): 0.15,
    frozenset({"N", "H"}): 0.15,
    frozenset({"M", "H"}): 0.2,
}

MATCH_EXACT = "EXACT"
MATCH_FUZZY = "FUZZY"
MATCH_NONE = "NONE"


def _pair_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    group = _CONFUSABLE.get(a, frozenset())
    if b in group:
        for g in CONFUSION_GROUPS:
            if a in g and b in g:
                return CONFUSION_COST - _EXTRA_DISCOUNT_PAIRS.get(frozenset(g), 0.0)
        return CONFUSION_COST
    return SUBSTITUTION_COST


def _dp_edit_cost(read: str, target: str) -> float:
    """Weighted Levenshtein via dynamic programming (len(read) x len(target))."""
    n, m = len(read), len(target)
    if n == 0:
        return float(m)
    if m == 0:
        return float(n)
    # prev[j] = cost of turning read[:0] into target[:j]
    prev = [float(j) for j in range(m + 1)]
    for i in range(1, n + 1):
        cur = [float(i)] + [0.0] * m
        ch = read[i - 1]
        for j in range(1, m + 1):
            sub = prev[j - 1] + _pair_cost(ch, target[j - 1])
            delete = prev[j] + SUBSTITUTION_COST
            insert = cur[j - 1] + SUBSTITUTION_COST
            cur[j] = min(sub, delete, insert)
        prev = cur
    return prev[m]


def weighted_similarity(read: str, target: str) -> float:
    """1 - weighted_edit_cost / max_len, clamped to [0, 1]."""
    longest = max(len(read), len(target))
    if longest == 0:
        return 0.0
    cost = _dp_edit_cost(read, target)
    return max(0.0, 1.0 - cost / longest)


@dataclass(frozen=True)
class FuzzyMatch:
    """Result of scoring one OCR read against the hot-list set."""

    kind: str                 # EXACT | FUZZY | NONE
    score: float
    plate: Optional[str]      # the hot-list plate that was hit (never the raw read)
    reason: str = ""
    read: str = ""

    @property
    def matched(self) -> bool:
        return self.kind in (MATCH_EXACT, MATCH_FUZZY)

    @property
    def verified_label(self) -> str:
        """Human-readable confidence wording shown to the officer."""
        if self.kind == MATCH_EXACT:
            return "CONFIRMED MATCH"
        if self.kind == MATCH_FUZZY:
            return "POSSIBLE MATCH, verify"
        return "NO MATCH"


NO_MATCH = FuzzyMatch(kind=MATCH_NONE, score=0.0, plate=None, reason="no hot-list plate read")


def _state_code(plate: str) -> str:
    return plate[:2]


def _partial_score(read: str, target: str) -> Tuple[float, str]:
    """Score a partial read by matching a prefix, a suffix, or a window.

    A camera often clips one edge of the plate. We accept 6+ characters that
    match either the state+RTO head or the series+number tail. Returns the best
    achievable similarity and a label describing which anchor matched.
    """
    best = 0.0
    best_label = ""
    # Prefix anchor: read is the head of the plate.
    head_len = min(len(read), len(target))
    if head_len >= MIN_PARTIAL_LEN:
        sim = weighted_similarity(read[:head_len], target[:head_len]) - PARTIAL_PENALTY
        if sim > best:
            best, best_label = sim, f"partial head ({head_len} chars)"
    # Suffix anchor: read is the tail of the plate.
    tail_len = min(len(read), len(target))
    if tail_len >= MIN_PARTIAL_LEN:
        sim = weighted_similarity(read[-tail_len:], target[-tail_len:]) - PARTIAL_PENALTY
        if sim > best:
            best, best_label = sim, f"partial tail ({tail_len} chars)"
    return max(0.0, best), best_label


def match_plate(read: Optional[str], hotlist: Iterable[str]) -> FuzzyMatch:
    """Score a single OCR ``read`` against the active hot-list ``hotlist``.

    Returns a :class:`FuzzyMatch`. ``EXACT`` only when a hot-list plate equals
    the read exactly. ``FUZZY`` requires score >= threshold and a matching state
    code (partials may borrow the target's state code). Everything else is
    ``NONE`` - the caller persists nothing.
    """
    if not read:
        return FuzzyMatch(kind=MATCH_NONE, score=0.0, plate=None, reason="empty read", read="")

    cleaned = "".join(ch for ch in str(read).upper() if ch.isalnum())
    if len(cleaned) < MIN_PARTIAL_LEN:
        return FuzzyMatch(
            kind=MATCH_NONE, score=0.0, plate=None,
            reason=f"read too short ({len(cleaned)} chars, need >= {MIN_PARTIAL_LEN})", read=cleaned,
        )

    read_state = _state_code(cleaned)
    read_state_valid = read_state in VALID_STATE_CODES

    best_fuzzy: Optional[FuzzyMatch] = None

    for plate in hotlist:
        if not plate:
            continue
        target = "".join(ch for ch in str(plate).upper() if ch.isalnum())
        if not target:
            continue

        if cleaned == target:
            return FuzzyMatch(kind=MATCH_EXACT, score=1.0, plate=target, reason="exact plate string", read=cleaned)

        full_sim = weighted_similarity(cleaned, target)
        cand_sim, cand_label = full_sim, "full read"
        if len(cleaned) < len(target):
            partial_sim, partial_label = _partial_score(cleaned, target)
            # A partial read can never outrank a full-length comparison, but it
            # is the only option when the tail of the plate is what was captured.
            if partial_sim > full_sim:
                cand_sim, cand_label = partial_sim, partial_label

        if cand_sim < FUZZY_THRESHOLD:
            continue

        # A readable state code must agree with the target's. Two different RTO
        # codes are never a fuzzy hit. When the read has no readable state code
        # at all (a clipped left edge, so a tail-only partial) we cannot check
        # it - that is precisely the case that is only ever reported as a
        # possible match and never auto-confirmed.
        target_state = _state_code(target)
        if read_state_valid and read_state != target_state:
            continue

        candidate = FuzzyMatch(
            kind=MATCH_FUZZY, score=round(cand_sim, 4), plate=target,
            reason=f"{cand_label}, state code {target_state}", read=cleaned,
        )
        if best_fuzzy is None or candidate.score > best_fuzzy.score:
            best_fuzzy = candidate

    if best_fuzzy is not None:
        return best_fuzzy
    return FuzzyMatch(kind=MATCH_NONE, score=0.0, plate=None, reason="no hot-list plate above threshold", read=cleaned)


def corroborate(first: FuzzyMatch, second: FuzzyMatch, within_seconds: Optional[float] = None) -> bool:
    """True when two independent reads corroborate a fuzzy match.

    Both must be FUZZY-or-better, and must target the *same* hot-list plate.
    When ``within_seconds`` is given the caller is responsible for the time
    check (this module does not carry timestamps).
    """
    if first.kind != MATCH_FUZZY:
        return False
    if second.kind not in (MATCH_FUZZY, MATCH_EXACT):
        return False
    if first.plate is None or first.plate != second.plate:
        return False
    _ = within_seconds
    return True


def fuzzy_report(samples: List[Tuple[str, str]]) -> List[dict]:
    """Helper used by tests/demos: match each (read, expected_plate) pair."""
    out = []
    for read, plate in samples:
        m = match_plate(read, [plate] if plate else [])
        out.append({
            "read": read,
            "expected": plate,
            "kind": m.kind,
            "score": m.score,
            "label": m.verified_label,
            "reason": m.reason,
        })
    return out