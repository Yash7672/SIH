"""Indian plate normalization, validation and OCR-repair rules.

An Indian registration plate is ``<RTO state code><1-2 digits><1-3 letters><1-4 digits>``.
The pattern alone is far too permissive (``HL09AB1234`` matches it even though no
Indian RTO uses "HL"), so every candidate is also validated against
:data:`VALID_STATE_CODES` — an unrecognised state code means the read is wrong,
not that the plate is rare.
"""

from __future__ import annotations

import re
import string
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

INDIAN_PLATE_PATTERN = re.compile(r"^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4}$")
DIGIT_TO_LETTER = {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "7": "T", "8": "B", "9": "G"}
LETTER_TO_DIGIT = {"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6", "T": "7"}

# 28 states + 8 union territories + the Bharat (BH) series.
VALID_STATE_CODES = frozenset(
    {
        "AN", "AP", "AR", "AS",
        "BR", "BH",
        "CH", "CG",
        "DD", "DL", "DN",
        "GA", "GJ",
        "HP", "HR",
        "JH", "JK",
        "KA", "KL",
        "LA", "LD",
        "MH", "ML", "MN", "MP", "MZ",
        "NL",
        "OD", "OR",
        "PB", "PY",
        "RJ",
        "SK",
        # TG is Telangana's legacy code, still in use on plates issued before 2014
        # alongside the current TS; both must be accepted.
        "TG",
        "TN", "TR", "TS",
        "UK", "UP",
        "WB",
    }
)

PLATE_MIN_LEN, PLATE_MAX_LEN = 8, 10

# Characters RapidOCR regularly swaps for one another on plates. Used to repair
# the state code only (position 0-1) and only when the original code is invalid.
VISUAL_SIMILARITY: Dict[str, Tuple[str, ...]] = {
    "A": ("4", "R"),
    "B": ("8", "R", "M"),
    "C": ("G", "O"),
    "D": ("O", "0"),
    "E": ("F", "L"),
    "F": ("E",),
    "G": ("6", "C"),
    "H": ("M", "N", "B", "R"),
    "I": ("L", "1", "T"),
    "J": ("T",),
    "K": ("X",),
    "L": ("I", "1", "E"),
    "M": ("H", "N", "W", "B"),
    "N": ("H", "M", "W"),
    "O": ("Q", "D", "0"),
    "P": ("R",),
    "Q": ("O",),
    "R": ("P", "B", "H"),
    "S": ("5", "8"),
    "T": ("I", "J", "7"),
    "U": ("V",),
    "V": ("U", "Y"),
    "W": ("M", "N"),
    "X": ("K",),
    "Y": ("V",),
    "Z": ("2",),
    "0": ("O", "D"),
    "1": ("I", "L"),
    "2": ("Z",),
    "5": ("S",),
    "6": ("G",),
    "7": ("T",),
    "8": ("B", "S"),
    "9": ("G",),
}

# Which letters a digit observed in the state-code slot could really be. Wider
# than DIGIT_TO_LETTER on purpose: a clipped glyph often reads as a digit.
STATE_DIGIT_LETTERS: Dict[str, Tuple[str, ...]] = {
    "0": ("O", "D", "Q"),
    "1": ("I", "L"),
    "2": ("Z",),
    "3": ("B", "E"),
    "4": ("A",),
    "5": ("S",),
    "6": ("G", "B"),
    "7": ("T", "F"),
    "8": ("B",),
    "9": ("G", "Q"),
}

# The blue "IND" band on BH/HSRP plates sits at the left edge of the crop and is
# picked up as its own OCR fragment; drop it before anything else.
STRIP_PREFIXES = ("INDIA", "IND")

# A repair that changes this many characters of the state code is not trusted.
MAX_STATE_REPAIRS = 2


@dataclass
class NormalizedPlate:
    raw: str
    normalized: str
    valid: bool
    reason: str = ""
    state_code_valid: bool = False
    repaired: bool = False
    candidates: List[str] = field(default_factory=list)

    @property
    def uncertain(self) -> bool:
        """True when we have a best guess we do not trust enough to report."""
        return not self.valid


def _normalize_segment(segment: str, *, expected: str) -> str:
    chars = list(segment)
    has_alpha = any(ch.isalpha() for ch in chars)
    for i, ch in enumerate(chars):
        if expected == "letters" and ch.isdigit():
            if not has_alpha:
                continue
            chars[i] = DIGIT_TO_LETTER.get(ch, ch)
        elif expected == "digits" and ch.isalpha():
            chars[i] = LETTER_TO_DIGIT.get(ch, ch)
    return "".join(chars)


