from pathlib import Path

import pytest

from ai.detector import resolve_model_path
from ai.ocr_engine import OcrHit, drop_strip_artifacts, merge_line_hits
from app.services.plate import (
    VALID_STATE_CODES,
    is_valid_plate,
    normalize_plate,
    normalize_plate_for_entry,
    pad_short_plate,
    plate_key,
    plate_read_reliable,
    plate_spellings,
)


def test_model_path_points_to_trained_plate_model():
    model_path = resolve_model_path()
    assert Path(model_path).name == "license-plate-finetune-v1n.pt"
    assert Path(model_path).exists()


def test_norm_hyphens_spaces():
    p = normalize_plate("ts-09 ab 1234")
    assert p.valid is True
    assert p.normalized == "TS09AB1234"


def test_norm_dots_and_slashes():
    p = normalize_plate("TS.09.AB/1234")
    assert p.valid is True
    assert p.normalized == "TS09AB1234"


def test_norm_already_clean():
    assert normalize_plate("KA01AB1234").normalized == "KA01AB1234"


# --------------------------------------------------------------------------- #
# the district block, written two ways
# --------------------------------------------------------------------------- #
def test_key_drops_the_district_leading_zero():
    """`01` and `1` are the same RTO office, so they are the same car."""
    assert plate_key("DL01ZA9092") == "DL1ZA9092"
    assert plate_key("DL1ZA9092") == "DL1ZA9092"


def test_key_leaves_a_two_digit_district_without_a_zero_alone():
    assert plate_key("MH12AB1234") == "MH12AB1234"
    assert plate_key("UP32XA9801") == "UP32XA9801"


def test_key_is_the_same_under_either_spelling():
    assert plate_key("KA01AB1234") == plate_key("KA1AB1234")


def test_key_leaves_non_plates_unchanged():
    """Called on a partial read or nothing at all, it must not raise."""
    assert plate_key("") == ""
    assert plate_key(None) == ""
    assert plate_key("DL1ZA") == "DL1ZA"


def test_spellings_include_both_forms_for_a_padded_district():
    assert set(plate_spellings("DL01ZA9092")) == {"DL01ZA9092", "DL1ZA9092"}
    assert set(plate_spellings("DL1ZA9092")) == {"DL01ZA9092", "DL1ZA9092"}


def test_spellings_of_an_unpadded_district_are_just_itself():
    assert plate_spellings("MH12AB1234") == ("MH12AB1234",)


def test_spellings_always_contain_the_key():
    """The cache stores keys and a lookup uses spellings, so they must overlap."""
    for plate in ("DL01ZA9092", "DL1ZA9092", "KA01AB1234", "MH12AB1234", "UP32XA9801"):
        assert plate_key(plate) in plate_spellings(plate)


def test_spellings_never_merge_two_different_cars():
    """Only the district's leading zero is insignificant - nothing else.

    The tail digit is a different car, and the district is a different office.
    """
    assert set(plate_spellings("DL01ZA9092")) & set(plate_spellings("DL01ZA9093")) == set()
    assert set(plate_spellings("DL01ZA9092")) & set(plate_spellings("DL02ZA9092")) == set()


def test_a_district_without_a_leading_zero_has_only_one_spelling():
    """`12` has no zero to drop, so `TN12AB3456` is not `TN1AB3456`.

    The equivalence is the *leading zero* of the district block, not "one or two
    digits are interchangeable" - district 12 and district 1 are different RTOs
    and different cars.
    """
    assert plate_spellings("TN12AB3456") == ("TN12AB3456",)
    assert set(plate_spellings("TN12AB3456")) & set(plate_spellings("TN1AB3456")) == set()
    assert plate_spellings("MH12AB1234") == ("MH12AB1234",)


def test_spellings_of_a_non_plate_come_back_as_itself():
    assert plate_spellings("junk") == ("junk",)
    assert plate_spellings(None) == ()


def test_valid_plate_forms():
    for raw in ["KA01AB1234", "MH12JK4567", "UP32XA9801", "DL8CBF4890", "TS09AB1234"]:
        assert normalize_plate(raw).valid is True, raw


def test_invariant_lowercase():
    assert normalize_plate("ts09ab1234").normalized == "TS09AB1234"


def test_invalid_plate_short():
    assert normalize_plate("AB12").valid is False


def test_invalid_plate_no_series_letters():
    # cleaned form may still fail the pattern when letters are missing
    assert normalize_plate("TS091234").valid is False


def test_none_plate_invalid():
    assert normalize_plate(None).valid is False


def test_empty_plate_invalid():
    assert normalize_plate("").valid is False


def test_leading_trailing_whitespace():
    assert normalize_plate("  ts09ab1234  ").normalized == "TS09AB1234"


