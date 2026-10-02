/**
 * RAKSHAK mobile design tokens.
 *
 * This is the single source of truth for the Expo app. The two web clients
 * declare the same palette in their (byte-identical) tailwind.config.js, so a
 * plate that reads as "danger red" in the console reads as "danger red" on the
 * volunteer's phone.
 *
 * Only values, never components: importing this file must not pull React or
 * React Native into a module that only needs a colour.
 */

export const lightColors = {
  // Primary: deep indigo/navy. 600 is the main action colour.
  primary: {
    50: "#EEF1FF",
    100: "#E0E5FF",
    200: "#C7CFFF",
    300: "#A3AEFF",
    400: "#7C85F5",
    500: "#5A61E4",
    600: "#2F4BD8",
    700: "#2539AE",
    800: "#1D2C87",
    900: "#0B1437",
  },
  // Accent: teal/cyan for active states and highlights.
  accent: {
    50: "#ECFEFF",
    100: "#CFFAFE",
    200: "#A5F3FC",
    300: "#67E8F9",
    400: "#22D3EE",
    500: "#06B6D4",
    600: "#0891B2",
    700: "#0E7490",
    800: "#155E75",
    900: "#164E63",
  },
  // Neutrals.
  surface: {
    bg: "#F8FAFC",
    card: "#FFFFFF",
    border: "#E2E8F0",
    text: "#0F172A",
    muted: "#64748B",
    subtle: "#94A3B8",
    raised: "#FFFFFF",
  },
  // Dark surfaces: the scanner chrome is dark so the plate stays the brightest
  // thing on screen.
  dark: {
    900: "#0B1220",
    800: "#111A2E",
    700: "#1B2742",
    600: "#27354F",
    500: "#3A4A68",
  },
  // Semantic status colours.
  success: "#16A34A",
  warning: "#F59E0B",
  danger: "#DC2626",
  info: "#0EA5E9",
  // Solid action fills. Kept dark in BOTH themes so white label text is always
  // AA - matches the `solid` token in both web clients.
  solid: "#2F4BD8",
  solidHover: "#2539AE",
  solidDanger: "#B91C1C",
  solidDangerHover: "#991B1B",
  // Text colours that sit on the dark surfaces above.
  onDark: {
    strong: "#F8FAFC",
    body: "#E2E8F0",
    muted: "#94A3B8",
    faint: "#64748B",
  },
};

export const darkColors = {
  primary: {
    50: "#111A2E",
    100: "#1B2742",
    200: "#27354F",
    300: "#5A61E4",
    400: "#7C85F5",
    500: "#A3AEFF",
    600: "#C7CFFF",
    700: "#E0E5FF",
    800: "#EEF1FF",
    900: "#FFFFFF",
  },
  accent: {
    50: "#0F2635",
    100: "#164E63",
    200: "#155E75",
    300: "#22D3EE",
    400: "#67E8F9",
    500: "#A5F3FC",
    600: "#CFFAFE",
    700: "#ECFEFF",
    900: "#FFFFFF",
  },
  surface: {
    bg: "#0B1220",
    card: "#111A2E",
    border: "#263552",
    text: "#E6EDF7",
    muted: "#94A3B8",
    subtle: "#94A3B8",
    raised: "#1B2742",
  },
  dark: {
    900: "#0B1220",
    800: "#111A2E",
    700: "#1B2742",
    600: "#263552",
    500: "#3A4A68",
  },
  success: "#34D399",
  warning: "#FBBF24",
  danger: "#F87171",
  info: "#38BDF8",
  solid: "#3B58E8",
  solidHover: "#2F4BD8",
  solidDanger: "#7F1D1D",
  solidDangerHover: "#671616",
  onDark: {
    strong: "#F8FAFC",
    body: "#E2E8F0",
    muted: "#94A3B8",
    faint: "#64748B",
  },
};

const colors = lightColors;

/**
 * Complaint status mapping. Identical to STATUS_STYLES in the web
 * components/ui/StatusChip.tsx so a complaint never changes colour between
 * the citizen portal, the console and this app. Only the chip background and
 * foreground differ per theme; the label never does.
 */
