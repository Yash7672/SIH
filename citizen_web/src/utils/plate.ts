/**
 * Client-side mirror of the backend's complaint-entry plate rules.
 *
 * A citizen typing their own registration often leaves the leading zeros out -
 * "TS1A12" is how the number is spoken, "TS01A0012" is how it is registered.
 * The backend completes that form (``app/services/plate.py``,
 * ``normalize_plate_for_entry``) and this module exists so the form can show the
 * completed value *before* the request, instead of silently rewriting it after.
 *
 * The rules are duplicated rather than fetched because the form has to react on
 * every keystroke, and a round trip per character is not worth it. The lists are
 * intentionally kept small and identical to the server's:
 *   <2-letter state><1-2 digit district><1-3 letters><4 digits>
 *
 * Keep this in step with backend/app/services/plate.py. The server remains the
 * authority - anything this lets through is still validated again on arrival.
 */

/** State and union-territory codes the backend whitelists (plus the BH series). */
const VALID_STATE_CODES = new Set([
  "AN", "AP", "AR", "AS", "BR", "BH", "CH", "CG", "DD", "DL", "DN", "GA", "GJ",
  "HP", "HR", "JH", "JK", "KA", "KL", "LA", "LD", "MH", "ML", "MN", "MP", "MZ",
  "NL", "OD", "OR", "PB", "PY", "RJ", "SK", "TG", "TN", "TR", "TS", "UK", "UP",
  "WB",
]);

/** Digits a state-code slot could really be, when the glyph was clipped. */
const STATE_DIGIT_LETTERS: Record<string, string> = {
  "0": "O", "1": "I", "2": "Z", "3": "B", "4": "A",
  "5": "S", "6": "G", "7": "T", "8": "B", "9": "G",
};

const LETTER_TO_DIGIT: Record<string, string> = {
  O: "0", I: "1", L: "1", S: "5", B: "8", Z: "2", G: "6", T: "7",
};

/** Canonical widths: a district is two digits, a tail number is four. */
const RTO_DIGITS_WIDTH = 2;
const TAIL_DIGITS_WIDTH = 4;

/** The shortest string we will try to complete. Below this it is not a plate. */
export const PLATE_ENTRY_MIN_LEN = 5;
/** Longest input accepted, matching the backend's PLATE_MAX_LEN. */
export const PLATE_ENTRY_MAX_LEN = 10;

export function stripPlate(raw: string): string {
  return (raw || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
}

/**
 * Complete a loosely typed registration to its canonical form.
 *
 * Returns ``null`` when the string cannot be completed - the caller then shows
 * the ordinary "check the number" message rather than inventing a plate.
 */
export function padPlate(raw: string): string | null {
  const compact = stripPlate(raw);
  if (compact.length < PLATE_ENTRY_MIN_LEN) return null;

  // Repair a state code whose letters were typed as their similar-looking
  // digits ("7S11AB1234" -> "TS11AB1234"), exactly as the backend does.
  let head = compact.slice(0, 2);
  if (!/^[A-Z]{2}$/.test(head)) {
    head = head
      .split("")
      .map((ch) => (/\d/.test(ch) ? STATE_DIGIT_LETTERS[ch] ?? ch : ch))
      .join("");
  }
  if (!/^[A-Z]{2}$/.test(head) || !VALID_STATE_CODES.has(head)) return null;

  // The body alternates digits, letters, digits - that shape is what makes the
  // remainder a plate rather than an arbitrary string.
  const parts = compact.slice(2).match(/[0-9]+|[A-Z]+/g);
  if (!parts || parts.length !== 3) return null;
  const [rtoRaw, seriesRaw, tailRaw] = parts;
  if (!/^\d+$/.test(rtoRaw) || !/^[A-Z]+$/.test(seriesRaw) || !/^\d+$/.test(tailRaw)) {
    return null;
  }
  if (rtoRaw.length > RTO_DIGITS_WIDTH || tailRaw.length > TAIL_DIGITS_WIDTH) return null;
  if (seriesRaw.length < 1 || seriesRaw.length > 3) return null;

  const series = seriesRaw
    .split("")
    .map((ch) => (/\d/.test(ch) ? LETTER_TO_DIGIT[ch] ?? ch : ch))
    .join("");

  return `${head}${rtoRaw.padStart(RTO_DIGITS_WIDTH, "0")}${series}${tailRaw.padStart(TAIL_DIGITS_WIDTH, "0")}`;
}

/** True when the string already looks like a complete registration. */
export function isCompletePlate(raw: string): boolean {
  const compact = stripPlate(raw);
  return /^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{4}$/.test(compact) && VALID_STATE_CODES.has(compact.slice(0, 2));
}

/**
 * One line of guidance under the field, plus the completed value when the
 * entries differ so the citizen can see what will be recorded.
 */
export function plateEntryHint(raw: string): string {
  const compact = stripPlate(raw);
  if (compact.length === 0) {
    return "Format: state code, district number, series letters, number — e.g. TS11A1234 or DL01ZA9092.";
  }
  const padded = padPlate(compact);
  if (padded && padded !== compact) {
    return `Will be recorded as ${padded}.`;
  }
  if (isCompletePlate(compact)) {
    return "Looks complete.";
  }
  return "Add the missing characters, or check the state code.";
}
