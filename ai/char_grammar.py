"""Grammar-constrained decoding for Indian plate strings.

Why grammar at all
------------------
``DL1ZA9092`` and ``DL12AG092`` are both legal plates. A per-character
classifier that is *fairly* sure the third glyph is a ``2`` cannot tell those
two readings apart on its own - and neither can a validator, because both
parse. What does separate them is the grammar:

    <state:2 alpha> <RTO:1-2 digits> <series:1-3 alpha> <tail:1-4 digits>

A candidate reading is only worth considering if it *parses*, and the
positions that are digits are allowed to be corrected only into digits. That
constraint is worth more than any amount of extra confidence, because it
removes the entire space of readings that would send an officer to the wrong
car instead of just to the wrong block.

This module holds the grammar and nothing else: no model, no image, no I/O.
``run_ocr`` does the arithmetic with the classifier; tests exercise the
grammar here directly, which is why the interesting cases (``DL1ZA9092``,
Z/7/2, O/0, I/1, ...) can be asserted without a trained network on disk.

The state whitelist comes from :data:`app.services.plate.VALID_STATE_CODES`,
imported lazily so this module also works where the backend is not importable
(in which case the constraint is simply not applied, and
:meth:`GrammarResult.reason` says why).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Dict, List, Optional, Sequence, Tuple

# The verifier's class order. Importing it from `char_verifier` keeps the two
# in step by construction; the fallback exists only so this module can be
# imported on a machine without onnxruntime (char_verifier imports it).
try:  # pragma: no cover - trivial
    from ai.char_verifier import CHARS as CHARS
except Exception:  # noqa: BLE001
    CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"  # type: ignore[assignment]

DIGITS = "0123456789"
ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
CHAR_TO_IDX: Dict[str, int] = {c: i for i, c in enumerate(CHARS)}

# Segment shapes the grammar accepts. RTO codes are one or two digits (the
# leading zero of a code like ``01`` is significant and easy for OCR to eat),
# the series is one to three letters, the numeric tail one to four digits.
RTO_LEN = (1, 2)
SERIES_LEN = (1, 2, 3)
TAIL_LEN = (1, 2, 3, 4)


def templates(length: int) -> List[str]:
    """Every ``A``/``D`` pattern of ``length`` that parses as an Indian plate.

    ``"AA" + "D"*rto + "A"*series + "D"*tail``. An empty list means no reading
    of that length can be a legal plate - which is information, not failure:
    the caller marks the read unverifiable rather than inventing a parse.
    """
    out: List[str] = []
    for rto in RTO_LEN:
        for series in SERIES_LEN:
            for tail in TAIL_LEN:
                if 2 + rto + series + tail == length:
                    out.append("AA" + "D" * rto + "A" * series + "D" * tail)
    return out


def parse_segments(pattern: str) -> Tuple[int, int, int]:
    """``(rto_digits, series_letters, tail_digits)`` for a template."""
    i = 2
    start = i
    while i < len(pattern) and pattern[i] == "D":
        i += 1
    rto_n = i - start
    start = i
    while i < len(pattern) and pattern[i] == "A":
        i += 1
    series_n = i - start
    tail_n = len(pattern) - i
    return rto_n, series_n, tail_n


def allowed_chars(pattern: str) -> List[str]:
    """The single class each position may hold, as a list of candidate chars."""
    return [ALPHA if c == "A" else DIGITS for c in pattern]


def state_whitelist() -> Optional[List[str]]:
    """Valid two-letter state codes, or ``None`` when unavailable.

    ``None`` is a real answer: without the backend import there is no
    whitelist, and the decoder reports that it did not apply one instead of
    pretending it did.
    """
    try:
        from app.services.plate import VALID_STATE_CODES  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    return sorted(VALID_STATE_CODES)


@dataclass
class GrammarResult:
    """What the grammar decided, before the classifier is consulted."""

    ok: bool = False
    pattern: str = ""
    state: str = ""
    # Per position, the characters that position may legally become. Empty
    # list when the grammar could not be applied (unknown length, or no
    # whitelist to constrain the state with).
    allowed: List[List[str]] = field(default_factory=list)
    reason: str = ""
    segments: Tuple[int, int, int] = (0, 0, 0)

    @property
    def classes(self) -> List[str]:
        """``"A"`` / ``"D"`` per position, for logging and tests."""
        return ["A" if set(a) <= set(ALPHA) else "D" for a in self.allowed]


def decode(
    ocr_text: str,
    probs: Optional[Sequence[Sequence[float]]] = None,
    states: Optional[Sequence[str]] = None,
    require_state: bool = True,
) -> GrammarResult:
    """Pick the template and per-position character whitelist for ``ocr_text``.

    The chosen state is the valid code that scores best against whatever the
    classifier believes the first two glyphs are - but the OCR's own state wins
    by a fixed margin whenever it is valid, because flipping ``DL`` to ``MH``
    because the classifier liked ``M`` 4% more would be a strictly worse error
    than the one we are trying to correct.

    ``probs`` is one 36-length probability vector per position (``None`` entries
    allowed); it is only consulted to choose between valid states.

    ``require_state=False`` keeps the letter/digit shape but drops the state
    whitelist (used when the backend is not importable).
    """
    text = "".join(ch for ch in ocr_text.upper() if ch.isalnum())
    pats = templates(len(text))
    if not pats:
        return GrammarResult(ok=False, reason=f"length {len(text)} has no legal plate shape")

    if require_state and states is None:
        states = state_whitelist()

    # A length can have more than one parse (10 chars is both 2+1+3+4 and
    # 2+2+2+4). Prefer the parse whose classes the OCR text already agrees
    # with: that is the reading under which the OCR was not making a mistake.
    def class_agreement(p: str) -> int:
        n = 0
        for ch, want in zip(text, p):
            if (want == "A" and ch.isalpha()) or (want == "D" and ch.isdigit()):
                n += 1
        return n

    pats.sort(key=lambda p: -class_agreement(p))
    pattern = pats[0]

    state = ""
    whitelisted = bool(require_state and states)
    if len(text) >= 2 and text[:2].isalpha():
        if not whitelisted:
            state = text[:2]
        elif text[:2] in states:
            state = text[:2]
        elif probs:
            # Not a legal state: fall back to the classifier's evidence rather
            # than silently allowing any two letters through.
            state = choose_state(probs, text, states)

    allowed = allowed_chars(pattern)
    if whitelisted:
        if state:
            allowed[0] = [state[0]]
            allowed[1] = [state[1]]
        else:
            # No evidence at all for a state: keep every legal letter at those
            # positions, so the fusion step still cannot emit a non-letter.
            allowed[0] = sorted({s[0] for s in states or ()}) or list(ALPHA)
            allowed[1] = sorted({s[1] for s in states or ()}) or list(ALPHA)

    return GrammarResult(
        ok=True,
        pattern=pattern,
        state=state,
        allowed=allowed,
        reason="state whitelist applied" if whitelisted else "shape only (no state whitelist)",
        segments=parse_segments(pattern),
    )


def choose_state(
    probs: Sequence[Sequence[float]],
    ocr_text: str,
    states: Optional[Sequence[str]] = None,
    bonus: float = 2.0,
) -> str:
    """The valid state code the classifier most supports, favouring the OCR's.

    ``bonus`` (in nats) is how much a *different* state has to beat the OCR's
    own by. Scores are ``sum(log p)`` over the two positions, so a rival state
    must be meaningfully more likely, not just fractionally.
    """
    if states is None:
        states = state_whitelist()
    if not states or len(probs) < 2 or len(ocr_text) < 2:
        return ""
    ocr_state = ocr_text[:2].upper()

    def score(code: str) -> float:
        total = 0.0
        for i, ch in enumerate(code[:2]):
            p = probs[i]
            idx = CHAR_TO_IDX.get(ch)
            if idx is None or idx >= len(p):
                return float("-inf")
            # Log probabilities: multiplying raw probabilities would let one
            # very confident position buy a state that loses the other one.
            total += math.log(max(float(p[idx]), 1e-9))
        return total

    best_code, best_score = "", float("-inf")
    for code in states:
        if len(code) != 2:
            continue
        s = score(code)
        if s == float("-inf"):
            continue
        if code != ocr_state:
            s -= bonus
        if s > best_score:
            best_code, best_score = code, s
    return best_code
