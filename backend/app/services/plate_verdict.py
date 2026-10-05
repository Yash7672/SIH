"""One place that decides STOLEN / POSSIBLE / CLEAR / UNREAD for a track.

Why this module exists
----------------------
The live scanner used to answer "is this stolen?" with a single ``plate in
hotlist`` test on whatever one OCR pass happened to return, and the phone drew a
red banner whenever that flag was set. That produced two bad outcomes at once:

* a *correct* read could be rewritten by ``normalize_plate`` into a different,
  also-valid plate (DL 1 ZA 9092 -> DL 12 AG 092), so a stolen car was reported
  as innocent; and
* a *garbled* read that happened to be spelled like a listed plate raised a red
  banner and woke the police.

Neither is a bug in the normaliser or in the OCR; the bug is that one weak read
was allowed to decide. The decision needs more than one observation, so it lives
here, per track, and the phone renders exactly what this returns - it is not
allowed to match plates itself.

The four states
---------------
``STOLEN``   exact hot-list match, corroborated. Red banner, sighting, police alert.
``POSSIBLE`` close to a listed plate but not provably it. Amber banner, phone only.
``CLEAR``    a valid read that is not on the hot-list.
``UNREAD``   nothing valid yet.

The asymmetry is deliberate: a false STOLEN sends a police unit after a car that
may be innocent, and a false CLEAR is only a missed sighting. So every rule that
can raise the state has to be satisfied by evidence, and nothing lowers the state
except a new read.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.services.plate import VALID_STATE_CODES

# --------------------------------------------------------------------------- #
# States
# --------------------------------------------------------------------------- #
STOLEN = "STOLEN"
POSSIBLE = "POSSIBLE"
CLEAR = "CLEAR"
UNREAD = "UNREAD"

# --------------------------------------------------------------------------- #
# Strict syntax
# --------------------------------------------------------------------------- #
# `<state><RTO digits 1-2><series letters 1-3><trailing digits 1-4>`.
#
# This is deliberately stricter than `app.services.plate.INDIAN_PLATE_PATTERN`
# on one point: the *pattern* there also admits a plate whose state code is not a
# real RTO, and it treats a 1-digit RTO and a 2-digit RTO as equally good. For a
# decision that pages the police, "looks like a plate" is not enough - the state
# code must be a real one, and the common 4-digit tail is worth more than the
# legal-but-rare 1-to-3 digit tails.
STRICT_PATTERN = re.compile(r"^([A-Z]{2})([0-9]{1,2})([A-Z]{1,3})([0-9]{1,4})$")

# Four trailing digits is the normal case and gets the full score. One to three
# is legal (older Delhi format, some reissues) but is also what a clipped right
# edge produces, so it is worth less.
TAIL_LENGTH_SCORE = {4: 1.0, 3: 0.85, 2: 0.7, 1: 0.55}

# The RTO block is one or two digits; two is overwhelmingly the norm.
RTO_LENGTH_SCORE = {2: 1.0, 1: 0.8}


def strict_syntax(plate: Optional[str]) -> Tuple[bool, str, float]:
    """Check a normalised plate against the full Indian plate grammar.

    Returns ``(ok, reason, score)`` where score in 0..1 is the grammar prior:
    it is multiplied into the read's confidence so that a legal-but-unusual
    layout has to be read *better* before it is acted on, rather than being
    rejected outright.
    """
    if not plate:
        return False, "empty", 0.0
    match = STRICT_PATTERN.match(plate)
    if not match:
        return False, f"{plate} does not match <state><1-2 digits><1-3 letters><1-4 digits>", 0.0
    state, rto, _series, tail = match.groups()
    if state not in VALID_STATE_CODES:
        return False, f"{state} is not a real RTO state code", 0.0
    score = RTO_LENGTH_SCORE[len(rto)] * TAIL_LENGTH_SCORE[len(tail)]
    return True, f"{state} + {len(rto)} RTO digits + {len(tail)} trailing digits", score


# --------------------------------------------------------------------------- #
# Ambiguous characters
# --------------------------------------------------------------------------- #
# Characters that OCR confuses with one another, and the pairs that matter. A
# read that used one of these and is not confident about it can never be STOLEN
# on its own - it needs a second look at the same characters.
#
# Z/2 is the important one. DL 1 ZA 9092 and DL 12 A 9092 are *both* valid
# plates, so a misread Z does not produce an invalid string that the validator
# can throw away; it produces a different real car. That is precisely why a
# single read is not enough.
AMBIGUOUS_PAIRS: Dict[str, str] = {
    "Z": "2",
    "2": "Z",
    "O": "0",
    "0": "O",
    "I": "1",
    "1": "I",
    "S": "5",
    "5": "S",
    "B": "8",
    "8": "B",
    "G": "6",
    "6": "G",
    "9": "G",
    "D": "0",
    "Q": "0",
    "M": "H",
    "H": "M",
    "N": "M",
}
AMBIGUOUS_CHARS: frozenset = frozenset(AMBIGUOUS_PAIRS)

# A character below this confidence is "not believed". Only characters that are
# themselves ambiguous are held to this bar; a confident 7 is just a 7.
AMBIGUOUS_CHAR_CONF = 0.80


def ambiguous_positions(plate: str, char_confs: Optional[Sequence[float]]) -> List[int]:
    """Positions holding an ambiguous glyph whose confidence we do not trust.

    With no per-character scores there is nothing to distrust individually, so
    the answer is empty and the *line* score has to carry the decision.
    """
    if not char_confs:
        return []
    out: List[int] = []
    for i, ch in enumerate(plate):
        if i >= len(char_confs):
            break
        if ch in AMBIGUOUS_CHARS and char_confs[i] < AMBIGUOUS_CHAR_CONF:
            out.append(i)
    return out


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #
# Two reads of the same string, this far apart, count as the same observation.
CONSENSUS_WINDOW_S = 5.0
# ...or one read that is this good on its own.
SINGLE_READ_CONF = 0.92
# ...and every character of it at least this good.
MIN_CHAR_CONF = 0.80
# Per track, this many reads are kept.
MAX_READS = 5
# Stop early once this many reads agree at this confidence.
EARLY_STOP_COUNT = 2
EARLY_STOP_CONF = 0.90
# Fuzzy match needed for POSSIBLE.
FUZZY_THRESHOLD = 0.85
# A read below this is never reported as a plate at all.
MIN_READ_CONF = 0.45


# --------------------------------------------------------------------------- #
# Fuzzy scoring
# --------------------------------------------------------------------------- #
# Substitution costs, lowest to highest. A pair that is genuinely confusable
# costs less than a same-class typo, which costs less than swapping a digit for
# an unrelated letter - because a swap like 4 -> A almost always means the
# segment was mis-segmented rather than mis-read.
_COST_CONFUSION = 0.35
_COST_DIGIT_DIGIT = 0.60
_COST_LETTER_LETTER = 0.70
_COST_CROSS_CLASS = 0.95
_COST_GAP = 1.0


def _sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if AMBIGUOUS_PAIRS.get(a) == b:
        return _COST_CONFUSION
    if a.isdigit() and b.isdigit():
        return _COST_DIGIT_DIGIT
    if a.isalpha() and b.isalpha():
        return _COST_LETTER_LETTER
    return _COST_CROSS_CLASS


def weighted_distance(a: str, b: str) -> float:
    """Weighted Levenshtein distance between two plate strings."""
    if a == b:
        return 0.0
    prev = [_COST_GAP * j for j in range(len(b) + 1)]
    for i, ca in enumerate(a, start=1):
        cur = [_COST_GAP * i]
        for j, cb in enumerate(b, start=1):
            cur.append(
                min(
                    prev[j - 1] + _sub_cost(ca, cb),
                    prev[j] + _COST_GAP,
                    cur[j - 1] + _COST_GAP,
                )
            )
        prev = cur
    return prev[-1]


def fuzzy_score(read: str, listed: str) -> float:
    """0..1 similarity, weighted so that real confusions are cheap."""
    longest = max(len(read), len(listed))
    if longest == 0:
        return 0.0
    return max(0.0, 1.0 - weighted_distance(read, listed) / longest)


def best_fuzzy_match(
    read: str,
    hotlist: Iterable[str],
    threshold: float = FUZZY_THRESHOLD,
) -> Tuple[Optional[str], float]:
    """Closest hot-list plate to ``read``, but only within the same RTO.

    Same state code is a hard requirement. Without it, "DL12AG092" would sit
    0.85+ away from any Delhi plate and the amber banner would fire on a
    coincidence; with it, the amber banner means "this looks like that specific
    listed car", which is the only thing a volunteer can act on.
    """
    if not read or len(read) < 2:
        return None, 0.0
    state = read[:2]
    best: Optional[str] = None
    best_score = 0.0
    for listed in hotlist:
        if not listed or listed[:2] != state:
            continue
        score = fuzzy_score(read, listed)
        if score > best_score:
            best, best_score = listed, score
    if best_score < threshold:
        return None, best_score
    return best, best_score


# --------------------------------------------------------------------------- #
# Reads and verdicts
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlateRead:
    """One OCR observation of one track, in one frame."""

    text: str
    norm: str
    conf: float
    at: float
    valid: bool = False
    char_confs: Optional[Tuple[float, ...]] = None
    reason: str = ""

    @property
    def grammar_ok(self) -> bool:
        return strict_syntax(self.norm)[0]


@dataclass
class Verdict:
    """The one answer the phone is allowed to render."""

    state: str
    plate: str = ""
    confidence: float = 0.0
    reads: int = 0
    matches: Optional[str] = None
    score: float = 0.0
    ambiguous: bool = False
    repaired: bool = False
    reason: str = ""
    weak: Tuple[int, ...] = ()

    def as_dict(self) -> dict:
        """Wire shape. Fields already exist on the alert payload, none added."""
        return {
            "state": self.state,
            "plate": self.plate,
            "confidence": round(float(self.confidence), 4),
            "reads": self.reads,
            "matches": self.matches,
            "score": round(float(self.score), 4),
            "ambiguous": bool(self.ambiguous),
            "repaired": bool(self.repaired),
            "reason": self.reason,
            # *Which* glyphs the recogniser was unsure about, so the phone can put
            # a "?" on the character instead of only saying "something is off".
            # Indices, not a string: a dropped character must not shift every
            # mark after it onto the wrong glyph.
            "weak": list(self.weak),
        }


def _blank(reason: str = "") -> Verdict:
    return Verdict(state=UNREAD, reason=reason)


class TrackVerdict:
    """Per-track read history and the decision derived from it.

    Lives on the connection, next to the track, and is discarded with the track.
    One instance per track id: reads from a car that left view are not evidence
    about the car that replaced it.
    """

    def __init__(self, tid: int, now: Optional[float] = None) -> None:
        self.tid = tid
        self.reads: List[PlateRead] = []
        self._last_stolen_at: Optional[float] = None
        self._now = now if now is not None else time.monotonic()
        # The last state handed out. Kept so the caller can ask "is this a *new*
        # STOLEN or the same one I already alerted on?", which is what stops a
        # parked stolen car from waking the police on every frame.
        self.state: str = UNREAD

    # ------------------------------------------------------------------ reads

    def add(self, read: PlateRead) -> None:
        self.reads.append(read)
        if len(self.reads) > MAX_READS:
            # Keep the best-scoring ones rather than the oldest: a track that sat
            # unreadable for a while then came into focus should not lose its good
            # reads to a flood of junk.
            self.reads.sort(key=lambda r: (r.conf, r.at), reverse=True)
            del self.reads[MAX_READS:]

    @property
    def has_confirmed(self) -> bool:
        """True once a read is good enough that more reads cannot add anything."""
        counts: Dict[str, int] = {}
        for r in self.reads:
            if r.valid and r.conf >= EARLY_STOP_CONF:
                counts[r.norm] = counts.get(r.norm, 0) + 1
        return any(v >= EARLY_STOP_COUNT for v in counts.values())

    # ------------------------------------------------------------- consensus

    def _cluster(self) -> Tuple[str, float, int, float]:
        """Vote the whole string first, then per character when lengths agree.

        Returns ``(norm, confidence, reads_backing_it, stability)``. Whole-string
        voting runs first because a plate that reads identically twice is far
        stronger evidence than a per-character majority assembled from several
        different misreads.
        """
        usable = [r for r in self.reads if r.valid and r.norm and r.conf >= MIN_READ_CONF]
        if not usable:
            # Nothing read well enough to act on. Keep the best raw guess anyway
            # so the phone can show "unsure: <what it thinks it saw>" instead of an
            # empty box - silence reads as "nothing there", which is a different
            # and equally wrong claim. `evaluate` still reports UNREAD for these,
            # so the guess is shown and never acted on.
            guesses = [r for r in self.reads if r.norm]
            if not guesses:
                return "", 0.0, 0, 0.0
            best_guess = max(guesses, key=lambda r: r.conf)
            return best_guess.norm, best_guess.conf, 0, 0.0

        # 1) whole-string vote, weighted by confidence
        weight: Dict[str, float] = {}
        members: Dict[str, List[PlateRead]] = {}
        for r in usable:
            weight[r.norm] = weight.get(r.norm, 0.0) + r.conf
            members.setdefault(r.norm, []).append(r)
        best = max(weight, key=lambda k: (weight[k], -len(k)))
        group = members[best]

        same_frame = {(r.at, r.norm) for r in group}
        backing = len({r.at for r in group})
        total = len(usable)
        stability = (len(group) / total) if total else 0.0
        del same_frame

        if len(group) == 1 and len({len(r.norm) for r in usable}) == 1:
            # 2) per-character vote, confidence weighted. Only worth doing when
            #    every candidate has the same length, otherwise "character 5" is
            #    not the same character in two different readings.
            voted = []
            for i in range(len(best)):
                tally: Dict[str, float] = {}
                for r in usable:
                    if len(r.norm) != len(best):
                        continue
                    ch = r.norm[i]
                    weight_ch = r.char_confs[i] if (r.char_confs and i < len(r.char_confs)) else r.conf
                    tally[ch] = tally.get(ch, 0.0) + weight_ch
                if tally:
                    voted.append(max(tally, key=lambda c: tally[c]))
            if voted:
                merged = "".join(voted)
                merged_conf = min(r.conf for r in group)
                # Only accept the character vote when it agrees with what the
                # string vote already had confidence in; otherwise keep the string.
                agree = sum(1 for a, b in zip(merged, best) if a == b)
                if agree >= max(1, len(best) // 2) and fuzzy_score(merged, best) >= 0.9:
                    return merged, merged_conf, backing, stability

        best_read = max(group, key=lambda r: r.conf)
        return best, best_read.conf, backing, stability

    def _within_window(self, winner: str) -> bool:
        """True when two reads of ``winner`` landed inside the consensus window.

        Measured on the *winner's own* timestamps, not on all valid reads: the
        question is whether the same string was seen twice in quick succession,
        and mixing in reads of some other string could make two far-apart
        observations of the same plate look close together.
        """
        times = sorted(r.at for r in self.reads if r.valid and r.norm == winner)
        if len(times) < 2:
            return False
        return (times[-1] - times[0]) <= CONSENSUS_WINDOW_S

    # -------------------------------------------------------------- decision

    def evaluate(
        self,
        hotlist: Iterable[str],
        now: Optional[float] = None,
        cooldown_ok: bool = True,
    ) -> Verdict:
        """The verdict for this track, right now.

        ``cooldown_ok`` is the caller's 60 s sighting cooldown. It gates the
        *alert*, not the state: a track that is genuinely stolen stays STOLEN
        even while the cooldown suppresses a second row, because the phone still
        has to show it.
        """
        now = self._now if now is None else now
        plates: Set[str] = {p for p in hotlist if p}
        norm, conf, backing, stability = self._cluster()
        reads = len([r for r in self.reads if r.valid])

        if not norm:
            return _blank("no valid read yet")

        if conf < MIN_READ_CONF:
            # `_cluster` handed back its best guess rather than a real read. Show
            # it so the volunteer sees roughly what is on the plate, but report
            # UNREAD: a 0.2-confidence string is not evidence about anything, and
            # matching it loosely against the hot-list would raise an amber banner
            # over a car nobody can read.
            return Verdict(
                state=UNREAD,
                plate=norm,
                confidence=conf,
                reads=0,
                reason=f"unsure: best guess {conf:.2f}, below the {MIN_READ_CONF:.2f} floor",
            )

        ok, why, grammar = strict_syntax(norm)
        reads_for_best = [
            r for r in self.reads if r.norm == norm and (r.valid or not ok)
        ] or [r for r in self.reads if r.valid]
        char_confs = next((r.char_confs for r in reads_for_best if r.char_confs), None)
        amb = ambiguous_positions(norm, char_confs)
        repaired = any(r.reason.startswith("repaired") for r in reads_for_best)

        base = {
            "plate": norm,
            "confidence": conf,
            "reads": reads,
            "ambiguous": bool(amb),
            "repaired": repaired,
            # *Which* glyphs the recogniser was unsure of, so the phone can put a
            # "?" on the character itself. A verdict of POSSIBLE because of one
            # doubtful `Z` is a different situation from one doubtful because of
            # four characters, and the volunteer walking up to the car is the last
            # line of defence either way.
            "weak": tuple(amb),
        }

        if not ok:
            # A read that is not a well-formed plate can still be shown as a
            # guess, but it is never CLEAR and never POSSIBLE: reporting "not on
            # the hotlist" for something we could not parse is a claim we cannot
            # support.
            return Verdict(
                state=UNREAD,
                reason=f"unsure: {why}",
                score=0.0,
                **base,
            )

        # ---- the hot-list, exactly
        if norm in plates:
            corroborated = (backing >= 2 and self._within_window(norm)) or (
                conf >= SINGLE_READ_CONF
                and (char_confs is None or min(char_confs) >= MIN_CHAR_CONF)
                and not amb
            )
            if corroborated:
                if not cooldown_ok and self._last_stolen_at is not None:
                    return Verdict(
                        state=STOLEN,
                        reason="exact hot-list match, corroborated (alert suppressed by cooldown)",
                        score=1.0,
                        **{**base, "matches": norm},
                    )
                self._last_stolen_at = now
                how = (
                    f"{backing} reads within {CONSENSUS_WINDOW_S:.0f}s"
                    if backing >= 2
                    else f"single read at {conf:.2f} with no weak character"
                )
                return Verdict(
                    state=STOLEN,
                    reason=f"exact hot-list match, {how}",
                    score=1.0,
                    **{**base, "matches": norm},
                )
            # Exact but uncorroborated: this is the DL1ZA9092 -> DL12AG092 case.
            # Say so plainly rather than either hiding it or raising a banner.
            near, score = best_fuzzy_match(norm, plates)
            if near and near != norm:
                return Verdict(
                    state=POSSIBLE,
                    reason=(
                        f"matches {norm} but only once ({backing} read); "
                        f"needs a second frame before alerting"
                    ),
                    score=score,
                    **{**base, "matches": norm},
                )
            return Verdict(
                state=POSSIBLE,
                reason=(
                    f"on the hot-list but uncorroborated ({backing} read"
                    f"{', ambiguous character' if amb else ''}); not alerting"
                ),
                score=1.0,
                **{**base, "matches": norm},
            )

        # ---- near, but not it
        near, score = best_fuzzy_match(norm, plates)
        if near:
            return Verdict(
                state=POSSIBLE,
                reason=f"{score:.2f} weighted similarity to listed {near}",
                score=score,
                **{**base, "matches": near},
            )

        return Verdict(state=CLEAR, reason="valid read, not on the hot-list", score=1.0, **base)


def grammar_adjusted_confidence(norm: str, conf: float) -> float:
    """Confidence after the layout prior is applied.

    A read has to be *better* to be believed when its layout is the rare one,
    which is how a 3-digit tail stops being a 0.997 accident.
    """
    ok, _why, score = strict_syntax(norm)
    if not ok:
        return 0.0
    return max(0.0, min(1.0, conf * score))