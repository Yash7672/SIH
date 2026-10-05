# Project Map

reviewed at commit `live-detect` (branch) — see `git log -1`
Last full review: 2026-10-04, after the live-detection and Maps-heatmap work.

## Architecture Overview

RAKSHAK is a "Privacy-First Crowdsourced ANPR Vehicle Tracking System".

- **Backend**: FastAPI (Python 3.12), PostgreSQL 17 (source of truth), Redis 7 (cache only).
- **AI Engine**: YOLOv8n COCO (`backend/models/yolov8n.pt`) for **vehicles** —
  classes 2/3/5/7 → `CAR` / `BIKE` / `BUS` / `TRUCK`, and no other label is ever
  emitted; a fine-tuned YOLOv8n plate detector (`license-plate-finetune-v1n.pt`);
  RapidOCR (ONNX) for plate text. Inference runs **on the PC backend**, never on the
  phone — Expo Go cannot run native ML, and a frame that never leaves the process is a
  frame that cannot leak. Both models are loaded *and warmed* at startup
  (`warm_live_models()`), because the first YOLO call costs ~4 s and the first OCR
  call ~4.3 s, and paying that on the first volunteer frame is the difference
  between "the app is broken" and "it takes a second".
- **citizen_web**: Vite + React 18 SPA for filing complaints.
- **police_dashboard**: Vite + React 18 SPA for officers.
- **mobile_app**: Expo SDK 58 / React Native app for volunteers and officers.

Frames are held in memory for the duration of one inference and are never written to
disk or logged. Only `plate + lat + lng + timestamp + confidence` can be persisted,
and only for plates that are on the active hot-list.

## What start.bat does

`start.bat` invokes `scripts/start.ps1` (bash equivalent `scripts/start.sh`), which:

1. Checks prerequisites (Node, Python venv, Docker).
2. Detects the LAN IP and writes `.env` + `mobile_app/.env` (only if missing).
3. Installs dependencies (`npm install`, `pip install -r requirements.txt`).
4. Starts Postgres + Redis via `docker compose up -d`.
5. Downloads `backend/models/yolov8n.pt` if absent (idempotent; skips gracefully offline).
6. Runs `alembic upgrade head`.
7. Seeds demo data (`scripts/seed_demo.py`), guarded by a hash marker so it runs once.
8. Starts four processes: uvicorn `:8000`, citizen Vite `:5173`, police Vite `:5174`,
   `expo start` `:8081`.

`scripts/stop.ps1` / `stop.sh` tear all of it down.

### Import-root gotcha (already fixed, worth knowing)

