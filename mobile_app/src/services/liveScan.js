import AsyncStorage from "@react-native-async-storage/async-storage";
import { API_BASE, getToken, refreshSession, tokenExpiresInMs } from "./api";

const DEVICE_KEY = "rakshak_device_id";

// Reconnect backoff. A phone that walks out of WiFi range, backgrounds the app
// or loses the hotspot comes back within seconds, so the first retry has to be
// quick; beyond that, stop hammering the server.
const BACKOFF_MS = [1000, 2000, 5000, 10000];
const PING_MS = 20000;
// One unanswered frame is allowed. Anything older than this is presumed lost
// and its slot is released, so the stream recovers on its own after a dropped
// reply instead of wedging forever.
const UNANSWERED_MS = 1500;
// Re-auth this long before the access token actually expires. The socket
// authenticates once on open, so without this a volunteer scanning a road for
// half an hour holds a claim that has expired: every frame still works, but the
// first reconnect afterwards is refused with 4401.
const REAUTH_SKEW_MS = 120000;
// How close to expiry counts as "expired" for an in-flight socket.
const REAUTH_FLOOR_MS = 30000;

/**
 * The live-scan socket.
 *
 * One instance per screen, one WebSocket per instance, and exactly one frame in
 * flight. Every hard-won behaviour here is load-bearing:
 *
 *   - the token travels in the first `auth` message, never in the URL, because
 *     query strings end up in proxy and server access logs;
 *   - a frame awaiting a reply gives its slot back after 1.5 s, so a dropped
 *     reply cannot wedge the stream;
 *   - while busy, a new frame *replaces* the waiting one rather than queueing,
 *     so the detector always sees the freshest view of the road;
 *   - a 4401 buys exactly one silent token refresh and one retry. Retrying a
 *     credential the server keeps rejecting just spins.
 */
export class LiveScanClient {
  constructor(callbacks) {
    this.ws = null;
    this.callbacks = callbacks || {};
    this.seq = 0;
    this.connected = false;
    this.ready = false;

    // Guards every scheduled reconnect. `_closed` is the difference between
    // "the socket dropped, come back" and "the user left the screen".
    this._closed = true;
    this._attempt = 0;
    this._reconnectTimer = null;
    this._pingTimer = null;

    // At most one frame may be in flight. `pending` is the newest frame seen
    // while busy: newest-wins, so the server always gets the freshest view of
    // the road rather than a backlog of stale ones.
    this._busy = false;
    this._pending = null;
    this._busyTimer = null;
    this._inflightSeq = 0;
    // seq -> send time, for an honest round-trip number in the UI.
    this._sentAt = new Map();
    this.lastRoundTripMs = 0;
    this._idleWaiters = [];

    // 4401 means the access token was stale. One silent refresh, one retry,
    // then give up - retrying a credential the server keeps rejecting would
    // just spin.
    this._refreshTried = false;
  }

