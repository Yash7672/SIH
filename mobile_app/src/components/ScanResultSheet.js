import { memo, useEffect, useMemo, useRef, useState } from "react";
import {
  Animated,
  Easing,
  LayoutAnimation,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { confidenceColor, fontSize, fontWeight, radius, shadow, spacing } from "../theme";
import { useTheme } from "../theme/ThemeContext";
import Button from "./ui/Button";

/** A plate rendered the way it looks on the vehicle: black on white, boxed. */
export function PlateBadge({ plate, size = "md", style }) {
  const dims = {
    sm: { fontSize: fontSize.lg, paddingVertical: 6, paddingHorizontal: spacing.md },
    md: { fontSize: fontSize.xxl, paddingVertical: spacing.sm, paddingHorizontal: spacing.lg },
  }[size];

  return (
    <View style={[staticStyles.plateBadge, dims, style]}>
      <Text style={[staticStyles.plateText, { fontSize: dims.fontSize }]} numberOfLines={1} adjustsFontSizeToFit>
        {plate || "—"}
      </Text>
    </View>
  );
}

function ConfidenceBar({ value, palette }) {
  const width = useRef(new Animated.Value(0)).current;
  const pct = Math.max(0, Math.min(1, Number(value) || 0));

  useEffect(() => {
    Animated.timing(width, {
      toValue: pct,
      duration: 420,
      easing: Easing.out(Easing.cubic),
      useNativeDriver: false,
    }).start();
  }, [pct, width]);

  const color = confidenceColor(pct, palette);

  return (
    <View
      accessibilityRole="progressbar"
      accessibilityValue={{ now: Math.round(pct * 100), min: 0, max: 100 }}
      style={[staticStyles.barTrack, { backgroundColor: palette.dark[600] }]}
    >
      <Animated.View
        style={[
          staticStyles.barFill,
          {
            backgroundColor: color,
            width: width.interpolate({ inputRange: [0, 1], outputRange: ["0%", "100%"] }),
          },
        ]}
      />
    </View>
  );
}

const STATE = {
  success: { title: "Plate reported", tone: "success" },
  uncertain: { title: "Not sure — scan again", tone: "warning" },
  none: { title: "No plate found", tone: "info" },
  network: { title: "Network error", tone: "danger" },
  session: { title: "Session expired", tone: "danger" },
  forbidden: { title: "Not allowed", tone: "danger" },
  server: { title: "Server error", tone: "danger" },
};

const TIPS = [
  "Move closer to the plate",
  "Hold the phone steady",
  "Avoid glare and shadows",
  "Fill the frame with the plate",
];

/**
 * Result panel that slides up from the bottom after a scan.
 *
 * Purely presentational and memoised: it receives a plain result object, so a
 * parent re-render (log line, health check) does not re-run any of this. It
 * draws on the scanner's permanently-dark chrome, so no watermark sits over it.
 */
function ScanResultSheetBase({ result, onDismiss, onRescan, onDetailsLongPress }) {
  const { colors } = useTheme();
  const slide = useRef(new Animated.Value(0)).current;
  const [detailsOpen, setDetailsOpen] = useState(false);

  useEffect(() => {
    Animated.timing(slide, {
      toValue: result ? 1 : 0,
      duration: 240,
      easing: Easing.out(Easing.cubic),
      useNativeDriver: true,
    }).start();
  }, [result, slide]);

  const s = useMemo(() => makeStyles(colors), [colors]);

  if (!result) return null;

  const state = STATE[result.state] || STATE.success;
  const hotlist = Boolean(result.hotlist);

  return (
    <Animated.View
      accessibilityViewIsModal
      style={[
        s.sheet,
        shadow.overlay,
        {
          opacity: slide,
          transform: [{ translateY: slide.interpolate({ inputRange: [0, 1], outputRange: [40, 0] }) }],
        },
      ]}
    >
      <View style={s.grabber} />

      {hotlist ? (
        <View style={s.hotlistBanner} accessibilityRole="alert">
          <View style={s.hotlistIcon}>
            <Text style={s.hotlistIconText}>!</Text>
          </View>
          <View style={s.hotlistText}>
            <Text style={s.hotlistTitle}>Reported to police</Text>
            <Text style={s.hotlistSub}>This vehicle is on the active hotlist.</Text>
          </View>
        </View>
      ) : null}

      <View style={s.headerRow}>
        <View style={s.headerText}>
          <Text style={[s.title, hotlist && s.titleDanger]}>{state.title}</Text>
          {result.message ? (
            <Text style={s.subtitle} numberOfLines={3}>
              {result.message}
            </Text>
          ) : null}
        </View>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Dismiss result"
          onPress={onDismiss}
          hitSlop={10}
          style={s.close}
        >
          <Text style={s.closeText}>✕</Text>
        </Pressable>
      </View>

      {result.plate ? (
        <>
          <PlateBadge plate={result.plate} size="md" style={s.badge} />
          <View style={s.confRow}>
            <Text style={s.confLabel}>Confidence</Text>
            <Text style={[s.confValue, { color: confidenceColor(result.confidence, colors) }]}>
              {Math.round((result.confidence || 0) * 100)}%
            </Text>
          </View>
          <ConfidenceBar value={result.confidence} palette={colors} />
        </>
      ) : null}

      {result.state === "none" ? (
        <View style={s.tips}>
          {TIPS.map((tip) => (
            <Text key={tip} style={s.tip}>
              • {tip}
            </Text>
          ))}
        </View>
      ) : null}

      {/* Raw OCR text is debug output. Hidden behind an explicit press so a
          volunteer never mistakes a mangled read for a confirmed plate. */}
      {result.raw ? (
        <View style={s.detailsWrap}>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Toggle scan details"
            onPress={() => {
              LayoutAnimation.configureNext(LayoutAnimation.Presets.easeInEaseOut);
              setDetailsOpen((v) => !v);
            }}
            onLongPress={onDetailsLongPress}
            style={s.detailsToggle}
          >
            <Text style={s.detailsToggleText}>
              {detailsOpen ? "▾" : "▸"} Details
            </Text>
          </Pressable>
          {detailsOpen ? (
            <View style={s.details}>
              <DetailRow label="Raw OCR" value={result.raw || "—"} labelStyle={s.detailLabel} valueStyle={s.detailValue} />
              <DetailRow label="Normalised" value={result.normalized || "—"} labelStyle={s.detailLabel} valueStyle={s.detailValue} />
              <DetailRow label="Frames read" value={String(result.frames ?? 0)} labelStyle={s.detailLabel} valueStyle={s.detailValue} />
              <DetailRow label="Votes" value={JSON.stringify(result.votes || {})} labelStyle={s.detailLabel} valueStyle={s.detailValue} />
              {result.reason ? (
                <DetailRow label="Reason" value={result.reason} labelStyle={s.detailLabel} valueStyle={s.detailValue} />
              ) : null}
            </View>
          ) : null}
        </View>
      ) : null}

      <View style={s.actions}>
        <Button label="Scan again" size="lg" onPress={onRescan} style={s.flex} />
        <Button label="Dismiss" size="lg" variant="secondary" onPress={onDismiss} style={s.flex} />
      </View>
    </Animated.View>
  );
}

function DetailRow({ label, value, labelStyle, valueStyle }) {
  return (
    <View style={staticStyles.detailRow}>
      <Text style={labelStyle}>{label}</Text>
      <Text style={valueStyle} numberOfLines={2}>
        {value}
      </Text>
    </View>
  );
}

export const ScanResultSheet = memo(ScanResultSheetBase);

function makeStyles(c) {
  return StyleSheet.create({
    sheet: {
      position: "absolute",
      left: 0,
      right: 0,
      bottom: 0,
      backgroundColor: c.dark[800],
      borderTopLeftRadius: radius.xl,
      borderTopRightRadius: radius.xl,
      borderTopWidth: 1,
      borderColor: c.dark[600],
      paddingHorizontal: spacing.lg,
      paddingTop: spacing.sm,
      paddingBottom: spacing.xl,
      gap: spacing.md,
    },
    grabber: {
      alignSelf: "center",
      width: 40,
      height: 4,
      borderRadius: 2,
      backgroundColor: c.dark[500],
      marginBottom: spacing.sm,
    },
    headerRow: { flexDirection: "row", alignItems: "flex-start", gap: spacing.md },
    headerText: { flex: 1 },
    title: { color: c.onDark.strong, fontSize: fontSize.lg, fontWeight: fontWeight.bold },
    titleDanger: { color: "#FCA5A5" },
    subtitle: { color: c.onDark.muted, fontSize: fontSize.sm, marginTop: 3, lineHeight: 19 },
    close: { padding: spacing.xs },
    closeText: { color: c.onDark.muted, fontSize: fontSize.base, fontWeight: fontWeight.bold },
    hotlistBanner: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.md,
      backgroundColor: c.solidDanger,
      borderRadius: radius.md,
      padding: spacing.md,
    },
    hotlistIcon: {
      width: 34,
      height: 34,
      borderRadius: radius.sm,
      backgroundColor: "rgba(255,255,255,0.22)",
      alignItems: "center",
      justifyContent: "center",
    },
    hotlistIconText: { color: "#FFFFFF", fontSize: fontSize.xl, fontWeight: fontWeight.heavy },
    hotlistText: { flex: 1 },
    hotlistTitle: { color: "#FFFFFF", fontSize: fontSize.base, fontWeight: fontWeight.bold },
    hotlistSub: { color: "rgba(255,255,255,0.9)", fontSize: fontSize.xs, marginTop: 1 },
    badge: { marginTop: spacing.xs },
    confRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
    confLabel: { color: c.onDark.muted, fontSize: fontSize.sm },
    confValue: { fontSize: fontSize.base, fontWeight: fontWeight.bold },
    tips: { gap: 4 },
    tip: { color: c.onDark.body, fontSize: fontSize.sm },
    detailsWrap: { gap: spacing.sm },
    detailsToggle: { alignSelf: "flex-start" },
    detailsToggleText: { color: c.accent[300], fontSize: fontSize.sm, fontWeight: fontWeight.semibold },
    details: {
      backgroundColor: c.dark[900],
      borderRadius: radius.md,
      padding: spacing.md,
      gap: 6,
    },
    detailLabel: { color: c.onDark.faint, fontSize: fontSize.xs },
    detailValue: {
      color: c.onDark.body,
      fontSize: fontSize.xs,
      fontFamily: "monospace",
      flexShrink: 1,
      textAlign: "right",
    },
    actions: { flexDirection: "row", gap: spacing.md },
    flex: { flex: 1 },
  });
}

/** Theme-independent chrome (the plate badge always mimics a real plate). */
const staticStyles = StyleSheet.create({
  plateBadge: {
    alignSelf: "center",
    backgroundColor: "#FFFFFF",
    borderWidth: 2,
    borderColor: "#0F172A",
    borderRadius: radius.sm,
  },
  plateText: {
    color: "#0F172A",
    fontWeight: fontWeight.heavy,
    letterSpacing: 3,
    fontFamily: "monospace",
  },
  barTrack: {
    height: 8,
    borderRadius: 4,
    overflow: "hidden",
  },
  barFill: { height: "100%", borderRadius: 4 },
  detailRow: { flexDirection: "row", justifyContent: "space-between", gap: spacing.md },
});