`ai/` lives at the **repo root**, and `app/api/v1/live_scan.py` does `from ai.detector import …`.
`start.ps1` runs uvicorn with `WorkingDirectory = backend\`, which leaves the repo root off
`sys.path` and the backend dies at import with `ModuleNotFoundError: No module named 'ai'`.
`Start-BackendLocal` therefore sets `PYTHONPATH` to the repo root **for the uvicorn child
only** (Process scope, restored in a `finally`). `backend/pytest.ini` does the same via
`pythonpath = ..`. Docker was never affected: `backend/Dockerfile` copies `ai` into `/app`.

## Folder Purpose & Key Files

| Folder | Purpose | Key files |
| --- | --- | --- |
| `backend/app/main.py` | App factory, CORS, privacy guard middleware, lifespan (seeds users, rebuilds hot-list cache, warms models) | `warm_live_models()` |
| `backend/app/api/v1/` | HTTP + WebSocket routers | `live_scan.py`, `sightings.py`, `scanner.py`, `ws.py`, `hotlist.py` |
| `backend/app/models/models.py` | SQLAlchemy models | `User`, `Device`, `Complaint`, `Hotlist`, `Sighting`, `AuditLog` |
| `backend/app/services/` | Business logic | `hotlist_service.py`, `sighting_service.py`, `plate.py`, `plate_reader.py`, `plate_verdict.py`, `cache.py`, `seed.py` |
| `backend/app/ws/manager.py` | Police alert fan-out (`alert_manager`) | `broadcast()` |
| `backend/app/ws/scan_manager.py` | Per-connection live-scan state: `VehicleTracker` (IoU tracking, per-class chip numbering, plate read budget), `ScanConnection` (in-flight slots, OCR claims, device, per-track verdicts, hires_pending/hires_active/last_hires_at) | `VehicleTracker`, `ScanConnection` |
| `backend/alembic/versions/` | Migrations | `0001_initial`, `0002_perf_indexes` |
| `ai/` | Model wrappers (repo-root package) | `detector.py`, `ocr_engine.py`, `pipeline.py` |
| `scripts/` | Launchers, seeds, diagnostics | `start.ps1`, `seed_demo.py`, `live_scan_test.py`, `chain_check.py`, `heat_check.py`, `speed_check.py`, `speed_notes.py`, `probe_leg_contention.py`, `render_live_view.py`, `make_test_road_scene.py`, `bench_plate_reader.py`, `benchmark_ocr.py` |
| `citizen_web/` | Citizen SPA | `src/pages/`, `src/services/api.ts` |
| `police_dashboard/` | Police SPA | `src/pages/`, `src/components/map/VehicleMap.tsx`, `src/leaflet-theme.css` |
| `mobile_app/` | Expo app | `src/screens/ScannerScreen.js`, `src/services/liveScan.js`, `src/services/liveCapture.js`, `src/components/LiveDetectionOverlay.js` |
| `data/generated/` | Demo media for tests (gitignored) | `demo_road.mp4`, `plate_*.png` |

## API Route Table

Roles are enforced by `app/api/deps.py` (`require_cop`, `require_volunteer`, `require_admin`).

| Method | Path | Roles |
| --- | --- | --- |
| GET | `/api/v1/health`, `/api/v1/healthz` | public |
| POST | `/api/v1/auth/login`, `/register` | public |
| POST | `/api/v1/auth/refresh` | public (refresh token) |
| GET | `/api/v1/auth/me` | any authenticated |
| GET | `/api/v1/users` | ADMIN |
| GET/POST | `/api/v1/complaints` | CITIZEN (post), COP/ADMIN (list) |
| GET | `/api/v1/complaints/mine` | CITIZEN |
| GET | `/api/v1/complaints/{id}` | owner, COP, ADMIN |
| POST | `/api/v1/complaints/{id}/verify` | COP |
| POST | `/api/v1/complaints/{id}/reject` | COP |
| GET | `/api/v1/hotlist` | COP, ADMIN |
| POST | `/api/v1/hotlist` | COP |
| GET/PATCH | `/api/v1/hotlist/{id}` | COP |
| DELETE | `/api/v1/hotlist/{id}` | ADMIN |
| POST | `/api/v1/sightings` | VOLUNTEER, COP |
| GET | `/api/v1/sightings`, `/{id}` | COP, ADMIN |
| GET | `/api/v1/vehicles/{plate}` | COP, ADMIN |
| GET | `/api/v1/vehicles/{plate}/timeline`, `/route` | COP, ADMIN |
| GET | `/api/v1/devices` | owner, ADMIN |
| POST | `/api/v1/devices/register` | VOLUNTEER, COP |
| POST | `/api/v1/devices/{id}/revoke` | ADMIN |
| GET | `/api/v1/alerts` | COP, ADMIN |
| GET | `/api/v1/analytics/{overview,detections,hotlist-matches,locations,traffic}` | COP, ADMIN |
| POST | `/api/v1/scanner/scan` | VOLUNTEER, COP |
| GET | `/api/v1/geo/heat` | COP, ADMIN |

## WebSocket Endpoints

### `/api/v1/ws/police` — dashboard alert stream
Auth via `?token=` query param **or** a first `{type:"auth"}` message. Server pushes
`{"type":"hotlist_detection","payload":{…}}`. The dashboard retries every 3 s
(`src/hooks/usePoliceSocket.ts`) and shows "Reconnecting to the alert stream" while down.

The payload carries `sighting_id`, `plate`, `latitude`, `longitude`, `timestamp`,
`confidence`, `hotlist_id`, **`camera`**, `device_id`, `last_seen_at`. `camera` is
`device.device_name or device.device_type or "unknown"` — a cop deciding whether to
roll needs to know whether this is the junction camera or a volunteer's phone, and an
alert with only coordinates is not actionable at night. Both broadcast sites send it
(`live_scan.py:_check_hotlist_and_record`, `sightings.py:create_sighting`), so the two
paths into the dashboard's feed look identical. `AlertEvent` in
`police_dashboard/src/services/api.ts` types the new fields as optional, since rows
written before this change do not carry them.

`SightingOut` exposes `plate` and `camera` as **derived** properties on the model
(`Sighting.plate`, `Sighting.camera`), not columns. There is deliberately no `plate`
column on `sightings` — the plate belongs to the hot-list entry and duplicating it
would let the two drift apart — but a sightings list showing only `hotlist_id` cannot
be matched to anything on screen, so the value is read through the relationship. That
means `list_sightings` eager-loads `hotlist` and `device` with `selectinload`; without
it, rendering 500 rows costs 1000 extra queries. No migration is involved.

### `/api/v1/ws/scan` — live detection (volunteer phone)
Only mounted when `DEMO_MODE` is true. Max 3 concurrent connections.

Protocol — **three** replies per frame, not one, and the split is the whole design:

```
→ {"type":"auth","token":"<jwt>","device_id":"<uuid>"}   (must be first; 10 s timeout)
← {"type":"ready"}
→ {"type":"frame","seq":1,"w":1280,"h":844,"lat":17.36,"lng":78.51,"jpeg_b64":"…"}
← {"type":"boxes","seq":1,"w":1280,"h":844,"ms":60,"vehicles":[{"track":1,"label":"CAR 1","cls":2,"conf":0.82,"box":[…]}]}
← {"type":"plates","seq":1,"plates":[{"track":1,"conf":0.44,"box":[…]}]}
← {"type":"plate","track":1,"label":"CAR 1","seq":1,"text":"…","norm":"MH12JK4567","conf":0.997,"valid":true,"stolen":true}
← {"type":"error","code":"size_limit"|"bad_frame"|"missing_model", …}
← {"type":"ping"|"pong"}                                  (server pings every 20 s)
```

- Three replies because both detector passes plus decode measure ~190 ms here, so one
  message would put every green box behind the plate pass. `boxes` goes out at
  ~80 ms p50, `plates` at ~150 ms, and `plate` seconds later. The client keys both
  rectangles off `track`, so a plate box arriving a frame late still lands in the
  right vehicle. Both `track` values may be negative (see orphan plates above); the
  overlay stores them in a `Map` and never assumes they index anything.
- The token travels in the first message, **never in the URL**.
- Close codes: `4401` bad/missing token or auth timeout, `4403` role or device
  rejected, `4404` too many connections, `1013` server shutting down.
- **Vehicle model is YOLOv8n COCO** (`backend/models/yolov8n.pt`, downloaded
  idempotently by `start.bat`). Classes 2/3/5/7 → `CAR` / `BIKE` / `BUS` / `TRUCK`,
  and **no `AUTO` label is ever emitted** — COCO has no auto-rickshaw class, and
  `plate_model_source/` ships no Indian vehicle detector, so a chip that said "AUTO"
  could never be true. Numbers are per class, reusing the lowest free one.
- Tracking is **per connection** (`backend/app/ws/scan_manager.py`, one
  `VehicleTracker` per socket, never shared). The detector alone only answers
  "there is a car at […]"; without ids the overlay re-keys every rectangle each
  frame and "CAR 1 / CAR 2" shuffle by confidence order. Ids are never recycled
  inside a connection, so a `plate` reply arriving seconds late cannot paint a
  stolen plate onto a different car.
- Boxes are normalised 0..1 against the captured image. Vehicles `#22C55E` @3 px with
  a solid green chip, plates `#FF1F1F` @3 px with a red chip carrying the text and
  confidence (`"reading…"` before it lands).