const STATUS_LABELS = {
  PENDING: "Pending",
  UNDER_REVIEW: "Under review",
  VERIFIED: "Verified",
  REJECTED: "Rejected",
  HOTLISTED: "Hotlisted",
  CLOSED: "Closed",
};

export const STATUS_TOKENS = {
  PENDING: { bg: "#FEF3C7", text: "#78350F", label: STATUS_LABELS.PENDING },
  UNDER_REVIEW: { bg: "#E0F2FE", text: "#075985", label: STATUS_LABELS.UNDER_REVIEW },
  VERIFIED: { bg: "#DCFCE7", text: "#14532D", label: STATUS_LABELS.VERIFIED },
  REJECTED: { bg: "#FEE2E2", text: "#7F1D1D", label: STATUS_LABELS.REJECTED },
  HOTLISTED: { bg: "#E6EAFF", text: "#1D2C87", label: STATUS_LABELS.HOTLISTED },
  CLOSED: { bg: "#EEF2F7", text: "#334155", label: STATUS_LABELS.CLOSED },
};

const STATUS_TOKENS_DARK = {
  PENDING: { bg: "#453011", text: "#FCD34D", label: STATUS_LABELS.PENDING },
  UNDER_REVIEW: { bg: "#0C3350", text: "#7DD3FC", label: STATUS_LABELS.UNDER_REVIEW },
  VERIFIED: { bg: "#0F3A26", text: "#86EFAC", label: STATUS_LABELS.VERIFIED },
  REJECTED: { bg: "#4A1519", text: "#FCA5A5", label: STATUS_LABELS.REJECTED },
  HOTLISTED: { bg: "#24305C", text: "#C7CFFF", label: STATUS_LABELS.HOTLISTED },
  CLOSED: { bg: "#1B2742", text: "#CBD5E1", label: STATUS_LABELS.CLOSED },
};

/** Status chip tokens for the active theme. */
export function statusTokens(isDark) {
  return isDark ? STATUS_TOKENS_DARK : STATUS_TOKENS;
}

/** 4 / 8 / 12 / 16 / 24 / 32 base scale. */
export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  xxl: 32,
};

export const radius = {
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  pill: 999,
};

export const fontSize = {
  xs: 11,
  sm: 13,
  base: 15,
  lg: 17,
  xl: 20,
  xxl: 26,
  display: 32,
};

export const fontWeight = {
  regular: "400",
  medium: "500",
  semibold: "600",
  bold: "700",
  heavy: "800",
};

/** iOS ships a system font whose metrics match Roboto closely enough. */
export const fontFamily = {
  regular: undefined,
  mono: "monospace",
};

export const shadow = {
  card: {
    shadowColor: "#0F172A",
    shadowOpacity: 0.08,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 2 },
    elevation: 2,
  },
  raised: {
    shadowColor: "#0F172A",
    shadowOpacity: 0.16,
    shadowRadius: 16,
    shadowOffset: { width: 0, height: 6 },
    elevation: 6,
  },
  overlay: {
    shadowColor: "#0B1437",
    shadowOpacity: 0.32,
    shadowRadius: 24,
    shadowOffset: { width: 0, height: 12 },
    elevation: 12,
  },
};

/** Confidence thresholds, shared with the scanner's copy. */
export const confidence = {
  good: 0.85,
  fair: 0.6,
};

export function confidenceTone(value) {
  if (value >= confidence.good) return "success";
  if (value >= confidence.fair) return "warning";
  return "danger";
}

/**
 * Colour for a confidence value. Pass the active palette so the bar follows the
 * theme; it defaults to the light palette for existing call sites.
 */
export function confidenceColor(value, palette = lightColors) {
  if (value >= confidence.good) return palette.success;
  if (value >= confidence.fair) return palette.warning;
  return palette.danger;
}

export default {
  colors,
  lightColors,
  darkColors,
  spacing,
  radius,
  fontSize,
  fontWeight,
  fontFamily,
  shadow,
  confidence,
  confidenceTone,
  confidenceColor,
  statusTokens,
  STATUS_TOKENS,
  STATUS_TOKENS_DARK,
};