def _segmentations(compact: str):
    """Yield ``(rto_len, series_len, trailing_len)`` splits for a plate length."""
    for rto_len in (1, 2):
        for series_len in (1, 2, 3):
            trailing_len = len(compact) - 2 - rto_len - series_len
            if trailing_len < 1 or trailing_len > 4:
                continue
            if 2 + rto_len + series_len + trailing_len != len(compact):
                continue
            yield rto_len, series_len, trailing_len


def _candidate_variants(cleaned: str) -> List[str]:
    if len(cleaned) < PLATE_MIN_LEN or len(cleaned) > PLATE_MAX_LEN:
        return []

    variants = {cleaned}
    for rto_len, series_len, _ in _segmentations(cleaned):
        state = _normalize_segment(cleaned[:2], expected="letters")
        rto = _normalize_segment(cleaned[2 : 2 + rto_len], expected="digits")
        series = _normalize_segment(cleaned[2 + rto_len : 2 + rto_len + series_len], expected="letters")
        trailing = _normalize_segment(cleaned[2 + rto_len + series_len :], expected="digits")
        candidate = f"{state}{rto}{series}{trailing}"
        if candidate:
            variants.add(candidate)
    return sorted(variants)


def pattern_ok(candidate: str) -> bool:
    return bool(INDIAN_PLATE_PATTERN.match(candidate))


def state_code_ok(candidate: str) -> bool:
    return candidate[:2] in VALID_STATE_CODES


def is_valid_plate(candidate: Optional[str]) -> bool:
    """A plate is valid only when the pattern *and* the RTO whitelist both pass."""
    return bool(candidate) and pattern_ok(candidate) and state_code_ok(candidate)


def _strip_blue_band(compact: str) -> Tuple[str, bool]:
    """Drop a leading ``IND``/``INDIA`` blue-strip fragment."""
    for prefix in STRIP_PREFIXES:
        if compact.startswith(prefix) and len(compact) - len(prefix) >= PLATE_MIN_LEN:
            return compact[len(prefix) :], True
    return compact, False


def _edit_count(candidate: str, cleaned: str) -> int:
    """Hamming distance, padded to the longer string (insertions count as 1)."""
    length = max(len(candidate), len(cleaned))
    padded_a = candidate.ljust(length)
    padded_b = cleaned.ljust(length)
    return sum(1 for a, b in zip(padded_a, padded_b) if a != b)


def _state_code_letters(ch: str) -> Tuple[str, ...]:
    """Every letter a single observed state-code character could really be.

    A digit is not just its DIGIT_TO_LETTER partner: on a clipped "H" only the
    right stem survives and RapidOCR reports "1", so "1" has to stay a candidate
    for I *and* L. Returning the full set is what lets the caller notice that
    "M1" could be MH or ML and refuse to guess.
    """
    if ch.isalpha():
        return tuple(dict.fromkeys((ch, *(c for c in VISUAL_SIMILARITY.get(ch, ()) if c.isalpha()))))
    return STATE_DIGIT_LETTERS.get(ch, (DIGIT_TO_LETTER.get(ch, ""),))


def _lost_leading_options(compact: str) -> List[Tuple[str, str]]:
    """Restored leading character: "H12AB1234" -> "MH12AB1234".

    Only usable when the string cannot be a plate at all without one more
    character, otherwise this would happily invent a state code.
    """
    return [(ch + compact, f"leading character lost, restored as '{ch}'") for ch in string.ascii_uppercase if ch.isalpha()]


# Indian plate layouts are not equally common. "MH 12 AB 1234" (2 RTO digits +
# 2 series letters) dominates; "DL 8 CBF 4890" (1 digit + 3 letters) is the older
# Delhi format and still legal. When a string parses several ways - "MH1ZAB1234"
# is either MH+1+ZAB+1234 or MH+12+AB+1234 - this prior picks the likelier one.
FORMAT_PRIOR: Dict[Tuple[int, int], float] = {
    (2, 2): 3.0,
    (1, 2): 2.0,
    (2, 1): 1.0,
    (1, 3): 0.5,
    (2, 3): 0.25,
    (1, 1): 0.0,
}