- **`WORKING_WIDTH` is 1280, not 640, and that is a measured decision.** On
  `data/test_plates/road_scene.jpg`:
  - 640 px input → plate is 90×18 px → OCR returns nothing at all.
  - 960 px → 139×28 px → reads only at q40, and *worse* at q60/q80.
  - 1280 px → 185×39 px → reads 0.991 (q40) … 0.996 (q80).

  1280 is also faster to serve (no resize; a 1280→960 PIL bilinear costs more than
  the JPEG decode it saves) and it is the width `expo-camera` gives both the preview
  and the capture anyway. On Android `pictureSize` feeds *both* the Preview and the
  ImageCapture `ResolutionSelector` (`ExpoCameraView.kt`), so a small value would
  blur the live preview, not just the frames.
- `PLATE_IMGSZ` stays 640 and `PLATE_CONF`/normalisation in `ai/` are **untouched**:
  the plate reading is already correct and must stay byte-for-byte identical. Only
  `VEHICLE_IMGSZ` moved (416 → 384). ultralytics costs ~50 ms fixed per call on
  this CPU, which is why the plate detector runs **once per frame** and each plate is
  matched to the smallest vehicle box containing its centre.
- **Frame size is two bands, not one cap.** `SOFT_FRAME_BYTES` (300 KB) is logged and
  the client downscales; `HARD_FRAME_BYTES` (400 KB) returns `size_limit`. This
  replaced a flat 300 KB reject that silently killed live detection on any phone with
  a modern sensor. The client (`liveCapture.js`) resizes on-device so a normal frame
  never needs the server-side fallback, and the server refuses rather than decodes
  anything over the ceiling.
- **Two in-flight slots, one per stage** (`ScanConnection.frame_processing` /
  `plate_processing`). Holding one slot across both stages made the server refuse the
  next frame for the whole plate pass (~60–400 ms) while the phone — which paces on
  `boxes` — offers its next frame ~120 ms later: roughly 1 frame in 10 was thrown
  away and the phone saw a stalled feed. Splitting them took the stream benchmark
  from 1.2 fps / 2 unanswered to 4.2 fps / 0 unanswered.
- **OCR is capacity-gated** (`OCR_MAX_IN_FLIGHT = 2`, `OcrClaim`). OCR costs ~2 s per
  crop while a frame is ~200 ms, so an ungated stream grows the queue without limit
  and every queued crop burns cores the green boxes need. A track already in the
  pipeline is skipped *without* spending one of its `MAX_READ_ATTEMPTS`, otherwise a
  track could be settled after three gated-out frames and never read at all.
- **Read budget**: `MAX_READS_PER_FRAME = 3` crops per frame, `MIN_PLATE_PX = 45`,
  `MAX_READ_ATTEMPTS = 3` per track, stopping at `PLATE_CONF_DONE = 0.8`. Both the
  per-frame and the pixel minimum were tightened downward while nothing was reaching
  this code, which is how a frame with a third plate in view ended up silently unread.
- **A plate with no vehicle around it is still reported, drawn and read.** The plate
  detector and the vehicle detector are separate models and they disagree: the vehicle
  pass can miss a car that is half out of frame, at night or behind a pillar, while the
  plate pass still sees the rectangle it was trained to find. Those were being dropped
  (`assign_plates` did `continue` on no host) on the grounds that drawing them produced
  "stray red rectangles floating over empty road". That was the wrong trade — the
  dropped detection was usually the one car worth looking at, and the phone keys plate
  boxes on nothing but the track id the server sends, so reporting one costs it nothing.
  Orphans now get a **negative** track id (`ORPHAN_TID_BASE = -1`, counting down; the
  tracker counts up from 1, so the ranges cannot meet). They have no per-car budget to
  hang on, so they are charged against `ScanConnection.orphans`, keyed by a coarse
  position cell — `ORPHAN_MAX_ATTEMPTS = 1`, `MAX_ORPHAN_READS_PER_FRAME = 1`,
  `ORPHAN_MEMORY_SECONDS = 30`. A sign on a wall gets one look, ever; a jittering box
  does not earn a fresh budget each frame; and cars are spent first because
  `infer_plates` fills its crop list from tracks before it looks at orphans. An orphan
  is sent with `"label": null`, because a chip reading "CAR 1" over a plate with no car
  would be a lie. Pinned by `backend/tests/test_orphan_plates.py`.
- Backpressure: latest-frame-wins (a frame arriving while one is in flight is
  dropped), 15 fps per device. The server drops the whole burst while a frame is in
  flight, so it answers the frame it accepted rather than the newest one; newest-wins
  is the client's job (`liveScan.js` overwrites `_pending`).
