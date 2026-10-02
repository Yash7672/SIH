import { StyleSheet, Text, View } from "react-native";

export default function PlateResult({ hit }) {
  if (!hit) return null;
  const time = new Date(hit.timestamp || Date.now()).toLocaleTimeString();
  return (
    <View style={[styles.card, hit.confidence >= 0.6 ? styles.hit : styles.low]}>
      <View style={styles.row}>
        <View style={styles.plateBox}>
          <Text style={styles.plate}>{hit.plate || "—"}</Text>
        </View>
        <View style={styles.meta}>
          <Text style={styles.time}>{time}</Text>
          <Text style={styles.conf}>conf {Math.round((hit.confidence || 0) * 100)}%</Text>
        </View>
      </View>
      <Text style={styles.note}>{hit.note || "Reported to police network"}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: { backgroundColor: "#1e293b", borderRadius: 14, padding: 14, marginBottom: 10 },
  hit: { borderLeftWidth: 3, borderLeftColor: "#22c55e" },
  low: { borderLeftWidth: 3, borderLeftColor: "#f59e0b" },
  row: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
  plateBox: {
    backgroundColor: "#f8fafc",
    borderRadius: 8,
    paddingHorizontal: 14,
    paddingVertical: 8,
  },
  plate: { color: "#0f172a", fontSize: 20, fontWeight: "800", letterSpacing: 2 },
  meta: { alignItems: "flex-end" },
  time: { color: "#94a3b8", fontSize: 12 },
  conf: { color: "#e2e8f0", fontWeight: "700", fontSize: 13, marginTop: 2 },
  note: { color: "#64748b", fontSize: 12, marginTop: 8 },
});