  /**
   * Open the socket.
   *
   * Declared `async` for the one thing it awaits (the stored device id), but it
   * is written so it cannot reject: `new WebSocket()` throws synchronously on a
   * malformed URL, and this is called from a reconnect timer and from an effect
   * body, neither of which is watching for a promise. A failed connect has to
   * show up as OFFLINE on screen, not as an unhandled rejection.
   */
  async connect() {
    if (this.ws) return;
    this._closed = false;

    const token = getToken();
    if (!token) {
      if (this.callbacks.onError) this.callbacks.onError("No auth token");
      this._scheduleReconnect();
      return;
    }

    let deviceId = null;
    try {
      deviceId = await AsyncStorage.getItem(DEVICE_KEY);
    } catch (e) {
      deviceId = null;
    }
    if (this._closed) return;

    const wsUrl = API_BASE.replace(/^http/, "ws") + "/api/v1/ws/scan";
    let ws;
    try {
      ws = new WebSocket(wsUrl);
    } catch (e) {
      // An unusable base URL is a configuration fault, not a dropped connection.
      this._state("OFFLINE");
      this._scheduleReconnect();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.connected = true;
      // `_attempt` is reset on every successful connect, so a phone that flaps
      // between two hotspots always restarts at the 1 s backoff instead of
      // drifting up to the 10 s one and looking like it had given up.
      this._attempt = 0;
      this._refreshTried = false;
      this._state("CONNECTED");
      // The token travels in the first auth message, never in the URL: query
      // strings end up in proxy and server access logs.
      this._auth(ws, deviceId);
      this._startPing();
    };

    ws.onmessage = (e) => {
      if (this.ws !== ws) return;
      let data;
      try {
        data = JSON.parse(e.data);
      } catch (err) {
        return;
      }
      switch (data.type) {
        case "ready":
          this.ready = true;
          this._state("READY");
          break;
        case "boxes":
          // The first reply for a frame releases the slot: the boxes arrive
          // before the plate boxes and long before the OCR text, so holding the
          // slot any longer would idle the camera for no reason.
          this._ack(data.seq);
          this._releaseBusy(data.seq);
          if (this.callbacks.onBoxes) this.callbacks.onBoxes(data);
          break;
        case "plates":
          if (this.callbacks.onPlates) this.callbacks.onPlates(data);
          break;
        case "plate":
          if (this.callbacks.onPlate) this.callbacks.onPlate(data);
          break;
        case "ping":
          // Server keepalive: answer so it does not drop us as idle.
          try { ws.send(JSON.stringify({ type: "pong", t: data.t })); } catch (err) {}
          break;
        case "pong":
          break;
        case "error":
          // Server-side faults are reported by code so the UI can say the truth
          // ("camera frame too large", "vehicle model missing") instead of a
          // generic connection failure.
          if (this.callbacks.onError) this.callbacks.onError(data.message || data.code, data.code);
          break;
        default:
          break;
      }
    };

    ws.onclose = (e) => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.connected = false;
      this.ready = false;
      this._stopPing();
      this._releaseBusy();
      this._state("OFFLINE");

      if (this._closed) return;

      if (e && e.code === 4401 && !this._refreshTried) {
        this._refreshTried = true;
        // refreshSession() throws when the refresh token itself is dead, and in
        // that case it has already cleared the session - which is exactly the
        // signal to send the user back to the login screen instead of
        // reconnecting with a credential the server will refuse again.
        refreshSession()
          .then(() => {
            if (!this._closed) this._scheduleReconnect(250);
          })
          .catch(() => {
            if (this._closed) return;
            this._state("SESSION");
            this.callbacks.onSessionExpired && this.callbacks.onSessionExpired();
          });
        return;
      }
      this._scheduleReconnect();
    };