- Privacy: a plate that is **not** hot-listed is read, returned to that one phone, and
  then discarded. It is never stored, never counted anywhere, and never broadcast.
- Every processed frame also feeds the density grid: `traffic_accumulator.add(lat, lng,
  vehicles)` is called right after inference. It receives a coordinate and a per-class
  tally only — no plate, no image, no device id.

### The phone side — how the live view is put together

| File | Role |
| --- | --- |
| `src/screens/ScannerScreen.js` | All I/O: device registration, location, socket, capture loop, vibration, the event log. Renders the view component. |
| `src/components/ScannerScreenNew.js` | Presentation only (memoised): camera fills the top ~65%, compact panel below (last plate + confidence, stolen banner, recent events, LIVE/RECONNECTING/OFFLINE pill, fps + latency). |
| `src/services/liveCapture.js` | `LiveCaptureLoop` — one async loop, never two captures at once. Deps injected so the pacing logic is testable off-device. |
| `src/services/liveScan.js` | The socket. One instance, StrictMode-safe, re-auth on token refresh, seq-gated acks. |
| `src/components/liveOverlayStore.js` | External store (`useSyncExternalStore`) holding the current boxes. |
| `src/components/liveOverlayMath.js` | Pure normalised→screen projection. 37 unit tests, no React, no native. |
| `src/components/LiveDetectionOverlay.js` | Draws the rectangles. Reads the store directly. |

Four decisions that are load-bearing, and would be easy to undo by accident:

- **The overlay is an external store, not React state.** `CameraView` renders the
  video; if every `boxes` message were `useState`, the whole screen would re-render
  several times a second and the preview would stutter. The overlay subscribes to the
  store, and only the rectangles move.
- **The projection is pure and separately tested.** The frame is 4:3 and the preview is
  a taller crop of it, so the mapping is cover-with-crop, not a plain scale —
  `liveOverlayMath.js` is where that lives, with the 4:3-on-a-taller-preview case
  pinned as a test.
- **Animation is `Animated` with `useNativeDriver`, not Reanimated.** Reanimated is not
  installed. Only `transform` and `opacity` are animated, and only `translateX/Y` +
  `opacity` are used — those are the properties that can go on the native thread.
- **`skipProcessing: false` in `CAPTURE_OPTIONS`.** This is the bug that made live
  detection look dead: `skipProcessing: true` returns EXIF `width`/`height` that are
  often `-1` and *ignores* `quality`, so every frame arrived with a bogus size and a
  full-size payload. Verified against the installed expo-camera 58.0.7 source
  (`Options.kt`), not from memory.
- **`MIN_PICTURE_WIDTH` is 1280 and it is not negotiable.** The plate detector +
  OCR need it: at 640 the plate is 90×18 px and OCR returns *nothing*; at 960 it
  reads only at q40 and gets *worse* at higher quality; at 1280 it reads 0.991–0.996.
  On Android `pictureSize` also feeds the Preview `ResolutionSelector`
  (`ExpoCameraView.kt` builds one selector and hands it to both use cases), so a
  narrower capture would blur the preview as well — two reasons for the same floor.

### Never read a temp file back — this is what caused the crash

The `FileSystemFile.base64` ENOENT in the Metro log was not a permissions problem or
a camera bug. `_downscale()` did:

```js
const file = new File(uri);            // manipulator wrote this, fine
await manipulator.manipulate(uri).saveAsync({ compress, resize })  // ok
const base64 = file.base64();          // <- NOT awaited
```

`File.base64()` returns a **Promise** in expo-file-system 58 (the sync twin is
`base64Sync()`), and `File.delete()` is synchronous. So the un-awaited Promise was
`JSON.stringify`d as the frame body — `{}` — while `delete()` unlinked the file that
the native read was still queued against. Hence
`java.io.FileNotFoundException: .../cache/ExperienceData/.../ImageManipulator/<uuid>.jpg:
open failed: ENOENT`. The `ImageManipulator/` path in the message is what proves the
crash came from the resize step, not the camera's own temp file.

The rule now: **get the bytes in the same call that creates the image.** The resize
pass passes `base64: true` in `SaveOptions` and reads `ImageResult.base64`
(`expo-image-manipulator` 58.0.11, `ImageManipulator.types.d.ts`). No `File` is ever
constructed to read bytes back. Temp files are deleted only *after* the frame is sent,
inside the same awaited `try`/`finally`, and an already-collected file is ignored.

The ENOENT itself was a race, so the regression guard is the deterministic half:
`fsStats.reads === 0` in `mobile_app/src/services/__tests__/liveCaptureLoop.test.js`.
Never reading a file removes the window that the race lived in.

`expo-image-manipulator` is still a real dependency, deliberately: Android's
`ResolutionStrategy(size, FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER)` can hand back a
size *larger* than requested (1920), so the net is genuinely needed, not vestigial.

### The loop cannot be started twice

`loop.stop()` clears `_running` *before* awaiting, and the screen used to call it
fire-and-forget. A focus event landing in that window started a second loop while the
first was still mid-iteration. Now `start()` chains on the previous `stop()`'s
completion, a generation counter retires superseded loops, and one boolean lock is
taken before any `await` in an iteration and released in `finally`. Every async call
inside one `try`/`catch`/`finally` logs exactly one concise line per failure type.

After 5 consecutive capture failures the loop pauses 2 s, surfaces "Camera error,
retrying", and resumes. `onRecover` fires only once a frame actually succeeds, so the
message clears when the camera really came back rather than when the timer elapsed.

