import re
from dataclasses import dataclass

INDIAN_PLATE_PATTERN = re.compile(r"^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4}$")


@dataclass
class NormalizedPlate:
    raw: str
    normalized: str
    valid: bool


def normalize_plate(raw: str) -> NormalizedPlate:
    """Normalize an Indian-style plate string.

    - uppercases
    - strips spaces, hyphens, dots
    - positional OCR fixes only (e.g. O->0 in the numeric region,
      I/l->1 in the numeric region, S->5 is NOT applied blindly)
    """
    if raw is None:
        return NormalizedPlate(raw="", normalized="", valid=False)

    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(raw)).upper()

    if len(cleaned) < 8 or len(cleaned) > 10:
        return NormalizedPlate(raw=str(raw), normalized=cleaned, valid=False)

    # Split into alpha-state and digit-state runs to apply positional corrections.
    # Indian plates: 2 letters (state) + 1-2 digits (RTO) + 1-3 letters (series) + 1-4 digits (number)
    chars = list(cleaned)

    # Region 1: first 2 chars should be letters (state code)
    for i in range(min(2, len(chars))):
        if chars[i].isdigit():
            chars[i] = {"0": "O", "1": "I"}.get(chars[i], chars[i])

    # Find where digits begin (RTO code)
    idx = 2
    while idx < len(chars) and not chars[idx].isdigit():
        idx += 1

    # Region 2: RTO digits — map letter-like OCR mistakes to digits
    rto_start = idx
    while idx < len(chars) and chars[idx].isalpha():
        chars[idx] = {"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "G": "6", "Z": "2", "T": "7"}.get(
            chars[idx], chars[idx]
        )
        idx += 1

    # Region 3: series letters — map digit-like OCR mistakes to letters
    while idx < len(chars) and chars[idx].isdigit():
        idx += 1
    series_start = idx
    while idx < len(chars) and chars[idx].isalpha():
        chars[idx] = {"0": "O", "1": "I", "5": "S", "8": "B"}.get(chars[idx], chars[idx])
        idx += 1

    # Region 4: trailing number — map letter-like OCR mistakes to digits
    while idx < len(chars):
        if chars[idx].isalpha():
            chars[idx] = {"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "G": "6", "Z": "2", "T": "7"}.get(
                chars[idx], chars[idx]
            )
        idx += 1

    normalized = "".join(chars)
    valid = bool(INDIAN_PLATE_PATTERN.match(normalized))
    return NormalizedPlate(raw=str(raw), normalized=normalized, valid=valid)


def confidence_ok(confidence: float | None, threshold: float = 0.5) -> bool:
    if confidence is None:
        return False
    return confidence >= threshold