def _format_score(candidate: str) -> float:
    """How common the candidate's (RTO digits, series letters) layout is.

    Only layouts whose segments actually type-check count: "MH1ZAB1234" cannot be
    read as RTO "1Z" because "Z" is not a digit, so its only real layout is
    1 digit + 3 series letters - the rare old format, not the dominant one.
    """
    if not pattern_ok(candidate):
        return 0.0
    best = 0.0
    for rto_len, series_len, _ in _segmentations(candidate):
        rto = candidate[2 : 2 + rto_len]
        series = candidate[2 + rto_len : 2 + rto_len + series_len]
        if not rto.isdigit() or not series.isalpha():
            continue
        best = max(best, FORMAT_PRIOR.get((rto_len, series_len), 0.0))
    return best


def _edit_trust(edits: int) -> int:
    """How much evidence a candidate is allowed to override with layout prior.

    A single glyph confusion (Z read as 2, 1 read as I) is a well-known failure
    mode, and trusting a one-character repair when it produces a far more common
    layout is the whole point of `FORMAT_PRIOR` - it is what turns
    "MH1ZAB1234" into the "MH12AB1234" the car is actually carrying.

    Two characters is a different claim. "DL 1 ZA 9092" and "DL 12 AG 092" are
    *both* well-formed plates of *different real cars*, and the only thing
    separating them is a preference for common layouts. Letting that preference
    outrank two substitutions means a correct read gets silently rewritten into
    another vehicle, which is how a stolen car was reported as innocent - so a
    candidate that needs two or more changes can never outrank an exact one.
    """
    if edits == 0:
        return 2
    if edits == 1:
        return 1
    return 0


def _score_candidate(candidate: str, cleaned: str, ocr_score: float) -> Tuple:
    """Rank tuple; larger is better. Only pattern+whitelist-valid plates rank above 0.

    Order matters: how many characters had to change decides before how common
    the layout is. Ranking the layout prior first would let a nicer-looking
    guess overwrite an exact read.
    """
    edits = _edit_count(candidate, cleaned)
    return (
        1 if is_valid_plate(candidate) else 0,
        1 if PLATE_MIN_LEN <= len(candidate) <= PLATE_MAX_LEN else 0,
        _edit_trust(edits),
        _format_score(candidate),
        -edits,
        round(ocr_score, 4),
        -len(candidate),
    )