Removed on purpose: the Scan-plate shutter button, the guide frame, the dashed
rectangle and the hint strip. There is no shutter on a live feed, and text drawn over
the plate area lowers plate-read accuracy on screen. `useKeepAwake` is held only
while the stream is actually running, so the rest of the app still sleeps.

## Maps Heatmap

### `GET /api/v1/geo/heat` — COP/ADMIN only
A density grid for the police dashboard. Two layers behind one response shape.

| Param | Meaning |
| --- | --- |
| `layer` | `traffic` (default) or `stolen` |
| `from`, `to` | ISO timestamps. Default: the last 15 minutes. Max span 7 days. |
| `vehicle_class` | `two_wheeler` \| `car` \| `bus` \| `truck`. Traffic layer only. |
| `bbox` | `south,west,north,east`. |

Returns `{cells: [{lat, lng, w, n}], max, min, generated_at}`.

- `w` is **vehicles per processed frame** for `traffic`, and the **time-decayed sighting
  count** (`exp(-age_hours / HEAT_TAU_HOURS)`) for `stolen`. Both are normalised to 0..1
  client-side, which is why one renderer draws both.
- Per-frame average, not a raw total: a cell watched for ten minutes and one watched for a
  minute have to be comparable, and only the per-frame average is.
- A cell needs ≥ 3 frames before it is shown, so one frame with two cars does not paint as
  bright as a junction with 200.
- Redis-cached for 10 s; a cache outage degrades to a direct read, never a failure.
- RBAC is the point: 401 with no token, 403 for CITIZEN and VOLUNTEER.

### `traffic_cells`
One row per (cell, UTC hour, real-or-synthetic).

```
id, cell_lat, cell_lng, hour_bucket, frames,
two_wheeler, car, bus, truck, synthetic
```

Counts only. No plate, no image, no device id, not even the raw lat/lng — a density grid
that can be joined back to a single vehicle is not a density grid.

- `frames` counts every processed frame, including frames that saw nothing, so an empty
  road reads as low density rather than as missing data.
- `synthetic` is part of the unique key, so demo rows can never merge into real ones and
  `--purge` can remove exactly them.
- Cells are ~110 m (0.001°), deliberately coarser than a phone's GPS error.
- `synthetic is false` rows appear automatically once a phone scans a road.

### Timezone gotcha (cost real debugging time, worth keeping in mind)
`hour_bucket` buckets in **UTC**. `date_trunc('hour', ts)` on a `timestamptz` truncates in
the **session** timezone, which here is `Asia/Calcutta` — so a UTC-bucketed table and an
IST-truncated window disagree by 5½ hours and the query silently returns nothing. `_utc_hour`
in `backend/app/api/v1/geo.py` floors the window in Python instead. Do not reintroduce
`date_trunc` on a timestamptz here.

### Flushing
15 fps × several phones would be thousands of statements a minute for data only ever read as
an hourly aggregate. Counts accumulate in memory per (cell, hour, class) and flush as one
upsert every 5 s, on socket disconnect, and at shutdown. A failed flush puts the batch back
rather than dropping it. Retention (90 days) runs at startup.

### `scripts/seed_traffic.py`
Synthesises a Hyderabad-area grid (8 corridors, diurnal shape) so `/maps` is not empty on a
fresh database. `--purge` before seeding keeps it idempotent; `--purge-only` and `--prune`
are also available. Every row is tagged `synthetic = true`. `start.ps1` runs it once, guarded
by its own `.rakshak/seed-traffic.done` marker.

## Database & Alembic

- **Tables**: `users`, `devices`, `complaints`, `hotlist`, `sightings`, `audit_logs`,
  `traffic_cells`.
- **Chain**: `0001_initial` → `0002_perf_indexes` → `0003_traffic_cells` (**head**).
- **Local DB** `alembic_version` = `0003_traffic_cells` (in sync with head). Upgrade and
  downgrade round-trip verified.

## Environment Variables

Defined in `backend/app/core/config.py`, documented in `.env.example`:

