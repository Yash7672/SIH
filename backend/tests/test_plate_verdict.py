"""The decision rules, tested on their own.

These are the rules that decide whether a police unit is dispatched, so they are
tested without any OCR, any detector and any WebSocket: a read is constructed by
hand and the verdict is read back. That is deliberate - the bug being fixed here
was never in the OCR, it was in believing one read, and the only way to pin that
down is to be able to say exactly which reads are trusted.
"""

from __future__ import annotations

import time

import pytest

from app.services.plate_verdict import (
    AMBIGUOUS_CHARS,
    CLEAR,
    FUZZY_THRESHOLD,
    MAX_READS,
    POSSIBLE,
    STOLEN,
    UNREAD,
    PlateRead,
    TrackVerdict,
    best_fuzzy_match,
    fuzzy_score,
    grammar_adjusted_confidence,
    strict_syntax,
)

# The plate from the field report, and the hot-list it belongs on.
HOT = {"DL1ZA9092"}


def _read(text, norm=None, conf=0.95, at=0.0, char_confs=None, valid=None):
    """One observation. `at` is monotonic seconds, so tests can age reads."""
    n = norm if norm is not None else text
    return PlateRead(
        text=text,
        norm=n,
        conf=conf,
        at=at,
        valid=strict_syntax(n)[0] if valid is None else valid,
        char_confs=tuple(char_confs) if char_confs else None,
    )


def _verdict(reads, hot=HOT, now=None):
    tv = TrackVerdict(1, now=now if now is not None else reads[-1].at if reads else 0.0)
    for r in reads:
        tv.add(r)
    return tv.evaluate(hot, now=now if now is not None else (reads[-1].at if reads else 0.0))


ALL_HIGH = tuple([0.99] * 9)


# --------------------------------------------------------------------------- #
# STOLEN
# --------------------------------------------------------------------------- #
def test_the_real_plate_read_twice_is_stolen():
    """The reported case, working: the same plate, seen twice."""
    v = _verdict([_read("DL1ZA9092", at=0.0), _read("DL1ZA9092", at=1.0)])
    assert v.state == STOLEN
    assert v.plate == "DL1ZA9092"
    assert v.matches == "DL1ZA9092"
    assert v.reads == 2


def test_one_perfect_read_of_a_real_plate_is_enough():
    """A single read may stand alone, but only if every character is solid.

    The licence for this shortcut is narrow on purpose: 0.92 on the line *and*
    0.80 on every glyph *and* nothing ambiguous below that bar. Anything less and
    the read has to be corroborated by a second frame.
    """
    v = _verdict([_read("DL1ZA9092", conf=0.95, char_confs=ALL_HIGH)])
    assert v.state == STOLEN
    assert v.reads == 1


def test_a_single_read_below_the_confidence_bar_is_not_stolen():
    v = _verdict([_read("DL1ZA9092", conf=0.91, char_confs=ALL_HIGH)])
    assert v.state == POSSIBLE
    assert "uncorroborated" in v.reason


def test_a_single_read_with_a_weak_character_is_not_stolen():
    """The Z in DL1ZA9092, read with low confidence, is the whole ballgame."""
    weak = (0.99, 0.99, 0.99, 0.55, 0.99, 0.99, 0.99, 0.99, 0.99)
    v = _verdict([_read("DL1ZA9092", conf=0.97, char_confs=weak)])
    assert v.state == POSSIBLE
    assert v.ambiguous is True
    # ...but two frames of it is enough, because now it is corroborated.
    two = _verdict(
        [
            _read("DL1ZA9092", conf=0.97, at=0.0, char_confs=weak),
            _read("DL1ZA9092", conf=0.97, at=1.0, char_confs=weak),
        ]
    )
    assert two.state == STOLEN


def test_two_reads_too_far_apart_do_not_corroborate():
    """Consensus means the same car in two nearby frames.

    Two observations half a minute apart are not corroboration - by then it is
    more likely to be a different car than the same one. The read here is
    deliberately below the single-read bar (0.85 < 0.92) so that consensus is the
    *only* route to STOLEN, which is what this test is about.
    """
    far = _verdict(
        [_read("DL1ZA9092", conf=0.85, at=0.0), _read("DL1ZA9092", conf=0.85, at=30.0)],
        now=30.0,
    )
    assert far.state == POSSIBLE

    near = _verdict(
        [_read("DL1ZA9092", conf=0.85, at=0.0), _read("DL1ZA9092", conf=0.85, at=3.0)],
        now=3.0,
    )
    assert near.state == STOLEN