def test_normalizes_common_indian_plate_variants():
    for raw in [
        "TG11AB1234",
        "TG 11 AB 1234",
        "TG-11-AB-1234",
        "TG11 AB1234",
        "TG 11AB 1234",
        "TG11AB-1234",
    ]:
        assert normalize_plate(raw).valid is True
        assert normalize_plate(raw).normalized == "TG11AB1234"


def test_corrects_position_aware_ocr_confusion():
    assert normalize_plate("TG11A81234").normalized == "TG11AB1234"
    assert normalize_plate("TG11O81234").normalized == "TG11OB1234"


# --- state-code whitelist -------------------------------------------------


def test_every_whitelisted_state_code_validates():
    for code in sorted(VALID_STATE_CODES):
        assert is_valid_plate(f"{code}12AB1234") is True, code


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("MH12AB1234", "MH12AB1234"),
        ("MH01CD5678", "MH01CD5678"),
        ("TS09AB1234", "TS09AB1234"),
        ("DL3CAB1234", "DL3CAB1234"),
        ("KA05MN1234", "KA05MN1234"),
    ],
)
def test_real_plates_still_validate(raw, expected):
    p = normalize_plate(raw)
    assert p.valid is True
    assert p.normalized == expected
    assert p.repaired is False


def test_invalid_state_code_is_rejected():
    # "HL" is not an Indian RTO: no such state or union territory exists.
    p = normalize_plate("HL09AB1234")
    assert p.valid is False
    assert is_valid_plate(p.normalized) is False


@pytest.mark.parametrize("raw", ["ZZ09AB1234", "XX09AB1234", "KN09AB1234", "QQ12AB1234"])
def test_other_fake_state_codes_are_rejected(raw):
    assert normalize_plate(raw).valid is False


def test_ocr_noise_cases_are_repaired():
    assert normalize_plate("MH12A81234").normalized == "MH12AB1234"
    assert normalize_plate("MHI2AB1234").normalized == "MH12AB1234"
    # NH is not a valid code, so the N/M confusion is fixed in favour of MH.
    assert normalize_plate("NH12AB1234").normalized == "MH12AB1234"


def test_a_valid_read_is_never_overwritten_by_a_prettier_layout():
    # "MH1ZAB1234" and "MH12AB1234" are *both* well-formed plates of two
    # different real cars. Preferring the dominant 2-digit-RTO layout used to
    # rewrite the first into the second, which reported a stolen car as an
    # innocent one. An exact read now always survives: only reads that are not
    # plates at all (MH12A81234, MHI2AB1234, NH...) get repaired.
    p = normalize_plate("MH1ZAB1234")
    assert p.valid is True
    assert p.normalized == "MH1ZAB1234"
    assert p.repaired is False


def test_two_character_rewrites_never_win_over_an_exact_read():
    # The reported case: the plate is DL 1 ZA 9092 and the reader produced
    # DL12AG092 by reading Z as 2 and 9 as G. That is a different, valid plate.
    for raw in ("DL 1 ZA 9092", "DL1ZA9092", "dl1za9092"):
        p = normalize_plate(raw)
        assert p.normalized == "DL1ZA9092", raw
        assert p.repaired is False, raw
    # A short tail is still a plate; it is not padded out to four digits.
    assert normalize_plate("DL1ZA909").normalized == "DL1ZA909"


def test_camera_path_never_pads_a_short_read():
    # The safety rule above is about the camera, and stays true no matter what
    # the complaint form does: a read is reported as read.
    for raw in ("DL1ZA909", "TS1A12", "TS1A123"):
        p = normalize_plate(raw)
        assert p.normalized == raw, raw
        assert p.repaired is False, raw


def test_complaint_entry_pads_a_loosely_typed_plate():
    # The reported case: a citizen types "ts 1 a 12" and means TS 01 A 0012.
    assert normalize_plate_for_entry("ts 1 a 12").normalized == "TS01A0012"
    assert normalize_plate_for_entry("TS1A12").normalized == "TS01A0012"
    assert normalize_plate_for_entry("TS1AB12").normalized == "TS01AB0012"
    assert normalize_plate_for_entry("ts1a12").normalized == "TS01A0012"
    for raw in ("ts 1 a 12", "TS1A12", "TS1AB12"):
        p = normalize_plate_for_entry(raw)
        assert p.valid is True, raw
        assert p.repaired is True, raw


def test_entry_padding_keeps_letters_exactly_as_typed():
    # Padding widens digit runs only; it never invents a series letter, because
    # a longer series is a different car, not a longer spelling of this one.
    assert normalize_plate_for_entry("TS1A12").normalized == "TS01A0012"
    assert normalize_plate_for_entry("TS1AB12").normalized == "TS01AB0012"


def test_entry_leaves_a_complete_plate_alone():
    # A registration already written in full is never rewritten - the display
    # form of an existing plate is not this function's business.
    for raw in ("TS11A1234", "KA01AB1234", "DL8CBF4890", "DL1ZA9092", "DL1ZA909"):
        p = normalize_plate_for_entry(raw)
        assert p.normalized == raw, raw
        assert p.valid is True, raw