`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `DATABASE_URL`, `REDIS_URL`,
`JWT_SECRET`, `JWT_ACCESS_MINUTES`, `JWT_REFRESH_DAYS`, `HOTLIST_CONFIRMATION_HOURS`,
`DEMO_MODE`, `CORS_ORIGINS`, `CORS_ALLOW_PRIVATE_NETWORK`, `STORAGE_BACKEND`,
`STORAGE_LOCAL_PATH`, `AI_MODEL_PATH`, `OCR_MODEL_PATH`, `API_BASE_URL`,
`DETECTION_COOLDOWN_SECONDS`, `MAX_UPLOAD_MB`, `RATE_LIMIT_DETECTIONS_PER_MINUTE`,
`RATE_LIMIT_AUTH_PER_MINUTE`, `HEAT_TAU_HOURS`, `VITE_API_URL`, `VITE_WS_URL`,
`EXPO_PUBLIC_API_URL`.

Web clients read `VITE_API_URL` / `VITE_WS_URL`; the mobile app reads
`EXPO_PUBLIC_API_URL`. `mobile_app/.env` ships it **empty on purpose** — the app derives
the host from `Constants.expoConfig.hostUri` (Metro's LAN address) so the QR works from any
PC on the network.

## Tests

`backend/tests/`, run with `pytest` from `backend/` (`pythonpath = ..` in `pytest.ini`).

| File | Covers |
| --- | --- |
| `test_auth.py` | login, refresh, role checks |
| `test_complaints.py` | filing, verification, proof upload |
| `test_health.py` | healthz |
| `test_plate.py` | plate normalisation, state-code whitelist |
| `test_privacy.py` | 415 on video/multipart, no imagery columns on `sightings`, `/ws/scan` auth gate |
| `test_sightings.py` | hot-list match, cooldown suppression, last-seen update, police WS alert |
| `test_geo.py` | `/geo/heat` RBAC, bbox / window / class validation, hour-bucket window trap |
| `test_heat_layers.py` | per-frame density, min-frames threshold, time decay, no-attribution |
| `test_traffic_grid.py` | cell maths, GPS-fix rejection, batching, failed-flush retention, retention sweep |
| `test_live_scan_frames.py` | working width matches what the app sends, ceiling ordering, aspect ratio, read budget |
| `test_live_scan_ws.py` | the whole wire contract end-to-end with the detectors stubbed: `boxes`/`plates`/`plate` ordering, per-connection trackers, auth + RBAC close codes, latest-frame-wins, split in-flight slots, OCR queue depth, privacy (a non-hot-listed plate stores nothing) |
| `test_scan_tracker.py` | `VehicleTracker`: IoU matching, id stability, per-class chip numbering, expiry, plate read budget |
| `test_orphan_plates.py` | a plate with no vehicle around it is drawn and read under a negative id; one read per frame, one attempt ever, jitter does not earn a fresh budget, the entry does not outlive its object |

Shared grid fixtures live in `geo_helpers.py` (not a test module).

179 tests, all passing (`pytest -q -p no:randomly`, ~100 s).

`scripts/live_scan_test.py` is the live-path check (needs the backend running) —
24 checks covering auth rejection, RBAC, a self-calibrating probe of what the OCR
actually reads, streaming throughput, the stolen-vehicle alert, the
no-imagery-leaves-the-host guarantee, both frame-size bands, and stale-frame
dropping. It registers a **fresh device id per phase** because the sighting
cooldown key is `plate:<plate>:<device id>`, and it hard-codes no plate name: the
probe learns one from the fixture and asserts against that.

Its input fixture, `data/test_plates/road_scene.jpg`, is built by
`scripts/make_test_road_scene.py` (`--check` re-verifies it): an MIT-licensed
sample road photo with the project's own synthetic plate renderer composited over
the photo's blurred plate region. Real photos of Indian roads with a legible plate
are not redistributable, and a synthetic plate on a real background is the only way
to keep a *fixed* expected plate string in a committed fixture. Multi-vehicle
behaviour is covered by the deterministic `VehicleTracker` unit tests instead.

Mobile tests: `node --test src/components/__tests__/liveOverlayMath.test.js
src/services/__tests__/liveCaptureLoop.test.js` — **43 pass** (37 overlay maths, 6
capture-loop). Run from `mobile_app` with explicit paths; from the repo root Node
reports `MODULE_NOT_FOUND`.

`liveCaptureLoop.test.js` is the regression harness for the ENOENT crash and it is
built to be hard to pass by accident:

- 500 iterations against a hostile camera (fails, deletes files, throws) — asserts
  **zero** uncaught rejections, `fsStats.reads === 0`, and `maxInFlight === 1` on both
  the camera and the socket.
- Two mutations prove it is not vacuous. Reinstating the old read-back fails on "no
  read-back, even on the resize path"; restoring the original lifecycle fails
  deterministically on "timed out after 30000ms waiting for: 2 captures".
- The breaker, the resize path, and the capture options are pinned separately.
- Native seams (`fs`, `location`, `image`) are lazy and injectable, and the pacing
  constants (`minIntervalMs`, `slowIntervalMs`, `idleTimeoutMs`, `failureBackoffMs`)
  are injected too, so the whole loop runs under `node --test` with no device.

### The three end-to-end scripts

| Script | Answers | Needs |
| --- | --- | --- |
| `scripts/chain_check.py` | the whole chain through the real API — complaint → Verify → hot-list immediately → frame → STOLEN banner → `/ws/police` alert within 3 s → one sighting row (60 s cooldown) → heat cells → the plate coming off the list stops it. **33/33.** | backend running |
| `scripts/heat_check.py` | items 5 and 7 over the public API only: 401 without a token, 403 for CITIZEN and VOLUNTEER, both layers, the 7-day and 5,000-cell caps, the 10 s cache via `generated_at`, and that `/sightings` refuses multipart (415), refuses image/video fields (422), and has no imagery column. **14/14.** | backend running |
| `scripts/speed_check.py` | item 8 — 1 / 4 / 6 vehicles at fixed width, closed loop and saturated. Budget: 150 ms p95 on the boxes reply. | backend running |

`chain_check.py` hard-codes no plate name: it reads `MH12JK4567` from the fixture and
asserts against that, so the check cannot pass on an OCR result nobody actually got.

### `scripts/render_live_view.py`

Draws what the phone would draw — green `#22C55E` vehicle rectangles with a solid chip,
red `#FF1F1F` plate rectangles with the text and confidence below — onto
`data/test_plates/road_scene.jpg`, for `--repeat N --motion 0.035` consecutive panned
frames so the tracking and the prediction can be eyeballed. `--hotlist` reads the real
hot-list table so a stolen plate is drawn the way it will actually appear.

This script earned its place by finding a real bug: it scaled `y` by the frame *width*,
which stretched every rectangle 1.52× down the image and looked plausible in a thumbnail.
`_to_px(box, width, height)` is now the only place normalised coordinates become pixels,
and the row/column runs it prints are checkable numerically when you cannot eyeball the
image. It draws with `cv2.getTextSize` / `putText` on purpose — no Pillow — so it agrees
with the model pipeline about what a pixel is.

