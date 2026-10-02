import Constants from "expo-constants";
import { File } from "expo-file-system";

import { clearSession, loadSession, saveTokens } from "../storage/store";

/**
 * Decide which host the backend is on, at runtime.
 *
 * The app is always served by Metro on the PC, and Metro tells the bundle which
 * PC that is. Deriving the API host from that address is what makes the app
 * survive a network change: the PC gets a new IP, the phone rescans the QR,
 * Metro reports the new host, and the app follows automatically. No saved IP, no
 * edit to a .env, nothing to remember to re-run.
 *
 * An explicit EXPO_PUBLIC_API_URL still wins so a pinned/staging build is
 * possible, but start.ps1 deliberately leaves it empty.
 *
 * @returns {{ url: string, source: string }}
 */
function resolveApiBase() {
  const explicit = String(process.env.EXPO_PUBLIC_API_URL || "").trim();
  if (explicit) return { url: explicit.replace(/\/+$/, ""), source: "EXPO_PUBLIC_API_URL" };

  const extra = Constants.expoConfig?.extra?.apiUrl;
  if (extra) return { url: String(extra).replace(/\/+$/, ""), source: "expoConfig.extra.apiUrl" };

  // hostUri is "<host>:<port>" for the machine Metro is running on. The fields
  // below are the documented fallbacks across Expo Go and dev-client runtimes.
  const candidates = [
    ["Constants.expoConfig.hostUri", Constants.expoConfig?.hostUri],
    ["Constants.expoConfig.debuggerHost", Constants.expoConfig?.debuggerHost],
    ["Constants.expoGoConfig.debuggerHost", Constants.expoGoConfig?.debuggerHost],
    ["Constants.manifest2.debuggerHost", Constants.manifest2?.debuggerHost],
    ["Constants.manifest2.extra.expoGo?.debuggerHost", Constants.manifest2?.extra?.expoGo?.debuggerHost],
  ];

  for (const [source, value] of candidates) {
    const raw = typeof value === "string" ? value.trim() : "";
    if (!raw) continue;
    // Strip any scheme first: some runtimes hand back "http://10.0.0.5:8081".
    const host = raw.replace(/^[a-z]+:\/\//i, "").split(":")[0].trim();
    if (!host) continue;
    // 127.0.0.1 on a phone is the phone itself, which is never the backend.
    if (host === "127.0.0.1" || host === "localhost" || host === "::1") {
      return { url: "http://127.0.0.1:8000", source: `${source} (loopback)` };
    }
    return { url: `http://${host}:8000`, source };
  }

  return { url: "http://127.0.0.1:8000", source: "default (no Metro host found)" };
}

const resolved = resolveApiBase();
export const API_BASE = resolved.url;
export const API_BASE_SOURCE = resolved.source;

// Printed once at startup, with the source. When the phone cannot reach the
// backend this line - visible in the Metro console and in the in-app Test
// backend result - says which host the app believes it should be talking to.
console.log(`[RAKSHAK] API base: ${API_BASE}  (source: ${API_BASE_SOURCE})`);

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
    const reason = error?.message || "unknown";
    // A fetch that rejects while BUILDING the request never touched the network
    // ("Unsupported FormDataPart implementation" and friends). Only RN's
    // "Network request failed" means the request went out and the link failed,
    // so the two must not share a message or the real cause gets hidden.
    if (/unsupported formdatapart|failed to construct|unsupported body|unsupported request|invalid formdata/i.test(reason)) {
      throw new Error(`Request error: ${reason}`);
    }
    throw new Error(`Network error | URL=${url} | Reason=${reason} | Timeout=${timeoutMs}ms`);
  } finally {
    clearTimeout(timeoutId);
  }
}

/**
 * Backend health probe.
 *
 * The result carries the host the app actually used, so the on-screen Test
 * backend result doubles as proof of which machine the app believes it is
 * talking to. After a network change that is the one line that distinguishes
 * "the app still has the old IP" from "the phone cannot reach the new one".
 */
export async function healthCheck() {
  const url = `${API_BASE}/health`;
  const { json } = await fetchJson(url, { method: "GET", headers: { Accept: "application/json" } });
  return { ...(json || {}), api_base: API_BASE, api_base_source: API_BASE_SOURCE };
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

/**
 * The one HTTP entry point: JSON in, JSON out, Bearer token attached.
 *
 * There is deliberately no multipart branch. Expo SDK 58's ambient fetch has its
 * own FormData whose parts reject React Native's `{ uri, name, type }` file
 * object with `Unsupported FormDataPart implementation`, thrown before the
 * request leaves the phone. Upload binary data as base64 JSON instead - see
 * scanImage(). Keeping the option alive would only be an accident waiting to
 * happen.
 */
async function request(method, path, { body, retried = false, timeoutMs } = {}) {
  // Make sure a persisted token is loaded before the header is built.
  await restoreSessionOnce();

  const headers = {};
  if (session.token) headers.Authorization = `Bearer ${session.token}`;
  authDebug("request start", { method, path, authorizationAttached: !!session.token, retried });

  let payload;
  if (body !== undefined && body !== null) {
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
    return request(method, path, { body, retried: true, timeoutMs });
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

/** Scan timeouts. The host runs CPU OCR and loads the YOLO + OCR models lazily,
 *  so the very first scan after a backend restart pays a cold start measured at
 *  ~14 s on the dev PC (more with a phone upload on top). Every later scan is
 *  warm at ~4-6 s. */
export const SCAN_TIMEOUT_COLD_MS = 45000;
export const SCAN_TIMEOUT_WARM_MS = 30000;

let scansCompleted = 0;

/** Timeout for the next scan: generous on the first call, tighter afterwards. */
export function scanTimeoutMs() {
  return scansCompleted === 0 ? SCAN_TIMEOUT_COLD_MS : SCAN_TIMEOUT_WARM_MS;
}

/**
 * Read a captured frame as bare base64 (no `data:` prefix).
 * Failure here is a capture-side fault, so it is labelled as such rather than
 * being allowed to look like a network problem.
 */
export async function frameToBase64(uri) {
  try {
    return await new File(uri).base64();
  } catch (error) {
    throw new Error(`Could not prepare image: ${error?.message || error}`);
  }
}

/**
 * POST one or more frames to /scanner/scan as JSON base64.
 *
 * Deliberately NOT multipart: Expo SDK 58's ambient fetch (expo/fetch via the
 * winter runtime) has its own FormData implementation, and appending React
 * Native's legacy `{ uri, name, type }` object to it throws
 * `Unsupported FormDataPart implementation` before the request is ever sent.
 * Base64 JSON has no FormData at all and behaves identically on every SDK.
 *
 * @param {string[]} base64Frames bare base64 JPEGs, 1-3 of them
 */
export async function scanImage(base64Frames) {
  const frames = Array.isArray(base64Frames) ? base64Frames.filter(Boolean) : [base64Frames];
  if (!frames.length) throw new Error("Could not prepare image: empty frame");

  const startedAt = Date.now();
  const payload = frames.length === 1 ? { image_b64: frames[0] } : { images_b64: frames };
  const data = await request("POST", "/scanner/scan", {
    body: payload,
    timeoutMs: scanTimeoutMs(),
  });
  scansCompleted += 1;
  return { ...data, elapsedMs: Date.now() - startedAt, uploadVia: "base64-json", framesSent: frames.length };
}

export async function reportSighting(sighting) {
  return request("POST", "/sightings", { body: sighting });
}

export default API_BASE;
