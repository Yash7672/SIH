# Project Map

reviewed at commit `live-detect` (branch) — see `git log -1`
Last full review: 2026-10-04, after the live-detection and Maps-heatmap work.

## Architecture Overview

RAKSHAK is a "Privacy-First Crowdsourced ANPR Vehicle Tracking System".

- **Backend**: FastAPI (Python 3.12), PostgreSQL 17 (source of truth), Redis 7 (cache only).
- **AI Engine**: YOLOv8n (`backend/models/yolov8n.pt`) for vehicles, a fine-tuned
  YOLOv8n plate detector (`license-plate-finetune-v1n.pt`), RapidOCR (ONNX) for plate text.
  Inference runs **on the PC backend**, never on the phone — Expo Go cannot run
  native ML, and a frame that never leaves the process is a frame that cannot leak.
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
| `backend/app/services/` | Business logic | `hotlist_service.py`, `sighting_service.py`, `plate.py`, `cache.py`, `seed.py` |
| `backend/app/ws/manager.py` | Police alert fan-out (`alert_manager`) | `broadcast()` |
| `backend/alembic/versions/` | Migrations | `0001_initial`, `0002_perf_indexes` |
| `ai/` | Model wrappers (repo-root package) | `detector.py`, `ocr_engine.py`, `pipeline.py` |
| `scripts/` | Launchers, seeds, diagnostics | `start.ps1`, `seed_demo.py`, `live_scan_test.py` |
| `citizen_web/` | Citizen SPA | `src/pages/`, `src/services/api.ts` |
| `police_dashboard/` | Police SPA | `src/pages/`, `src/components/map/VehicleMap.tsx`, `src/leaflet-theme.css` |
| `mobile_app/` | Expo app | `src/screens/ScannerScreen.js`, `src/services/liveScan.js`, `src/components/LiveDetectionOverlay.js` |
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
| GET | `/api/v1/analytics/{overview,detections,hotlist-matches,locations}` | COP, ADMIN |
| POST | `/api/v1/scanner/scan` | VOLUNTEER, COP |
| GET | `/api/v1/geo/heat` | COP, ADMIN |

## WebSocket Endpoints

### `/api/v1/ws/police` — dashboard alert stream
Auth via `?token=` query param **or** a first `{type:"auth"}` message. Server pushes
`{"type":"hotlist_detection","payload":{…}}`. The dashboard retries every 3 s
(`src/hooks/usePoliceSocket.ts`) and shows "Reconnecting to the alert stream" while down.

### `/api/v1/ws/scan` — live detection (volunteer phone)
Only mounted when `DEMO_MODE` is true. Max 3 concurrent connections.

Protocol:

```
→ {"type":"auth","token":"<jwt>","device_id":"<uuid>"}   (must be first; 10 s timeout)
← {"type":"ready"}
→ {"type":"frame","seq":1,"w":640,"h":480,"lat":17.36,"lng":78.51,"jpeg_b64":"…"}
← {"type":"boxes","seq":1,"w":640,"h":480,"ms":244,"vehicles":[…],"plates":[…]}
← {"type":"plate","plate_id":0,"seq":1,"text":"…","norm":"MH12JK4567","conf":0.99,"valid":true,"stolen":true}
← {"type":"error","code":"size_limit"|"missing_model", …}
← {"type":"ping"|"pong"}                                  (server pings every 20 s)
```

- The token travels in the first message, **never in the URL**.
- Close codes: `4401` bad/missing token or auth timeout, `4403` role or device rejected.
- Boxes are normalised 0..1 against the captured image. Vehicles `#FF8A00` @2.5 px,
  plates `#FF1F1F` @3 px.
- **Frame size is handled by downscaling, never by refusing.** `SOFT_FRAME_BYTES`
  (300 KB) is the point past which `decode_and_infer` resizes to `WORKING_WIDTH`
  (1280 px); `HARD_FRAME_BYTES` (8 MB) is an abuse ceiling that returns
  `size_limit`. This replaced a flat 300 KB reject that silently killed live
  detection on any phone with a modern sensor — a 12 MP capture is ~500 KB even at
  JPEG 0.3, so *every* frame was dropped and the app looked like it had no
  detector at all. The app additionally resizes on-device
  (`prepareLiveFrame`, a width × quality ladder measured against the real payload)
  so a normal frame never needs the server-side fallback.
- **Latency contract**: boxes go out as soon as detection finishes (~240 ms p50).
  OCR costs ~2 s per crop on CPU, so plate text is decoded on a separate single-worker
  thread pool and arrives later as a `plate` message. It never blocks the frame path.