def test_entry_still_rejects_what_is_not_a_plate():
    # Too short to complete, missing the series letters, or a bogus state code.
    for raw in ("AB12", "TS091234", "HL09AB1234", ""):
        assert normalize_plate_for_entry(raw).valid is False, raw


def test_pad_short_plate_returns_none_when_it_cannot_help():
    assert pad_short_plate("TS091234") is None      # no series letters
    assert pad_short_plate("AB12") is None          # below the floor
    assert pad_short_plate("TS1A12345") is None     # tail too long
    assert pad_short_plate("1234A12") is None       # state code is not letters
    assert pad_short_plate("TS11A1234") is None     # already complete
    assert pad_short_plate("TS1A12") == "TS01A0012"


def test_entry_rejects_a_bogus_state_code_even_when_padding():
    # Padding must not resurrect an un-whitelisted code.
    assert normalize_plate_for_entry("ZL1A12").valid is False


def test_ambiguous_state_code_is_never_guessed():
    # HL can be read as ML (Meghalaya) or NL (Nagaland): two plausible answers,
    # so the whitelist rejects it instead of inventing one.
    p = normalize_plate("HL09AB1234")
    assert p.valid is False
    assert p.normalized == "HL09AB1234"
    assert "ambiguous" in p.reason


def test_repaired_state_code_is_flagged_as_needing_corroboration():
    p = normalize_plate("NH12AB1234")
    assert p.valid is True
    assert p.repaired is True


def test_damaged_state_code_glyph_is_not_guessed():
    # "M1" could be a clipped MH or a genuine ML - never silently become ML.
    p = normalize_plate("M12JK4867")
    assert p.valid is False
    assert p.normalized != "ML2JK4867"


def test_ind_strip_fragment_is_dropped():
    for raw, expected in [
        ("IND MH12AB1234", "MH12AB1234"),
        ("INDMH12AB1234", "MH12AB1234"),
        ("INDIA MH 12 JK 4567", "MH12JK4567"),
    ]:
        p = normalize_plate(raw)
        assert p.valid is True
        assert p.normalized == expected


def test_lowercase_and_punctuation_variants():
    for raw in ["mh 12 jk 4567", "mh-12-jk-4567", "MH.12.JK.4567", "  MH12JK4567  "]:
        p = normalize_plate(raw)
        assert p.valid is True
        assert p.normalized == "MH12JK4567"


def test_reliability_gate():
    assert plate_read_reliable("MH12AB1234", 0.95) is True
    assert plate_read_reliable("MH12AB1234", 0.60) is False  # below 0.85
    assert plate_read_reliable("HL09AB1234", 0.99) is False  # invalid state code
    assert plate_read_reliable("MH12AB1234", None) is False


# --- merge logic ----------------------------------------------------------


def _hit(text, x0, x1, y0=0, y1=20, score=0.9):
    return OcrHit(
        text=text,
        normalized=text,
        valid_plate=False,
        score=score,
        box=[[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
    )


def test_merge_orders_fragments_left_to_right_by_x():
    # Deliberately supplied out of order: the state code must end up first.
    hits = [_hit("4567", 300, 400), _hit("12", 120, 200), _hit("MH", 0, 100), _hit("JK", 210, 290)]
    merged = merge_line_hits(hits)
    assert merged[0].text == "MH12JK4567"
    assert merged[0].normalized == "MH12JK4567"
    assert merged[0].valid_plate is True


def test_merge_retry_keeps_the_leading_state_fragment():
    # The stray "XX" makes the full merge too long, so a retry drops one fragment.
    # It must drop the stray, never the leading "MH": losing the state code is
    # what turned a correct plate into an unreadable one.
    hits = [
        _hit("MH", 0, 100),
        _hit("12", 100, 200),
        _hit("XX", 200, 260),
        _hit("AB", 260, 340),
        _hit("1234", 340, 520),
    ]
    valid = [h for h in merge_line_hits(hits) if h.valid_plate]
    assert valid, "dropping the stray fragment should yield a valid plate"
    assert valid[0].text.startswith("MH")
    assert valid[0].normalized == "MH12AB1234"


def test_drop_strip_artifacts_removes_left_ind_band():
    hits = [_hit("IND", 0, 60), _hit("MH12JK4567", 80, 600)]
    assert [h.text for h in drop_strip_artifacts(hits)] == ["MH12JK4567"]


def test_drop_strip_artifacts_keeps_wide_left_fragment():
    # A wide left fragment is real plate text, not a blue-band artifact.
    hits = [_hit("MH12", 0, 200), _hit("JK4567", 210, 600)]
    assert len(drop_strip_artifacts(hits)) == 2