def test_a_single_very_strong_read_needs_no_second_frame():
    """The other half of the rule: a read this good is allowed to stand alone.

    Above the single-read bar with nothing doubtful about it, waiting for a
    second frame would only lose a stolen car that is driving away.
    """
    v = _verdict([_read("DL1ZA9092", conf=0.95)])
    assert v.state == STOLEN
    assert "single read" in v.reason


def test_the_reported_misread_is_never_stolen():
    """DL12AG092 - the plate the app actually read - must not alert."""
    v = _verdict([_read("DL12AG092", conf=0.93)])
    assert v.state == POSSIBLE
    assert v.state != STOLEN
    assert v.matches == "DL1ZA9092"


def test_the_reported_misread_repeated_is_still_not_stolen():
    """Even confirmed in two frames it is a different real car."""
    v = _verdict([_read("DL12AG092", at=0.0), _read("DL12AG092", at=1.0)])
    assert v.state == POSSIBLE
    assert v.matches == "DL1ZA9092"


def test_a_different_valid_plate_from_one_read_is_not_stolen():
    v = _verdict([_read("DL12A9092", conf=0.97)])
    assert v.state == POSSIBLE
    assert v.matches == "DL1ZA9092"


def test_a_different_valid_plate_confirmed_twice_is_still_possible():
    """Twice is not the point. Agreement with the hot-list is the point.

    If two frames agreed on DL12A9092 that is a car whose plate is DL12A9092,
    which is not the car that was reported stolen.
    """
    v = _verdict([_read("DL12A9092", at=0.0), _read("DL12A9092", at=1.0)])
    assert v.state == POSSIBLE


def test_a_short_tail_is_not_a_full_match():
    v = _verdict([_read("DL1ZA909", conf=0.97)])
    assert v.state == POSSIBLE


def test_an_impossible_state_code_is_unread_not_clear():
    """HL is not an RTO code. Claiming "not on the hot-list" for it would be a
    statement we cannot support."""
    v = _verdict([_read("HL09AB1234", conf=0.99)])
    assert v.state == UNREAD
    assert "HL" in v.reason


def test_hl09ab1234_is_never_stolen_even_twice():
    v = _verdict([_read("HL09AB1234", at=0.0), _read("HL09AB1234", at=1.0)])
    assert v.state == UNREAD


# --------------------------------------------------------------------------- #
# CLEAR and UNREAD
# --------------------------------------------------------------------------- #
def test_a_real_plate_on_an_empty_hotlist_is_clear():
    v = _verdict([_read("DL1ZA9092", conf=0.93)], hot=set())
    assert v.state == CLEAR


def test_no_read_at_all_is_unread():
    assert _verdict([]).state == UNREAD


def test_an_underscore_level_read_leaves_it_unread():
    v = _verdict([_read("DL12AG092", conf=0.20)])
    assert v.state == UNREAD


def test_a_valid_but_unlisted_plate_is_clear():
    v = _verdict([_read("MH12JK4567", conf=0.99)], hot={"DL1ZA9092"})
    assert v.state == CLEAR
    assert v.matches is None


# --------------------------------------------------------------------------- #
# Formatting variants
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,norm",
    [
        ("DL 1 ZA 9092", "DL1ZA9092"),
        ("dl1za9092", "DL1ZA9092"),
        ("DL-1-ZA-9092", "DL1ZA9092"),
        ("DL.1.ZA.9092", "DL1ZA9092"),
        ("  DL1ZA9092  ", "DL1ZA9092"),
        ("dL1zA9092", "DL1ZA9092"),
    ],
)
def test_spacing_hyphens_and_case_all_reach_the_same_plate(raw, norm):
    """The citizen typed it with spaces; the officer typed it in caps. Same car."""
    from app.services.plate import normalize_plate

    n = normalize_plate(raw)
    assert n.normalized == norm
    assert n.valid is True
    v = _verdict([_read(raw, norm=n.normalized, conf=0.99, char_confs=ALL_HIGH)])
    assert v.state == STOLEN


def test_a_two_row_plate_reads():
    from app.services.plate import normalize_plate

    n = normalize_plate("DL12 AB1234")
    assert n.normalized == "DL12AB1234"
    v = _verdict([_read("DL12 AB1234", norm=n.normalized, conf=0.95)])
    assert v.state == CLEAR


# --------------------------------------------------------------------------- #
# Strict syntax and the grammar prior
# --------------------------------------------------------------------------- #
def test_strict_syntax_rejects_a_fake_state_code():
    ok, why, _score = strict_syntax("HL09AB1234")
    assert ok is False
    assert "HL" in why