### `scripts/make_test_road_scene.py`

Builds and (`--check`) verifies `data/test_plates/road_scene.jpg`. See the fixture note
under Tests above for why the fixture is composited rather than photographed.

## Police Dashboard Routes

Lazy-loaded one-per-page in `src/App.tsx`, sidebar defined in `src/components/Layout.tsx`.

| Path | Page | Notes |
| --- | --- | --- |
| `/` | Overview | |
| `/maps` | **Maps** | Leaflet + `leaflet.heat`. Lazy because it pulls in Leaflet. |
| `/alerts` | Live Alerts | fed by `usePoliceSocket`, unchanged |
| `/complaints` | Complaints | |
| `/hotlist` | Hotlist | |
| `/search`, `/vehicles/:plate` | Vehicle Search / Detail | Detail's `VehicleMap.tsx` untouched |
| `/analytics` | Analytics | Recharts behind its own chunk |
| `/admin` | Admin | ADMIN only |

`leaflet.heat@0.2.0` is pinned exact and is the only dependency added. It ships no types, so
`src/types/leaflet.heat.d.ts` declares the `L.heatLayer` factory it installs.
`src/components/map/HeatLayer.tsx` wraps it as a react-leaflet child and normalises weights
client-side — the server's `max` spans the whole window, and the visible viewport may hold
only the quietest tenth of the data, so server-side normalisation would paint the visible
area uniformly pale.

## Conventions

- UUID primary keys; all timestamps UTC in the database.
- Redis is a **cache**, never a source of truth; every read degrades to PostgreSQL.
- Images travel as base64 JSON, not multipart (multipart is rejected by middleware).
- Roles: `CITIZEN`, `VOLUNTEER`, `COP`, `ADMIN`.
- `privacy_guard` middleware returns `415` for any `multipart/form-data` or `video/*`
  body outside `POST /api/v1/complaints`.

## Found but not working or missing

- **Event bus / OD-flow pipeline: deliberately not merged.** `maps-feature` (`9e2c419`,
  parent `d2a6196` "wip: interrupted maps session snapshot") carried a second, unfinished
  traffic stack — `services/bus/` (redis-streams + Kafka), `ingest.py` / `ingest_consumer.py`
  / `ingest_service.py`, `maps.py` / `maps_service.py`, `traffic_sim.py`, `geo_support.py`,
  `plate_fuzzy.py`, `0003_maps_traffic_od_cameras.py`, `test_maps.py`. It defines its **own
  `traffic_cells`** (`camera_id`, `vehicle_class`, `count`) against the same table name this
  branch uses (`frames`, `two_wheeler`, `car`, `bus`, `truck`, no camera), and its migration
  claims revision id `0003` alongside `0003_traffic_cells.py`. Both cannot be true at once,
  and two writers with different count semantics would corrupt the verified heatmap. The
  merge therefore keeps this branch's grid, its `/api/v1/geo/heat` and its `0003_traffic_cells`
  migration, and takes from the branch only what does not collide: the `/analytics/traffic`
  snapshot (rewritten onto this grid — it reports per-hour volume, per-class mix and the
  busiest grid cells, not cameras, because the grid has no camera dimension) and the
  "View heat" link on `VehicleDetail`. The branch itself is untouched, so the dropped code
  is still reachable at `maps-feature`.
- **Demo camera seeding**: the `Camera` model, `cameras`/`od_flows` tables, `seed_cameras()`
  and the synthetic traffic simulator were dropped with the rest of that stack — nothing on
  this branch writes or reads them. `/api/v1/maps/*` (`/cameras`, `/traffic`, `/od-flows`,
  `/summary`) therefore does not exist; the dashboard's only map data source is
  `GET /api/v1/geo/heat`.
- **Expo SDK patch drift**: `expo-doctor` 19/20. Installed versions are one patch behind
  what SDK 58 wants (`expo` 58.0.2 vs ~58.0.3, `expo-camera` 58.0.7 vs ~58.0.8,
  `expo-font`, `expo-image-manipulator`, `expo-location`, `expo-splash-screen`). This
  predates the live-view work and is still deliberately left alone: the live capture
  options are pinned against the **installed** expo-camera 58.0.7 native source
  (`Options.kt`, `ExpoCameraView.kt`) — `skipProcessing`, `shutterSound`'s Android
  default and the shared Preview/ImageCapture `ResolutionSelector` were all read out of
  that tree, not out of a changelog. Bumping a patch to silence a cosmetic warning would
  invalidate every one of those facts and could not be re-validated here, because no
  device build can be run in this environment. `expo-keep-awake` (the one package this
  work added) is at the version SDK 58 wants.
- **`backend/data/uploads/`**: `STORAGE_LOCAL_PATH` is the relative `./data/uploads`, so
  running pytest or uvicorn from `backend/` creates a second uploads tree that the root
  `.gitignore` rule never matched. Now ignored explicitly.
- **`tree.txt`** at the repo root is a leftover scratch file from an earlier session; untracked.
- **`scripts/start.sh` is syntax-unverified**: no WSL on this machine, so `bash -n` cannot
  run. The `PYTHONPATH` fix it carries is mirrored from `start.ps1` and untested there.