def _rank_candidates(
    candidates: Sequence[str],
    cleaned: str,
    ocr_score: float,
) -> Tuple[Optional[str], str]:
    """Pick the best plate from ``candidates`` and explain why in one line."""
    if not candidates:
        return None, "no plate-length candidate"

    scored = sorted(
        ((_score_candidate(c, cleaned, ocr_score), c) for c in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best = scored[0]

    if not best_score[0]:  # nothing matched pattern + whitelist
        runner_up = ", ".join(sorted({c for _, c in scored})[:4])
        return best, f"invalid: {best} fails pattern/whitelist (tried: {runner_up})"

    edits = -best_score[3]
    exact = best == cleaned
    if exact:
        return best, f"exact match, state code {best[:2]} is a valid RTO code"
    return best, f"repaired to {best} ({edits} char fix, state code {best[:2]} whitelisted)"


def normalize_plate(raw: str, ocr_score: float = 1.0) -> NormalizedPlate:
    """Normalize and validate an Indian-style plate string.

    - uppercases and strips spaces, hyphens, dots, underscores and punctuation
    - drops a leading blue ``IND`` strip fragment
    - applies OCR-safe positional corrections only inside their expected segment
    - repairs the state code by scored single-character substitutions or a restored
      leading character, but only when the result passes the RTO whitelist
    - preserves the original OCR string in ``raw`` and explains the decision in ``reason``
    """
    if raw is None:
        return NormalizedPlate(raw="", normalized="", valid=False, reason="empty input")

    compact = "".join(ch for ch in unicodedata.normalize("NFKC", str(raw)).upper() if ch.isalnum())
    if not compact:
        return NormalizedPlate(raw=str(raw), normalized="", valid=False, reason="no alphanumeric characters")

    original_compact = compact
    compact, stripped_band = _strip_blue_band(compact)
    band_note = "dropped leading blue-strip fragment; " if stripped_band else ""

    if len(compact) < PLATE_MIN_LEN or len(compact) > PLATE_MAX_LEN + 1:
        return NormalizedPlate(
            raw=str(raw),
            normalized=original_compact,
            valid=False,
            reason=f"{band_note}length {len(compact)} is not a plate length ({PLATE_MIN_LEN}-{PLATE_MAX_LEN})",
            candidates=[original_compact],
        )

    variants = _candidate_variants(compact)
    pool = list(variants)
    repair_note = ""

    # A pattern-valid string with an un-whitelisted state code (HL, N+, ...) is a
    # misread, not a rare plate. Try to explain it, but only accept the repair when
    # exactly one real RTO code comes out of it - two plausible codes means we do
    # not know, and guessing would be exactly the "HL" bug.
    needs_repair = [v for v in variants if pattern_ok(v) and not state_code_ok(v)]
    if needs_repair and len(compact) <= PLATE_MAX_LEN:
        # Group by the RTO code we would have to believe, ignoring alternative
        # segmentations of the digits - "MH12AB1234" vs "MH12ABI234" are the same
        # belief about the state, so they must not look like an ambiguity.
        codes: Dict[str, str] = {}
        for variant in needs_repair:
            body = variant[2:]
            observed = compact[:2]
            # A digit where a state letter belongs means the glyph is damaged, not
            # that we know which letter it was. "M1" could be MH (clipped H) or ML
            # (real Meghalaya), so do not repair it - report it as uncertain instead.
            if not observed.isalpha():
                continue
            # Single-character substitution only: OCR damages one glyph, not two.
            # "NH" -> "MH" is the one valid single swap; "HL" has two (ML, NL) and
            # is therefore left alone rather than guessed at.
            for position in (0, 1):
                for letter in _state_code_letters(observed[position]):
                    if letter == observed[position]:
                        continue
                    state = observed[:position] + letter + observed[position + 1 :]
                    if state in VALID_STATE_CODES and is_valid_plate(state + body):
                        codes.setdefault(state, f"{observed[position]} read as '{letter}' at position {position + 1}")

        if len(codes) == 1:
            code = next(iter(codes))
            pool.extend(f"{code}{v[2:]}" for v in needs_repair)
            repair_note = f"{band_note}{codes[code]}; "
        elif len(codes) > 1:
            repair_note = f"{band_note}ambiguous state code ({'/'.join(sorted(codes))}); "

    # Nothing is even pattern-shaped: a character was most likely clipped off the
    # left edge of the crop. Offer the restored strings, but only trust them when a
    # single RTO code explains them.
    if not any(is_valid_plate(v) for v in pool) and len(compact) < PLATE_MAX_LEN:
        restored: Dict[str, List[str]] = {}
        for candidate, _note in _lost_leading_options(compact):
            for variant in _candidate_variants(candidate):
                if is_valid_plate(variant):
                    restored.setdefault(variant[:2], []).append(variant)
        if len(restored) == 1:
            code = next(iter(restored))
            pool.extend(restored[code])
            repair_note = f"{band_note}leading character lost, restored state code {code}; "
        elif len(restored) > 1:
            repair_note = f"{band_note}leading character lost, would be {'/'.join(sorted(restored))}; "

    best, reason = _rank_candidates(pool, compact, ocr_score)
    if best is None:
        return NormalizedPlate(
            raw=str(raw),
            normalized=original_compact,
            valid=False,
            reason=f"{band_note}{reason}",
            candidates=variants,
        )

    # Never report a plate whose state code is not a real RTO as valid.
    valid = is_valid_plate(best)
    normalized = best if valid else original_compact
    return NormalizedPlate(
        raw=str(raw),
        normalized=normalized,
        valid=valid,
        reason=f"{repair_note}{reason}",
        state_code_valid=valid or state_code_ok(original_compact),
        # A read whose *state code* had to be changed is a hypothesis: it is a
        # candidate plate, never a reliable one, so callers must corroborate it
        # (multi-frame vote) before acting on it. This is what stops "M1" from
        # becoming a confident "ML". Positional digit/letter fixes inside the
        # series and tail do not count - those are ordinary OCR corrections.
        repaired=bool(repair_note) and best[:2] != compact[:2],
        candidates=sorted({c for c in pool if is_valid_plate(c)} or {best}),
    )


def confidence_ok(confidence: float | None, threshold: float = 0.5) -> bool:
    if confidence is None:
        return False
    return confidence >= threshold


# A read must clear the state-code whitelist *and* look sharp before it is allowed
# to trigger a hotlist match. Below this we return "uncertain" with the best guess:
# a wrong plate match is far worse than asking for another frame.
RELIABLE_CONFIDENCE = 0.85
MIN_REPORT_CONFIDENCE = 0.5


def plate_read_reliable(plate: Optional[str], confidence: float | None) -> bool:
    """True when a read is confident enough to act on."""
    if not is_valid_plate(plate):
        return False
    if confidence is None or confidence < MIN_REPORT_CONFIDENCE:
        return False
    return confidence >= RELIABLE_CONFIDENCE