def test_strict_syntax_prefers_the_common_layout_without_forbidding_the_rest():
    ok, _why, four_digit = strict_syntax("MH12AB1234")
    assert ok and four_digit == 1.0
    ok, _why, two_digit = strict_syntax("MH12AB12")
    assert ok and two_digit < 1.0
    # Legal, just less common.
    ok, _why, one_digit = strict_syntax("DL8CBF4890")
    assert ok is True
    assert one_digit < 1.0


def test_the_layout_prior_slows_down_an_unusual_read():
    usual = grammar_adjusted_confidence("DL01ZA9092", 0.97)
    unusual = grammar_adjusted_confidence("DL1ZA9", 0.97)
    assert unusual < usual


# --------------------------------------------------------------------------- #
# Fuzzy matching
# --------------------------------------------------------------------------- #
def test_confusable_characters_score_close_but_not_identical():
    assert fuzzy_score("DL12AG092", "DL1ZA9092") >= FUZZY_THRESHOLD
    assert fuzzy_score("DL12AG092", "DL1ZA9092") < 1.0


def test_a_different_state_never_matches_however_similar():
    # Same shape, same letters, different RTO: a different state, and the amber
    # banner has to mean "that specific listed car" to be worth showing.
    near, _score = best_fuzzy_match("MH12AG092", {"MH1ZA9092", "DL1ZA9092"})
    assert near is None or near.startswith("MH")
    assert best_fuzzy_match("KL12AG092", {"DL1ZA9092"})[0] is None


def test_an_unrelated_plate_is_not_possible():
    assert best_fuzzy_match("MH12JK4567", {"DL1ZA9092"})[0] is None


# --------------------------------------------------------------------------- #
# Ambiguity
# --------------------------------------------------------------------------- #
def test_the_confusable_glyphs_are_the_documented_ones():
    for ch in "Z2O0I1S5B8G6G9D0Q0MHMN":
        assert ch in AMBIGUOUS_CHARS


def test_no_per_character_data_means_no_specific_doubt():
    """The installed RapidOCR can give per-character scores, but a caller that
    has none must not be treated as if every character were doubtful."""
    v = _verdict([_read("DL1ZA9092", conf=0.95, char_confs=None)])
    assert v.state == STOLEN


# --------------------------------------------------------------------------- #
# The history itself
# --------------------------------------------------------------------------- #
def test_at_most_five_reads_are_kept():
    tv = TrackVerdict(1)
    for i in range(20):
        tv.add(_read("DL1ZA9092", conf=0.6, at=float(i)))
    assert len(tv.reads) == MAX_READS


def test_the_best_reads_are_the_ones_kept():
    """A track that was unreadable and then came into focus should not lose its
    good read to a pile of junk."""
    tv = TrackVerdict(1)
    tv.add(_read("DL1ZA9092", conf=0.99, at=0.0))
    for i in range(1, 12):
        tv.add(_read("DL12AG092", conf=0.30, at=float(i)))
    assert any(r.conf == 0.99 for r in tv.reads)


def test_a_confirmed_track_does_not_need_more_reads():
    tv = TrackVerdict(1)
    tv.add(_read("DL1ZA9092", conf=0.93, at=0.0))
    assert tv.has_confirmed is False
    tv.add(_read("DL1ZA9092", conf=0.93, at=1.0))
    assert tv.has_confirmed is True


def test_reads_in_the_same_frame_are_still_two_observations():
    """Two variants of one frame agreeing is weaker evidence than two frames,
    but it is not nothing - and refusing it outright would throw away the only
    evidence available on a car that is leaving the frame."""
    tv = TrackVerdict(1)
    tv.add(_read("DL1ZA9092", conf=0.9, at=5.0))
    tv.add(_read("DL1ZA9092", conf=0.9, at=5.0))
    assert len({r.at for r in tv.reads}) == 1
    # The verdict still works, and does not claim more than it knows.
    assert tv.evaluate(HOT).state in (STOLEN, POSSIBLE)


def test_the_wire_shape_carries_everything_the_phone_needs():
    d = _verdict([_read("DL1ZA9092", at=0.0), _read("DL1ZA9092", at=1.0)]).as_dict()
    for key in ("state", "plate", "confidence", "reads", "matches", "reason"):
        assert key in d
    assert d["state"] == STOLEN
    assert isinstance(d["confidence"], float)


def test_time_moving_on_does_not_change_a_verdict():
    reads = [_read("DL1ZA9092", at=0.0), _read("DL1ZA9092", at=1.0)]
    v = _verdict(reads, now=1.0)
    later = TrackVerdict(1, now=1.0)
    for r in reads:
        later.add(r)
    assert later.evaluate(HOT, now=time.monotonic()).state == v.state