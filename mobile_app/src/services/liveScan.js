import AsyncStorage from "@react-native-async-storage/async-storage";
import { API_BASE, getToken, restoreSession } from "./api";

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

    // 4401 means the access token was stale. One silent refresh, one retry,
    // then give up - retrying a credential the server keeps rejecting would
    // just spin.
    this._refreshTried = false;
  }

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
    const ws = new WebSocket(wsUrl);
    this.ws = ws;

    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.connected = true;
      this._attempt = 0;
      this._state("CONNECTED");
      // The token travels in the first auth message, never in the URL: query
      // strings end up in proxy and server access logs.
      ws.send(JSON.stringify({ type: "auth", token: getToken() || token, device_id: deviceId }));
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
          // Any reply for a frame releases the slot.
          this._releaseBusy();
          if (this.callbacks.onBoxes) this.callbacks.onBoxes(data);
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
          if (this.callbacks.onError) this.callbacks.onError(data.message || data.code);
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
        restoreSession().finally(() => {
          if (!this._closed) this._scheduleReconnect(250);
        });
        return;
      }
      this._scheduleReconnect();
    };

    // RN gives no useful detail on onerror; the close handler decides.
    ws.onerror = () => {};
  }

  _state(status) {
    if (this.callbacks.onStateChange) this.callbacks.onStateChange(status);
  }

  _startPing() {
    this._stopPing();
    this._pingTimer = setInterval(() => {
      if (this.ws && this.connected) {
        try {
          this.ws.send(JSON.stringify({ type: "ping", t: Date.now() }));
        } catch (e) {
          // A dead socket surfaces as a close next tick.
        }
      }
    }, PING_MS);
  }

  _stopPing() {
    if (this._pingTimer) clearInterval(this._pingTimer);
    this._pingTimer = null;
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

  _releaseBusy() {
    if (this._busyTimer) clearTimeout(this._busyTimer);
    this._busyTimer = null;
    this._busy = false;
    if (this._pending) {
      const next = this._pending;
      this._pending = null;
      this._doSend(next.width, next.height, next.lat, next.lng, next.base64);
    }
  }

  disconnect() {
    this._closed = true;
    this._stopPing();
    if (this._reconnectTimer) clearTimeout(this._reconnectTimer);
    this._reconnectTimer = null;
    if (this._busyTimer) clearTimeout(this._busyTimer);
    this._busyTimer = null;
    this._busy = false;
    this._pending = null;
    if (this.ws) {
      try { this.ws.close(); } catch (e) {}
      this.ws = null;
    }
    this.connected = false;
    this.ready = false;
  }

  sendFrame(width, height, lat, lng, base64) {
    if (!this.connected || !this.ready || !this.ws) return;

    if (this._busy) {
      // Newest wins: overwrite whatever was waiting rather than queueing.
      this._pending = { width, height, lat, lng, base64 };
      return;
    }
    this._doSend(width, height, lat, lng, base64);
  }

  _doSend(width, height, lat, lng, base64) {
    const ws = this.ws;
    if (!ws) return;
    this._busy = true;
    this.seq += 1;
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
      if (this.callbacks.onError) this.callbacks.onError("Send failed");
      return;
    }

    this._busyTimer = setTimeout(() => {
      // No reply in 1.5 s: assume it was dropped and move on.
      this._releaseBusy();
    }, UNANSWERED_MS);
  }
}
