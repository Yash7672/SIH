import Constants from "expo-constants";

import { clearSession, loadSession, saveTokens } from "../storage/store";

function resolveApiBase() {
  const configured = process.env.EXPO_PUBLIC_API_URL || Constants.expoConfig?.extra?.apiUrl;
  if (configured) return String(configured).replace(/\/$/, "");

  const hostUri = Constants.expoConfig?.hostUri || Constants.expoConfig?.debuggerHost || "127.0.0.1:8081";
  const host = hostUri.split(":")[0];

  if (!host || host === "127.0.0.1" || host === "localhost") {
    return "http://127.0.0.1:8000";
  }

  return `http://${host}:8000`;
}

export const API_BASE = resolveApiBase();

// Printed once at startup: if the phone cannot reach the backend, this line is
// the fastest way to confirm the app is pointing at the PC's LAN IP and not at
// localhost (which on a phone means the phone itself).
console.log(`[RAKSHAK] API base: ${API_BASE}`);

function authDebug(message, extra = {}) {
  console.log(`[RAKSHAK AUTH] ${message}`, extra);
}

async function fetchJson(url, options = {}) {
  const timeoutMs = options.timeoutMs ?? 15000;
  // Callers that need to inspect the status themselves (the refresh flow)
  // pass throwOnError: false. Otherwise a 401 throws from inside here and the
  // caller's own 401 handling below is unreachable.
  const throwOnError = options.throwOnError !== false;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const text = await response.text();
    let json = null;
    try {
      json = text ? JSON.parse(text) : null;
    } catch (_error) {
      json = null;
    }

    if (!response.ok && throwOnError) {
      const detail = json?.detail || json?.message || text || `HTTP ${response.status}`;
      throw new Error(`HTTP ${response.status}: ${detail}`);
    }

    return { response, json, text };
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error(`Timeout after ${timeoutMs} ms while connecting to ${url}`);
    }
    if (error.message && error.message.startsWith("HTTP ")) {
      throw error;
    }
    throw new Error(`Network error | URL=${url} | Reason=${error.message || "unknown"} | Timeout=${timeoutMs}ms`);
  } finally {
    clearTimeout(timeoutId);
  }
}

export async function healthCheck() {
  const url = `${API_BASE}/health`;
  const { json } = await fetchJson(url, { method: "GET", headers: { Accept: "application/json" } });
  return json || {};
}

export function normalizePlate(raw = "") {
  if (raw == null) return "";
  const compact = String(raw).normalize("NFKC").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
  if (!compact) return "";

  const digitToLetter = { 0: "O", 1: "I", 2: "Z", 5: "S", 6: "G", 7: "T", 8: "B", 9: "G" };
  const letterToDigit = { O: "0", I: "1", L: "1", S: "5", B: "8", Z: "2", G: "6", T: "7" };

  function coerce(segment, expected) {
    return Array.from(segment).map((ch) => {
      if (expected === "letters" && /\d/.test(ch)) return digitToLetter[ch] || ch;
      if (expected === "digits" && /[A-Z]/.test(ch)) return letterToDigit[ch] || ch;
      return ch;
    }).join("");
  }

  const candidates = new Set([compact]);
  for (const rtoLen of [1, 2]) {
    for (const seriesLen of [1, 2, 3]) {
      const trailingLen = compact.length - 2 - rtoLen - seriesLen;
      if (trailingLen < 1 || trailingLen > 4) continue;
      const state = coerce(compact.slice(0, 2), "letters");
      const rto = coerce(compact.slice(2, 2 + rtoLen), "digits");
      const series = coerce(compact.slice(2 + rtoLen, 2 + rtoLen + seriesLen), "letters");
      const trailing = coerce(compact.slice(2 + rtoLen + seriesLen), "digits");
      candidates.add(`${state}${rto}${series}${trailing}`);
    }
  }

  const valid = Array.from(candidates).filter((candidate) => /^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4}$/.test(candidate));
  return valid.sort()[0] || compact;
}

// The module owns the tokens so a refresh is transparent to every caller:
// no screen has to re-thread a new token through props after it is renewed.
let session = { token: null, refreshToken: null };

export function setTokens({ token, refreshToken } = {}) {
  session = { token: token ?? null, refreshToken: refreshToken ?? null };
}

export function getToken() {
  return session.token;
}

// Persisted tokens only live in AsyncStorage; this module's copy is in memory.
// Without restoring it on startup every request after a cold start (or an
// Expo reload) went out with no Authorization header and the API answered
// 401 "Not authenticated" even though a perfectly good token was on disk.
// The restore runs once per app launch and is shared by all callers.
let restorePromise = null;

async function restoreSessionOnce() {
  if (!restorePromise) {
    restorePromise = (async () => {
      try {
        const stored = await loadSession();
        if (stored?.token && !session.token) {
          session = { token: stored.token, refreshToken: stored.refreshToken ?? null };
          authDebug("session restored from storage", {
            hasRefreshToken: Boolean(session.refreshToken),
          });
        }
      } catch (error) {
        authDebug("session restore failed", { reason: String(error?.message || error) });
      }
      return session;
    })();
  }
  return restorePromise;
}