- **Live-scan latency: green boxes 80 ms p50 / 341 ms p95**, round trip 83 / 377 ms,
  plate boxes 196 / 736 ms, and the OCR text seconds after that — measured
  end-to-end over the real WebSocket with the real models (`scripts/live_scan_test.py`,
  83 frames, 4.2 fps, 0 unanswered, 105.8 s CPU over 22.1 s wall = 4.79 cores, peak
  RSS 556 MB, on an 8-core box). The p50 target of 120 ms is met; **the p95 is not**.
  The earlier ~240 ms figure was two things at once: the old single `boxes` message
  that waited for the plate pass too, and one in-flight slot held across both stages.
  Splitting the stages and answering `boxes` first is what moved it.
- **The p95 tail is OCR contending with the pass the phone renders, not detection
  cost.** `scripts/probe_leg_contention.py` sends the *same* 1280 px one-car frame
  three ways and the box reply is **2.7x** slower when the plate reads: 317 ms p50 /
  1289 ms p95 with the plate readable, 118 / 230 with the plate cropped out of frame,
  122 / 709 with it blurred. Both legs share one process and
  `torch.set_num_threads(2)` (`live_scan.py:57`), so a readable plate costs the
  vehicle pass its threads for the ~2 s the OCR takes. Once a car's plate has been
  read (conf ≥ 0.8 sets `plate_settled`) no more OCR is queued for it and the box
  timing returns to baseline. So the cost curve has to be read from the *unreadable*
  column; the readable column is contention on top.
  - **`torch.set_num_threads(2)` is deliberately not raised.** It would clear most
    of the p95 on this 8-core laptop and push the same work onto a 4-core target
    machine. That is a hardware decision, not a bug fix. The alternative — moving OCR
    to its own process — removes the contention outright but doubles the resident
    model set.
  - `scripts/speed_check.py` measures 1 / 4 / 6 vehicles at a **fixed 1280 px width**,
    because an earlier version pasted cars side by side and so tripled the pixels to
    decode along with the vehicle count — it was measuring the JPEG decoder, not the
    cost of another detection. Geometry shrinks each car as the count grows, so at 6
    the plates are too small to read; the box timing is what is under test. It reports
    a closed-loop run (one frame in flight, what the phone does) and a saturated run
    (250 ms regardless) separately, and budgets only the closed loop.
  - `scripts/speed_notes.py` is the reconciliation, written because the three probes
    disagreed. One of them, `scripts/probe_static_scene.py`, is **void**: it ran both
    phases on one connection, so the first phase read the plate and set
    `plate_settled`, and the "static" phase inherited an already-settled track, never
    queued a crop and so was never contended. It is kept, not deleted, because anyone
    comparing the two scripts would otherwise think they contradict each other.
- **`POST /complaints/{id}/reject` cannot undo a Verify.** It only accepts `PENDING`
  or `UNDER_REVIEW` and returns `409` on a `HOTLISTED` complaint. That is deliberate
  (a verified theft report should not be discarded by a later click), but it means
  "rejecting removes the plate" is the wrong mental model for the hot-list path: once
  a plate is listed, `PATCH /hotlist/{id}` with `{"status":"CLOSED"}` is what takes it
  off (`hotlist_service` → `cache_service.remove_active_plate`), and that is what a cop
  uses when the car is recovered. Both are exercised by `scripts/chain_check.py`.
- **`GET /api/v1/sightings` has no `plate` filter** — only `hotlist_id`. Any other
  query parameter is silently ignored and the response is the most recent 500 rows for
  *every* plate. A `?plate=` request looks like it works and quietly returns unrelated
  rows, which is how a privacy check counting sightings can pass or fail for the wrong
  reason. Filter client-side until there is a server-side filter worth having.
- **`/api/v1/geo/heat` caching is only observable via `generated_at`.** The query is a
  couple of cells wide on a demo database, so a cache hit and a miss differ by less
  than request overhead and any timing-based check of the 10 s TTL is pure noise.
  `generated_at` is stamped when the payload is *built*, so an identical timestamp
  across two calls proves the cache and a new one after the TTL proves the expiry —
  that is what `scripts/heat_check.py` checks.
- **The demo road video yields plate detections but no COCO vehicle detections**, so a live
  `/ws/scan` run writes cells with `frames = N` and all vehicle counts zero. That is correct
  (an empty road is low density, not missing data) but means real density needs a real road.
  `scripts/seed_traffic.py` is what fills the map for a demo.
- **`scripts/e2e_verify.py` crashes on a cp1252 console** when a plate OCR reason
  contains a non-ASCII fragment (`UnicodeEncodeError` on `型`). Harmless to the checks
  themselves — run with `PYTHONIOENCODING=utf-8` — but the script should set
  `sys.stdout.reconfigure(encoding="utf-8")` so a plain run does not die.
- **The phone-side frame rate, preview smoothness and on-screen box alignment cannot be
  verified in this environment.** Everything measurable was measured against a real
  WebSocket with the real models and the real fixture, and every line of drawing maths
  is unit-tested, but three things need a device in a hand:
  1. the actual capture rate — `takePictureAsync` on a real sensor is slower than the
     loop's own pacing assumes, and `liveCapture.js` backs off when the round trip
     exceeds 800 ms, so the phone will settle below the benchmark's 4.5 fps;
  2. whether the preview stutters while frames are in flight;
  3. whether the rectangles land on the plates — i.e. whether the cover-with-crop
     projection in `liveOverlayMath.js` agrees with what `CameraView`'s
     `ResolutionSelector` actually puts on screen. Its 4:3-frame-on-a-taller-preview
     case is a unit test of the maths, which is not the same claim.
  `scripts/render_live_view.py` covers the geometry against a still image, and it is
  worth running by eye on the three output frames before trusting the projection.