- Backpressure: latest-frame-wins (a frame arriving while one is in flight is dropped),
  15 fps per device. Note the server drops the whole burst while a frame is in
  flight, so it answers the frame it accepted rather than the newest one; newest-wins
  is the client's job (`liveScan.js` overwrites `_pending`).
- **Read budget**: `MAX_READS_PER_FRAME = 3` crops per frame and `MIN_PLATE_PX = 45`.
  Both were tightened downward while nothing was reaching this code, which is how a
  frame with a third plate in view ended up silently unread.
- Privacy: a plate that is **not** hot-listed is read, returned to that one phone, and
  then discarded. It is never stored, never counted anywhere, and never broadcast.
- Every processed frame also feeds the density grid: `traffic_accumulator.add(lat, lng,
  vehicles)` is called right after inference. It receives a coordinate and a per-class
  tally only — no plate, no image, no device id.

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
| `test_live_scan_frames.py` | oversized frame downscaled not dropped, ceiling ordering, aspect ratio, read budget |

Shared grid fixtures live in `geo_helpers.py` (not a test module).

`scripts/live_scan_test.py` is the live-path check (needs the backend running) —
16 checks covering auth rejection, RBAC, streaming throughput, the stolen-vehicle
alert, the no-imagery-leaves-the-host guarantee, oversized-frame handling, and
stale-frame dropping.
Mobile overlay maths: `node --test src/components/__tests__/liveOverlayMath.test.js`.

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

- **Event bus**: source is **gone**. `backend/app/services/bus/` contains only stale
  `__pycache__` (`base`, `factory`, `kafka`, `redis_streams`, `__init__`); no `.py` files
  remain. Git history shows them in `9e2c419`/`d2a6196`, so the Python files were dropped
  in the GitHub restore and the `.pyc` files survived. Nothing imports it, so the app is
  unaffected — but the bus is not there.
- **Expo SDK patch drift**: `expo-doctor` 19/20. Installed versions are one patch behind
  what SDK 58 wants (`expo` 58.0.2 vs ~58.0.3, `expo-camera` 58.0.7 vs ~58.0.8,
  `expo-font`, `expo-image-manipulator`, `expo-location`, `expo-splash-screen`). Everything
  bundles and runs; left alone deliberately to avoid re-resolving `node_modules`.
- **`backend/data/uploads/`**: `STORAGE_LOCAL_PATH` is the relative `./data/uploads`, so
  running pytest or uvicorn from `backend/` creates a second uploads tree that the root
  `.gitignore` rule never matched. Now ignored explicitly.
- **`tree.txt`** at the repo root is a leftover scratch file from an earlier session; untracked.
- **`scripts/start.sh` is syntax-unverified**: no WSL on this machine, so `bash -n` cannot
  run. The `PYTHONPATH` fix it carries is mirrored from `start.ps1` and untested there.
- **Live-scan latency is ~240 ms p50 / ~570 ms p95**, not the 150 ms originally targeted.
  Both models together are ~110 ms; the rest is JPEG decode, base64, JSON and thread
  contention with the OCR pool on a 5-core i5. OCR is already off the hot path, so this is
  a CPU budget, not an architecture problem. Roughly 3–4 effective fps per phone.
- **The demo road video yields plate detections but no COCO vehicle detections**, so a live
  `/ws/scan` run writes cells with `frames = N` and all vehicle counts zero. That is correct
  (an empty road is low density, not missing data) but means real density needs a real road.
  `scripts/seed_traffic.py` is what fills the map for a demo.
- **`backend/app/ws/scan_manager.py` is an abandoned draft.** It holds a `ScanConnection`
  dataclass and an `IoUTracker` with no imports and no `scan_manager` instance, and
  `ws.py` had been edited to import from it — which broke the whole app at import time.
  Reverted; the file is untracked and unreferenced. Live scan lives in
  `app/api/v1/live_scan.py`. Extracting it is still reasonable, but as a deliberate
  refactor with the tracking behaviour ported deliberately, not left half-wired.
- **`scripts/e2e_verify.py` crashes on a cp1252 console** when a plate OCR reason
  contains a non-ASCII fragment (`UnicodeEncodeError` on `型`). Harmless to the checks
  themselves — run with `PYTHONIOENCODING=utf-8` — but the script should set
  `sys.stdout.reconfigure(encoding="utf-8")` so a plain run does not die.