export async function restoreSession() {
  await restoreSessionOnce();
  return session;
}

export async function hasSession() {
  await restoreSessionOnce();
  return Boolean(session.token);
}

// Drop both the in-memory copy and the persisted one. A token the server no
// longer accepts (expired, or the user was removed by a reseed) must not be
// retried forever.
export async function resetSession() {
  session = { token: null, refreshToken: null };
  restorePromise = null;
  try {
    await clearSession();
  } catch (error) {
    authDebug("clearing stored session failed", { reason: String(error?.message || error) });
  }
}

// One refresh at a time: several 401s arriving together would otherwise each
// burn the refresh token and the later ones would fail on a rotated token.
let refreshInFlight = null;

async function refreshSession() {
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    if (!session.refreshToken) {
      authDebug("refresh denied: no refresh token present");
      throw new Error("Session expired, please log in again");
    }
    authDebug("refresh attempted", { url: `${API_BASE}/api/v1/auth/refresh` });
    const { response, json, text } = await fetchJson(`${API_BASE}/api/v1/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: session.refreshToken }),
      throwOnError: false,
    });

    if (!response.ok || !json?.access_token) {
      const detail = json?.detail || text || `HTTP ${response.status}`;
      authDebug("refresh failed", { status: response.status, detail });
      // The refresh token is spent or the session was revoked server-side.
      // Clear it, otherwise the app retries with a token that can never work.
      await resetSession();
      throw new Error("Session expired, please log in again");
    }

    authDebug("refresh success", {
      accessTokenReceived: true,
      refreshTokenReceived: Boolean(json.refresh_token),
    });
    setTokens({ token: json.access_token, refreshToken: json.refresh_token });
    await saveTokens(json.access_token, json.refresh_token);
    return json;
  })();

  try {
    return await refreshInFlight;
  } finally {
    refreshInFlight = null;
  }
}

function describeHttpFailure(status, detail) {
  if (status === 401) return `HTTP 401: Session expired, please log in again (${detail})`;
  if (status === 403) return `HTTP 403: Not allowed (${detail})`;
  return `HTTP ${status}: ${detail}`;
}

async function request(method, path, { body, formData, retried = false, timeoutMs } = {}) {
  // Make sure a persisted token is loaded before the header is built.
  await restoreSessionOnce();

  const headers = {};
  if (session.token) headers.Authorization = `Bearer ${session.token}`;
  authDebug("request start", { method, path, authorizationAttached: !!session.token, retried });
  let payload;
  if (formData) {
    payload = formData;
  } else if (body) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  const url = `${API_BASE}/api/v1${path}`;
  const { response, json, text } = await fetchJson(url, {
    method,
    headers,
    body: payload,
    timeoutMs,
    throwOnError: false,
  });

  if (response.status === 401 && !retried && session.refreshToken && !path.startsWith("/auth/")) {
    authDebug("401 received, trying refresh", { method, path });
    try {
      await refreshSession();
    } catch (error) {
      authDebug("refresh did not recover the session", { method, path, reason: String(error?.message || error) });
      throw error;
    }
    authDebug("retrying original request after refresh", { method, path });
    return request(method, path, { body, formData, retried: true, timeoutMs });
  }

  if (!response.ok) {
    const detail = json?.detail || json?.message || text || `HTTP ${response.status}`;
    const message = describeHttpFailure(response.status, detail);
    authDebug("request failed", { method, url, status: response.status, detail });
    if (response.status === 401 && !path.startsWith("/auth/")) {
      // No refresh token, or the retry already failed: the stored session is
      // dead, so drop it and send the user back to Login.
      await resetSession();
    }
    throw new Error(message);
  }

  return json;
}

export async function login(email, password) {
  const data = await request("POST", "/auth/login", { body: { email, password } });
  authDebug("login success", { accessTokenReceived: !!data.access_token, refreshTokenReceived: !!data.refresh_token });
  setTokens({ token: data.access_token, refreshToken: data.refresh_token });
  return data;
}

export async function register({ name, email, phone, password, role }) {
  const data = await request("POST", "/auth/register", { body: { name, email, phone, password, role } });
  authDebug("register success", { accessTokenReceived: !!data.access_token, refreshTokenReceived: !!data.refresh_token });
  setTokens({ token: data.access_token, refreshToken: data.refresh_token });
  return data;
}

export async function registerDevice(name) {
  return request("POST", "/devices/register", {
    body: { device_type: "mobile", device_name: name },
  });
}

export async function scanImage(uri) {
  const formData = new FormData();
  formData.append("image", { uri, name: "frame.jpg", type: "image/jpeg" });
  // The host runs CPU OCR and loads the YOLO + OCR models lazily on the very
  // first scan; the default 15s budget is not enough for that cold start (it
  // measured ~12s on the dev PC and a phone upload adds more). Give it 45s.
  return request("POST", "/scanner/scan", { formData, timeoutMs: 45000 });
}

export async function reportSighting(sighting) {
  return request("POST", "/sightings", { body: sighting });
}

export default API_BASE;