    // RN gives no useful detail on onerror; the close handler decides.
    ws.onerror = () => {};
  }

  _auth(ws, deviceId) {
    try {
      ws.send(JSON.stringify({ type: "auth", token: getToken(), device_id: deviceId }));
    } catch (e) {
      // The socket is closing under us; onclose handles the reconnect.
    }
  }

  _state(status) {
    if (this.callbacks.onStateChange) this.callbacks.onStateChange(status);
  }

  _startPing() {
    this._stopPing();
    this._pingTimer = setInterval(() => {
      if (!this.ws || !this.connected) return;
      try {
        this.ws.send(JSON.stringify({ type: "ping", t: Date.now() }));
      } catch (e) {
        // A dead socket surfaces as a close next tick.
      }
      this._maybeReauth().catch(() => {});
    }, PING_MS);
  }

  _stopPing() {
    if (this._pingTimer) clearInterval(this._pingTimer);
    this._pingTimer = null;
  }

  /**
   * Re-authenticate on a healthy socket before the claim it holds goes stale.
   *
   * The server pins the identity it validated at connect time, so this cannot
   * extend the current socket; what it buys is that the token in memory matches
   * the token on disk when the phone eventually reconnects.
   */
  async _maybeReauth() {
    if (this._closed || !this.ws || !this.connected) return;
    const msLeft = tokenExpiresInMs();
    if (!msLeft || msLeft > REAUTH_SKEW_MS) return;
    try {
      await refreshSession();
    } catch (e) {
      // A dead refresh token is handled by the 4401 path on the next reconnect;
      // tearing the socket down here would only make the stream jump for nothing.
      return;
    }
    if (this._closed || !this.ws) return;
    this._auth(this.ws, this._deviceId);
  }

  _scheduleReconnect(delayOverride) {
    if (this._closed || this._reconnectTimer) return;
    const wait = delayOverride != null
      ? delayOverride
      : BACKOFF_MS[Math.min(this._attempt, BACKOFF_MS.length - 1)];
    this._attempt += 1;
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      this.connect();
    }, wait);
  }

  _releaseBusy(seq) {
    if (this._busyTimer) clearTimeout(this._busyTimer);
    this._busyTimer = null;
    // Only the frame we are waiting on may release the slot. A late reply for an
    // already-abandoned frame must not start a second inference chain.
    if (seq != null && this._inflightSeq && seq !== this._inflightSeq) return;
    this._busy = false;
    this._inflightSeq = 0;
    const waiters = this._idleWaiters;
    this._idleWaiters = [];
    waiters.forEach((fn) => fn());
    if (this._pending) {
      const next = this._pending;
      this._pending = null;
      this._doSend(next.width, next.height, next.lat, next.lng, next.base64);
    }
  }

  /** Record the round trip of a frame, which is what the capture loop paces on. */
  _ack(seq) {
    const at = seq != null ? this._sentAt.get(seq) : null;
    if (at == null) return;
    this._sentAt.delete(seq);
    // Only the newest few matter; a leaked map would grow for the whole shift.
    if (this._sentAt.size > 8) {
      const oldest = this._sentAt.keys().next().value;
      this._sentAt.delete(oldest);
    }
    this.lastRoundTripMs = Date.now() - at;
    if (this.callbacks.onRoundTrip) this.callbacks.onRoundTrip(this.lastRoundTripMs);
  }

  /**
   * Resolve once nothing is in flight. This is what keeps the capture loop to
   * one outstanding frame without it having to know the timeout itself.
   */
  whenIdle(timeoutMs) {
    if (!this._busy) return Promise.resolve();
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        const i = this._idleWaiters.indexOf(finish);
        if (i >= 0) this._idleWaiters.splice(i, 1);
        resolve();
      };
      const timer = setTimeout(finish, timeoutMs);
      this._idleWaiters.push(finish);
    });
  }

  disconnect() {
    this._closed = true;
    this._stopPing();
    if (this._reconnectTimer) clearTimeout(this._reconnectTimer);
    this._reconnectTimer = null;
    if (this._busyTimer) clearTimeout(this._busyTimer);
    this._busyTimer = null;
    this._busy = false;
    this._inflightSeq = 0;
    this._pending = null;
    this._sentAt.clear();
    this._idleWaiters = [];
    if (this.ws) {
      try { this.ws.close(); } catch (e) {}
      this.ws = null;
    }
    this.connected = false;
    this.ready = false;
  }

  /** Remembers the device id so a mid-socket re-auth can reuse it. */
  setDeviceId(deviceId) {
    this._deviceId = deviceId;
  }

  sendFrame(width, height, lat, lng, base64) {
    if (!this.connected || !this.ready || !this.ws) return false;

    if (this._busy) {
      // Newest wins: overwrite whatever was waiting rather than queueing.
      this._pending = { width, height, lat, lng, base64 };
      return false;
    }
    return this._doSend(width, height, lat, lng, base64);
  }

  _doSend(width, height, lat, lng, base64) {
    const ws = this.ws;
    if (!ws) return false;
    this._busy = true;
    this.seq += 1;
    this._inflightSeq = this.seq;
    this._sentAt.set(this.seq, Date.now());
    try {
      ws.send(JSON.stringify({
        type: "frame",
        seq: this.seq,
        w: width,
        h: height,
        // 0,0 is "unknown" to the server, which ignores such frames rather
        // than crediting them to a cell off the coast of Africa.
        lat: lat || 0,
        lng: lng || 0,
        jpeg_b64: base64
      }));
    } catch (e) {
      this._busy = false;
      this._inflightSeq = 0;
      this._sentAt.delete(this.seq);
      if (this.callbacks.onError) this.callbacks.onError("Send failed");
      return false;
    }

    this._busyTimer = setTimeout(() => {
      // No reply in 1.5 s: assume it was dropped and move on. This timeout is
      // the only thing that lets the capture loop recover from a lost reply.
      this._releaseBusy();
    }, UNANSWERED_MS);
    return true;
  }
}

/** Statuses the UI renders as LIVE / RECONNECTING / OFFLINE. */
export function socketStatusLabel(status) {
  if (status === "READY" || status === "CONNECTED") return "LIVE";
  if (status === "CONNECTING" || status === "RECONNECTING") return "RECONNECTING";
  if (status === "CHECKING") return "RECONNECTING";
  return "OFFLINE";
}

export { REAUTH_FLOOR_MS };