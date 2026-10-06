"""The character verifier: grammar, fusion, skip paths, and the verdict gate.

Three things are being pinned down here, in order of how much damage they do if
they rot:

1. **The grammar** (``ai/char_grammar``) must not let a correction move a
   character into a class the plate does not allow at that position. The
   ``Z``/``2`` pair is the whole reason this feature exists: ``DL1ZA9092`` and
   ``DL12A9092``... are *both* legal shapes, so a classifier that happily turns
   a series letter into a digit does not fix a read, it swaps one real car for
   another.
2. **The skip paths.** Missing model, missing segmenter, wrong glyph count -
   none of these may raise, and all of them must leave the read *marked*
   unverified, because that flag is what makes the verdict ask for a second
   frame.
3. **The verdict gate.** AMBIGUOUS positions never reach STOLEN, and a read
   nobody verified never corroborates itself.

No trained model is needed: the classifier results are constructed here, so
these tests are about the decisions around the model rather than about its
weights (``scripts/train_char_classifier.py`` owns those numbers).
"""

from __future__ import annotations

import types

import numpy as np
import pytest

from ai import char_grammar
from ai import char_verifier as _real_verifier
from ai.char_verifier import CHAR_TO_IDX, CHARS
from app.api.v1 import live_scan as ls
from app.services.plate_verdict import (
    AMBIGUOUS_CHAR_CONF,
    POSSIBLE,
    STOLEN,
    PlateRead,
    TrackVerdict,
    ambiguous_positions,
    strict_syntax,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _probs(picks: dict, rest: float = 0.002) -> np.ndarray:
    """A 36-length probability vector with probability mass where we want it."""
    p = np.full(len(CHARS), rest, np.float32)
    for ch, v in picks.items():
        p[CHAR_TO_IDX[ch]] = v
    return p


def _v(picks: dict) -> dict:
    """A classifier result dict shaped exactly like `classify_batch`'s."""
    probs = _probs(picks)
    order = np.argsort(probs)[::-1]
    return {
        "probs": probs,
        "max_c": CHARS[int(order[0])],
        "max_p": float(probs[order[0]]),
        "top3": [[CHARS[int(j)], float(probs[j])] for j in order[:3]],
    }


def _conf(n: float, hi: float = 0.95) -> list:
    return [hi] * n


def _verdict(reads, hot=("DL1ZA9092",), now=0.0):
    tv = TrackVerdict(1, now=now)
    for r in reads:
        tv.add(r)
    return tv.evaluate(hot, now=now)


# --------------------------------------------------------------------------- #
# 1. grammar / templates
# --------------------------------------------------------------------------- #
def test_every_legal_length_has_templates_and_nothing_else_does():
    # 2 state + 1..2 RTO + 1..3 series + 1..4 tail: 5..11 inclusive.
    for n in (5, 6, 7, 8, 9, 10, 11):
        pats = char_grammar.templates(n)
        assert pats, f"length {n} should parse"
        for p in pats:
            assert len(p) == n
            assert p.startswith("AA")
            rto, series, tail = char_grammar.parse_segments(p)
            assert rto in char_grammar.RTO_LEN
            assert series in char_grammar.SERIES_LEN
            assert tail in char_grammar.TAIL_LEN
            assert 2 + rto + series + tail == n
    # Three characters cannot be an Indian plate: 2 state + 1 RTO already uses
    # the whole string, leaving no room for a series or a tail. Four is the
    # same story one character later - the tail alone needs one.
    assert char_grammar.templates(3) == []
    assert char_grammar.templates(4) == []
    assert char_grammar.templates(12) == [], "and nothing past 2+2+3+4"


def test_dl1za9092_parses_and_whitelists_every_position():
    """The plate from the field report: Z at a series position, 9092 at the tail."""
    g = char_grammar.decode("DL1ZA9092")
    assert g.ok, g.reason
    assert g.state == "DL"
    assert g.allowed[0] == ["D"] and g.allowed[1] == ["L"], "the state is fixed"
    assert "1" in g.allowed[2], "the RTO code is digits"
    assert set(g.allowed[3]) <= set(char_grammar.ALPHA), "the series is letters"
    assert "Z" in g.allowed[3], "and Z is one of them"
    assert set(g.allowed[6]) <= set(char_grammar.DIGITS), "the tail is digits"
    assert g.segments[2] == 4, "DL1ZA9092 ends in four digits"


def test_an_unparseable_length_says_so_instead_of_guessing():
    g = char_grammar.decode("AB")
    assert not g.ok
    assert "no legal plate shape" in g.reason
    assert g.allowed == []


def test_a_state_that_is_not_a_state_does_not_survive_the_whitelist():
    """``XX`` is not an Indian state code, so the whitelist must not keep it."""
    states = char_grammar.state_whitelist()
    if not states:  # backend not importable in this environment
        pytest.skip("state whitelist unavailable")
    assert "XX" not in states and "DL" in states

    probs = [_probs({"D": 0.6, "L": 0.6}), _probs({"L": 0.6, "H": 0.3})] + [
        _probs({"1": 0.9}) for _ in range(7)
    ]
    g = char_grammar.decode("XX1ZA9092", probs)
    assert g.ok
    assert g.state in states, f"{g.state!r} should be a legal state code"
    assert g.state == "DL", "the classifier's evidence points at DL"
    assert g.allowed[0] == ["D"] and g.allowed[1] == ["L"]


# --------------------------------------------------------------------------- #
# 2. fusion: what the classifier may and may not change
# --------------------------------------------------------------------------- #
def test_a_confident_ocr_is_not_overruled_on_a_doubtful_glyph():
    """Z read confidently, classifier prefers 2 -> AMBIGUOUS, not a rewrite."""
    text = "DL1ZA9092"
    verdicts = [
        _v({"D": 0.9, "L": 0.05}),
        _v({"L": 0.9, "D": 0.05}),
        _v({"1": 0.9, "2": 0.05}),
        _v({"2": 0.9, "Z": 0.05, "7": 0.04}),  # wants 2 where a letter belongs
        _v({"A": 0.9, "Z": 0.05}),
        _v({"9": 0.9}),
        _v({"0": 0.9}),
        _v({"9": 0.9}),
        _v({"2": 0.9}),
    ]
    details, pattern = ls._fuse_chars(text, _conf(len(text)), verdicts)
    assert pattern, "the grammar should have picked a template"
    assert "".join(d["c"] for d in details) == text, "nothing may be rewritten"
    assert details[3]["cls"].isalpha(), (
        "the grammar only allows letters in the series, so '2' can never be chosen"
    )
    # The doubt has to survive somewhere. Here it survives as confidence: the
    # restricted answer is the right letter on almost no probability, so the
    # position reads weak and `ambiguous_positions` flags it downstream even
    # though the character itself was left alone.
    assert details[3]["p"] < 0.5, details[3]
    assert details[3]["p"] == pytest.approx((0.95 * 0.05) ** 0.5)


def test_a_correction_lands_when_the_ocr_is_weak_and_the_classifier_is_sure():
    """The same digit position, this time with the OCR unsure of itself."""
    text = "DL1ZA9092"
    confs = [0.95, 0.95, 0.95, 0.95, 0.95, 0.95, 0.95, 0.95, 0.05]
    verdicts = [
        _v({"D": 0.9}),
        _v({"L": 0.9}),
        _v({"1": 0.9}),
        _v({"Z": 0.9}),
        _v({"A": 0.9}),
        _v({"9": 0.9}),
        _v({"0": 0.9}),
        _v({"9": 0.9}),
        _v({"7": 0.92, "2": 0.04}),  # tail digit: 7 or 2, classifier says 7
    ]
    details, _ = ls._fuse_chars(text, confs, verdicts)
    assert details[8]["status"] == "replaced"
    assert details[8]["c"] == "7"
    assert "".join(d["c"] for d in details) == "DL1ZA9097"
    # A correction is only worth what the OCR's own doubt was worth.
    assert details[8]["p"] == pytest.approx(((1 - 0.05) * 0.92) ** 0.5)


def test_a_glyph_from_the_wrong_class_can_never_be_written_into_a_digit_slot():
    """``O`` where only digits parse must not become a letter.

    The classifier's *unrestricted* answer is O; the restricted one is the best
    digit. Neither may replace the OCR's ``0`` unless the digit itself is
    confidently the answer - so a strong wrong-class opinion degrades to
    AMBIGUOUS and the read stays as it was.
    """
    text = "MH12AB0456"
    verdicts = [
        _v({"M": 0.9}),
        _v({"H": 0.9}),
        _v({"1": 0.9}),
        _v({"2": 0.9}),
        _v({"A": 0.9}),
        _v({"B": 0.9}),
        _v({"O": 0.9, "0": 0.03}),  # insists on a letter in a digit slot
        _v({"4": 0.9}),
        _v({"5": 0.9}),
        _v({"6": 0.9}),
    ]
    assert strict_syntax(text)[0]
    details, _ = ls._fuse_chars(text, _conf(len(text)), verdicts)
    joined = "".join(d["c"] for d in details)
    assert joined == text, "a wrong-class glyph must not reach the string"
    assert details[6]["cls"].isdigit(), "the restricted answer stays a digit"
    # The grammar wins the character; the classifier's disagreement survives as
    # a confidence nobody will act on - below the bar `ambiguous_positions`
    # uses for a glyph that can be read two ways.
    assert details[6]["p"] < AMBIGUOUS_CHAR_CONF, details[6]
    assert ambiguous_positions(joined, [d["p"] for d in details]) == [6]


@pytest.mark.parametrize(
    "letter,digit,text",
    [
        # The pairs the whole feature was asked to separate.
        ("B", "8", "MH12AB8456"),   # B/8
        ("S", "5", "MH12AS5456"),   # S/5
        ("G", "6", "MH12AG6456"),   # G/6
        ("I", "1", "DL01IA0112"),   # I/1
    ],
)
def test_cross_class_confusions_are_blocked_in_both_directions(letter, digit, text):
    """A digit suggested in a letter slot, and a letter in a digit slot."""
    assert strict_syntax(text)[0], text
    # The classifier claims the digit belongs where the plate has a letter.
    want_digit = [
        _v({letter: 0.05, digit: 0.9}) if ch == letter else _v({ch: 0.9})
        for ch in text
    ]
    details, _ = ls._fuse_chars(text, _conf(len(text), hi=0.9), want_digit)
    assert letter in "".join(d["c"] for d in details), (
        f"{letter} was replaced by a {digit} in a letter slot"
    )
    assert all(d["status"] != "replaced" for d in details if d["ocr"] == letter)

    # And the other direction: a letter suggested where digits parse.
    digit_pos = next(i for i, ch in enumerate(text) if ch == digit)
    want_letter = [_v({ch: 0.9}) for ch in text]
    want_letter[digit_pos] = _v({letter: 0.9, digit: 0.03})
    details, _ = ls._fuse_chars(text, _conf(len(text), hi=0.9), want_letter)
    assert details[digit_pos]["c"] == digit, "a letter suggestion reached a digit slot"
    assert details[digit_pos]["status"] in {"ok", "ambiguous"}


def test_ocr_and_classifier_agreeing_is_marked_ok_and_keeps_both():
    text = "DL1ZA9092"
    verdicts = [_v({ch: 0.9}) for ch in text]
    details, _ = ls._fuse_chars(text, _conf(len(text)), verdicts)
    assert [d["status"] for d in details] == ["ok"] * len(text)
    assert "".join(d["c"] for d in details) == text
    for d in details:
        assert d["p"] == pytest.approx((0.95 * 0.9) ** 0.5)


def test_without_the_grammar_a_read_is_still_fused_but_unconstrained():
    """A length the grammar does not know about must not disable the verifier."""
    text = "AB12"  # 4 chars: the shortest legal plate is 5
    assert char_grammar.templates(len(text)) == []
    verdicts = [_v({ch: 0.9}) for ch in text]
    details, pattern = ls._fuse_chars(text, _conf(len(text)), verdicts)
    assert pattern == ""
    assert "".join(d["c"] for d in details) == text
    assert [d["status"] for d in details] == ["ok"] * len(text)
    for d in details:
        assert d["p"] > 0.9, "unconstrained agreement is still agreement"


# --------------------------------------------------------------------------- #
# 3. run_ocr: skip paths
# --------------------------------------------------------------------------- #
def _fake_read(text="DL1ZA9092", confs=None):
    return types.SimpleNamespace(
        text=text,
        char_confs=tuple(confs) if confs is not None else tuple([0.95] * len(text)),
        conf=0.95,
        best_variant="orig",
        reason="",
    )


class _StubVerifier:
    """The verifier interface run_ocr touches, with a controllable answer.

    `_fuse_chars` reaches for ``restricted_max``/``probability_of`` too, and
    those are pure functions of one result dict - so the stub delegates to the
    real ones rather than pretending they do not exist, which would turn every
    successful verification into the fault path.
    """

    def __init__(self, enabled=True, verdicts=None):
        self._enabled = enabled
        self._verdicts = verdicts

    def is_enabled(self):
        return self._enabled

    def classify_batch(self, crops):
        return list(self._verdicts or [None] * len(crops))

    def restricted_max(self, result, allowed):
        return _real_verifier.restricted_max(result, allowed)

    def probability_of(self, result, char):
        return _real_verifier.probability_of(result, char)


class _StubSegmenter:
    def __init__(self, count=None):
        self._count = count

    def segment_chars(self, crop, text):
        n = len(text) if self._count is None else self._count
        return [np.zeros((8, 8, 3), np.uint8) for _ in range(n)]


def _run(monkeypatch, *, crop_count=None, enabled=True, verdicts=None, text="DL1ZA9092"):
    monkeypatch.setattr(ls, "read_crop", lambda crop: _fake_read(text))
    monkeypatch.setattr(ls, "char_verifier", _StubVerifier(enabled, verdicts))
    monkeypatch.setattr(ls, "char_segment", _StubSegmenter(crop_count))
    monkeypatch.setattr(ls.settings, "CHAR_VERIFY", True, raising=False)
    return ls.run_ocr(np.zeros((64, 256, 3), np.uint8))


def test_a_segmentation_that_does_not_match_the_ocr_length_skips_verification(
    monkeypatch,
):
    """Fewer glyphs than characters: the labels would line up with nothing."""
    assert len("DL1ZA9092") == 9
    result = _run(monkeypatch, crop_count=8, verdicts=[_v({"D": 0.9})] * 9)
    assert result is not None
    assert result["verified"] is False
    assert result["ambiguous"] == []
    assert {d["status"] for d in result["chars"]} == {"unverified"}
    assert result["text"] == "DL1ZA9092"
    assert len(result["char_confs"]) == len(result["text"])


def test_a_missing_model_falls_back_to_an_unverified_read(monkeypatch):
    result = _run(monkeypatch, enabled=False)
    assert result["verified"] is False
    assert {d["status"] for d in result["chars"]} == {"unverified"}
    for key in ("text", "norm", "conf", "valid", "char_confs", "variant"):
        assert key in result, key


def test_char_verify_off_never_reaches_the_model(monkeypatch):
    """CHAR_VERIFY=off must not even ask for a segmentation."""
    seen = []

    class _CountingSegmenter(_StubSegmenter):
        def segment_chars(self, crop, text):
            seen.append(text)
            return super().segment_chars(crop, text)

    monkeypatch.setattr(ls, "read_crop", lambda crop: _fake_read())
    monkeypatch.setattr(ls, "char_verifier", _StubVerifier(True))
    monkeypatch.setattr(ls, "char_segment", _CountingSegmenter())
    monkeypatch.setattr(ls.settings, "CHAR_VERIFY", False, raising=False)
    result = ls.run_ocr(np.zeros((64, 256, 3), np.uint8))
    assert result["verified"] is False
    assert seen == [], "the segmenter must not be paid for when verification is off"


def test_a_verifier_fault_never_loses_the_read(monkeypatch):
    class _Boom:
        def is_enabled(self):
            return True

        def classify_batch(self, crops):
            raise RuntimeError("onnx exploded")

    monkeypatch.setattr(ls, "read_crop", lambda crop: _fake_read())
    monkeypatch.setattr(ls, "char_verifier", _Boom())
    monkeypatch.setattr(ls, "char_segment", _StubSegmenter())
    monkeypatch.setattr(ls.settings, "CHAR_VERIFY", True, raising=False)
    result = ls.run_ocr(np.zeros((64, 256, 3), np.uint8))
    assert result is not None
    assert result["verified"] is False
    assert result["text"] == "DL1ZA9092"


def test_a_successful_verification_reports_per_character_detail(monkeypatch):
    verdicts = [_v({ch: 0.9}) for ch in "DL1ZA9092"]
    result = _run(monkeypatch, verdicts=verdicts)
    assert result["verified"] is True
    assert result["grammar"]
    assert len(result["chars"]) == len(result["text"])
    assert all(d["status"] == "ok" for d in result["chars"])
    assert result["ambiguous"] == []
    assert set(result) >= {"text", "norm", "conf", "valid", "char_confs", "variant", "chars"}


def test_a_spaced_ocr_read_is_still_verified(monkeypatch):
    """RapidOCR keeps the spaces it saw; a plate does not have them.

    Before the read was compacted, ``'DL 1Z A90 92'`` was thirteen characters
    against a ten-glyph plate, so the segment count could never match and
    **every** real read skipped verification - while the returned string still
    looked perfectly normal, which is why nothing failed loudly.
    """
    monkeypatch.setattr(
        ls, "read_crop", lambda crop: _fake_read("DL 1Z A90 92", confs=[0.95] * 9)
    )
    monkeypatch.setattr(
        ls, "char_verifier", _StubVerifier(True, [_v({ch: 0.9}) for ch in "DL1ZA9092"])
    )
    monkeypatch.setattr(ls, "char_segment", _StubSegmenter())
    monkeypatch.setattr(ls.settings, "CHAR_VERIFY", True, raising=False)

    result = ls.run_ocr(np.zeros((64, 256, 3), np.uint8))

    assert result is not None
    assert result["text"] == "DL1ZA9092", "the space-free plate is what everyone downstream gets"
    assert result["verified"] is True, "a spaced read must not skip verification"
    assert len(result["chars"]) == len(result["text"]) == len(result["char_confs"])
    assert result["ambiguous"] == []
    assert [d["status"] for d in result["chars"]] == ["ok"] * 9


def test_confidences_that_do_not_fit_the_string_are_dropped_not_shifted(monkeypatch):
    """A per-character list of the wrong length is the wrong list, not a hint.

    Falling back to the line score is honest; zipping a 4-long list onto a
    9-character plate would put every doubt on the wrong glyph, and the wrong
    glyph is the one the verdict is about to ask a volunteer to check.
    """
    monkeypatch.setattr(
        ls, "read_crop", lambda crop: _fake_read("DL1ZA9092", confs=[0.1] * 4)
    )
    monkeypatch.setattr(
        ls, "char_verifier", _StubVerifier(True, [_v({ch: 0.9}) for ch in "DL1ZA9092"])
    )
    monkeypatch.setattr(ls, "char_segment", _StubSegmenter())
    monkeypatch.setattr(ls.settings, "CHAR_VERIFY", True, raising=False)

    result = ls.run_ocr(np.zeros((64, 256, 3), np.uint8))

    expected = (0.95 * 0.9) ** 0.5  # line score 0.95 x classifier 0.9
    assert result["verified"] is True
    assert result["char_confs"] == pytest.approx([expected] * 9)
    assert result["valid"] is True


def test_run_ocr_still_returns_none_below_the_read_floor(monkeypatch):
    """The verifier must not resurrect a crop the OCR could not read at all."""
    monkeypatch.setattr(ls, "read_crop", lambda crop: None)
    assert ls.run_ocr(np.zeros((64, 256, 3), np.uint8)) is None


# --------------------------------------------------------------------------- #
# 4. the verdict gate
# --------------------------------------------------------------------------- #
HOT = ("DL1ZA9092",)
HIGH = tuple([0.99] * 9)


def _read(text="DL1ZA9092", conf=0.97, at=0.0, char_confs=HIGH, verified=True, amb=()):
    return PlateRead(
        text=text,
        norm=text,
        conf=conf,
        at=at,
        valid=True,
        char_confs=tuple(char_confs) if char_confs else None,
        verified=verified,
        amb=tuple(amb),
    )


def test_one_verified_read_can_stand_alone():
    assert _verdict([_read(conf=0.95)], HOT).state == STOLEN


def test_consensus_is_required_when_the_verifier_was_skipped():
    """No glyphs checked -> one confident read is a hint, not a banner."""
    alone = _verdict([_read(conf=0.95, verified=False)], HOT)
    assert alone.state == POSSIBLE
    assert "not verified" in alone.reason

    twice = _verdict(
        [_read(conf=0.95, at=0.0, verified=False), _read(conf=0.95, at=1.0, verified=False)],
        HOT,
    )
    assert twice.state == STOLEN, "two agreeing reads are consensus with or without a model"


def test_an_ambiguous_position_never_reaches_stolen():
    """The Z that could be a 2: verified, confident line, still not STOLEN."""
    v = _verdict([_read(conf=0.99, amb=(3,))], HOT)
    assert v.state == POSSIBLE
    assert v.ambiguous is True
    assert v.weak == (3,), "the phone must be told *which* glyph is in doubt"


def test_ambiguity_from_a_read_that_settled_elsewhere_is_ignored():
    """Positions on a different string are not positions on this one.

    The other read is deliberately weaker so the string vote picks the clean
    one; a stronger misread would be a different test (which car is it?).
    """
    other = _read(text="DL12A9092", conf=0.7, amb=(3,))
    v = _verdict([_read(conf=0.95), other], HOT)
    assert v.state == STOLEN
    assert v.ambiguous is False
    assert v.weak == ()


def test_two_verified_reads_agreeing_still_alert():
    v = _verdict([_read(at=0.0), _read(at=1.0)], HOT)
    assert v.state == STOLEN
    assert v.reads == 2
