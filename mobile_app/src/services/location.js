// GPS helper with caching + graceful fallback.
//
// Performance: a fresh GPS lock costs seconds and battery. Detections happen in
// bursts (several plates per scan session), so a short-lived cache plus the
// OS's last-known position is used before asking for a brand new fix. If the
// device has no fix at all (or permission is denied) the scanner falls back to
// the last known coordinates so the demo still produces route points.

const FALLBACK = { latitude: 17.44, longitude: 78.35 };

// Coordinates are considered fresh for this long.
const CACHE_TTL_MS = 15000;
// An OS-cached fix younger than this is good enough to skip a new GPS lock.
const LAST_KNOWN_MAX_AGE_MS = 60000;

let cached = null; // { latitude, longitude, at }
let lastKnown = null;

function remember(coords) {
  cached = { latitude: coords.latitude, longitude: coords.longitude, at: Date.now() };
  lastKnown = { latitude: coords.latitude, longitude: coords.longitude };
  return lastKnown;
}

export async function getLocation() {
  if (cached && Date.now() - cached.at < CACHE_TTL_MS) {
    return { latitude: cached.latitude, longitude: cached.longitude };
  }

  try {
    const Location = require("expo-location");
    const { status } = await Location.requestForegroundPermissionsAsync();
    if (status !== "granted") {
      return lastKnown || (cached && { ...cached }) || FALLBACK;
    }

    // Fast path: reuse the platform's recent cached fix when available.
    if (typeof Location.getLastKnownPositionAsync === "function") {
      try {
        const last = await Location.getLastKnownPositionAsync({
          maxAge: LAST_KNOWN_MAX_AGE_MS,
          requiredAccuracy: 2000,
        });
        if (last?.coords) return remember(last.coords);
      } catch {
        /* fall through to a fresh fix */
      }
    }

    const pos = await Location.getCurrentPositionAsync({
      accuracy: Location.Accuracy.Balanced,
    });
    if (pos?.coords) return remember(pos.coords);
    return lastKnown || FALLBACK;
  } catch {
    return lastKnown || (cached && { ...cached }) || FALLBACK;
  }
}
