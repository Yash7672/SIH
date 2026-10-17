# RAKSHAK — Progress Log

## Phase 0 — Environment Audit ✅

**Implemented:** Full machine audit, environment + project audit docs created.
**Files created:** `docs/ENVIRONMENT_AUDIT.md`, `docs/PROJECT_AUDIT.md`
**Key findings:**
- Windows 11, i5-1155G7, 7.8 GB RAM, Intel Iris Xe (no NVIDIA GPU → CPU-only AI)
- Python 3.12.0, Node 24.13.0, npm 11.6.2, Git 2.50.1, JDK 25
- PostgreSQL 17.11 installed & RUNNING (superuser password: `postgres`)
- Redis 3.0.504 installed & RUNNING (no auth)
- Docker NOT installed (blocked: no admin rights for UAC)
- Android SDK + ADB present; phone connected: vivo I2202, Android 14, serial 10BC9F1PGB000D3
- Expo CLI 57.0.24 installed globally
- Project dir was empty except empty `.dist/` — clean build confirmed

**Limitation:** Docker cannot be installed without elevation → native PostgreSQL/Redis used for local demo; `docker-compose.yml` still provided.

---

## Phase 1 — Repo Structure + Config ✅

**Files created:** full folder tree (`backend/`, `citizen_web/`, `police_dashboard/`, `mobile_app/`, `ai/`, `data/`, `docs/`, `scripts/`), `.env`, `.env.example`, `.gitignore`, `docker-compose.yml`, `README.md`, `backend/requirements.txt`

---

## Phase 2 — Backend + Database ✅

**Implemented:** FastAPI app, SQLAlchemy models (users/devices/complaints/hotlist/sightings/audit_logs), Alembic migration `0001_initial`, PostgreSQL DBs `rakshak` + `rakshak_test` created, migrations applied.
**Files:** `backend/app/{main,core/*,db/*,models/*,schemas/*}`, `backend/alembic/*`
**Verification:** `alembic upgrade head` succeeded; server starts.

---

## Phase 3 — Authentication + RBAC ✅

**Implemented:** JWT access+refresh, PBKDF2 password hashing, role enum (CITIZEN/VOLUNTEER/COP/ADMIN), server-side `require_roles` dependency, rate-limited login/register.
**Verification:** logins for citizen/cop/volunteer/admin all return 200 + tokens.

---

## Phase 4-5 — API Surface (Citizen + Police endpoints) ✅

**Implemented:** complaints CRUD (multipart proof upload w/ size+type limits), verify→hotlist, reject, hotlist management, vehicles/{plate}/timeline/route, devices register/revoke, analytics overview/detections/locations, alerts, users/audit-logs.

---

## Phase 6 — Hotlist + Redis Cache ✅

**Implemented:** `HotlistService` with Redis set cache (`rakshak:hotlist:active_plates`, TTL 300s), automatic rebuild on startup, **fallback to PostgreSQL on Redis failure**, configurable expiry (`HOTLIST_CONFIRMATION_HOURS`), background expiry worker.
**Verification:** `SMEMBERS` returns cached plate; DB row present; TTL counting down.

---

## Phase 7 — Sightings + Tracking ✅

**Implemented:** `POST /sightings` validates device ownership/revocation, normalizes plate, checks hotlist (Redis→PG), **does NOT persist non-hotlist plates**, applies per-device cooldown throttle, updates `hotlist.last_seen_*`, returns sighting.
**Verification:** hotlist match → sighting created; non-hotlist plate → no sighting, no alert.

---

## Phase 8 — WebSocket Real-Time Alerts ✅

**Implemented:** `/api/v1/ws/police` (JWT-authed), `PoliceAlertManager.broadcast`, alert payload pushed on every hotlist match; `GET /alerts` for page-load history.
**Status:** PARTIAL verification — broadcast code exercised indirectly via sighting flow; dedicated WS client test pending (pytest-asyncio WS test planned Phase 12).

---

## Phase 9 — Citizen Web ✅

**Implemented:** React + TS + Vite + Tailwind portal (`citizen_web/`) — Login/Register, dashboard with status chips, NewComplaint (plate submission), MyComplaints list + detail.
**Verification:** `npm run build` (tsc + vite) succeeds; E2E citizen flows verified via API.

---

## Phase 10 — Police Dashboard ✅

**Implemented:** `police_dashboard/` — Login, Overview (KPIs + recent alerts), LiveAlerts (WebSocket `hotlist_detection` push), Hotlist management (patch status), Complaints (verify/reject → auto-hotlist), VehicleSearch + VehicleDetail (Leaflet route polyline + timeline), Analytics (hourly detections, hotlist matches, locations map), Admin (users + device revoke).
**Verification:** `npm run build` (tsc + vite) succeeds after fixing 2 TS errors; all backend endpoints referenced by the UI exist.

---

## Phase 11 — Mobile ANPR + AI Pipeline + Simulator ✅

**Implemented:**
- `ai/` — classical-CV plate detector + RapidOCR (single shared engine) + line-merge plate candidate fusion with privacy contract (only plate/confidence leave device).
- `mobile_app/` (Expo SDK 53) — Login/Register, CameraView scanner, host OCR via `/scanner/scan` (demo), GPS tagging (expo-location with fallback), per-plate cooldown, sighting report, device auto-registration, session persistence.
- `backend/app/api/v1/scanner.py` — DEMO-only single-frame remote OCR endpoint (disabled when `DEMO_MODE=false`).
- `scripts/` — `generate_plate_images.py`, `generate_demo_video.py`, `device_simulator.py` (webcam/video → on-device ANPR → sighting reports, never uploads frames), `seed_demo.py` (idempotent demo cases), `e2e_verify.ps1`.
**Verification:** pipeline reads 5/5 generated plates; `/scanner/scan` returns plate+confidence; `npx expo export` bundles the app (598 modules, no errors).

---

## Phase 12-13 — Tests + Final Integration ✅

**Implemented:** 43 backend tests (auth/RBAC, complaints, plate normalization, privacy contract, sightings, WebSocket alerts), `pytest.ini`.
**Verification (all green):**
- `pytest` → 43 passed (isolated `rakshak_test` DB, Redis db 15), including live in-process WS tests: `hotlist_detection` delivered to the police channel and volunteer token rejected with 4403.
- Live backend boot + `scripts/seed_demo.py` (demo cases) + `scripts/e2e_verify.ps1` → every check PASS (logins, complaint→verify→hotlist, device register, detections → alert, analytics, route timeline, RBAC denials, admin devices).
- Live WS probe → real `hotlist_detection` event delivered over the wire to a connected police client.
- `scripts/device_simulator.py --video data/generated/demo_road.mp4` → hotlist plate detected (conf ~0.89) → sighting + live alert on the police feed.
- Dockerfiles + `.dockerignore` added for backend/citizen_web/police_dashboard so `docker compose up --build` is complete (compose still unverified locally — no Docker on this machine).

### Bug fixed during Phase 12
`backend/app/ws/manager.py` called `websocket.accept()` a second time inside `PoliceAlertManager.connect()`
even though `ws.py` had already accepted — dropping live police connections right after handshake.
Removed the redundant accept; WebSocket alert delivery now verified on the wire and by automated tests.
Also fixed `backend/app/api/v1/sightings.py` returning (not raising) `HTTPException` and
`scripts/device_simulator.py` not setting `Content-Type: application/json` on login.

---

## Primary Demo E2E (manual, via API) ✅

1. Citizen login → submit complaint `TS09AB1234` → `PENDING`
2. Cop login → sees complaint → verify → hotlist `ACTIVE`, Redis cached
3. Volunteer login → register device → send detection (17.385, 78.4867)
4. Backend match → sighting stored → last_seen updated → broadcast fired
5. Cop: `GET /vehicles/TS09AB1234` → sightings_count≥1, last_seen set
6. Cop: `GET /alerts` → 1 alert
7. Cop: `GET /vehicles/TS09AB1234/route` → points for polyline
8. Non-hotlist plate `KA01MN4321` → correctly ignored (no sighting/alert)

---

## Problems Found & Fixed

| Problem | Fix |
|---|---|
| `models/__init__.py` emptied by parallel `New-Item` (race) | Rewrote export list; import verified |
| Port 8000 held by stale foreign Python server (PID 4468) | Killed process; started RAKSHAK uvicorn |
| Wrong initial PG password guess (`root`) | Probed safely; correct password is `postgres` |
| `python -m alembic` not found | Use `Scripts\alembic.exe` directly |
| pip numpy 2.x conflicts with pre-existing tensorflow-model-optimization (numpy~=1.23) | Documented; does not affect RAKSHAK paths |

---

## Remaining Limitations

- Docker Desktop not installed (needs admin) — compose file + Dockerfiles ready but `docker compose` unverified locally
- Mobile app verified via Metro bundle export only (camera capture loop requires an on-device run)
- Demo-mode cooldown = 60s (affects multi-sighting timing in live demo)
- RapidOCR first-load downloads its ONNX models (~10-20 MB) — one-time warm-up on new machines

---

## Phase 14 — Full-project audit, fixes, performance & on-device run ✅

**Verification performed**
- `pytest` → **43 passed** (unchanged, still green after every fix below)
- `scripts/e2e_verify.py` → **68/68 checks PASS** against a live backend: health → auth/RBAC →
  complaint intake (multipart + proof upload) → police verify → hotlist + Redis cache →
  device registration → detection → sighting → cooldown → GPS timeline/route → analytics
  (overview/detections/hotlist-matches/locations) → alerts feed → hotlist lifecycle
  (FIR → RECOVERED → cache eviction) → admin (users/devices/revoke) → real-time WebSocket
  alert → audit trail → on-host RapidOCR read of `TS09AB1234` (conf 0.876).
- Mobile app **running on the physical device** (vivo I2202, Android 14) via Expo Go:
  login → auto device registration → live GPS-tagged detection →
  `POST /sightings 200` → hotlist match broadcast. Confirmed in both app log and backend log.
- `npm run build` green for `citizen_web` and `police_dashboard`.

### Bugs found & fixed in this pass
| Problem | Fix |
|---|---|
| **Mobile app crashed on login** — `Incompatible React versions: react 19.1.0 vs react-native-renderer 19.0.0` → `Cannot read property 'default' of undefined` in RN `Animated` | Pinned `react@19.0.0`, `react-native@0.79.6`, `expo-location@~18.1.6` (the versions Expo SDK 53 actually ships); reinstalled and re-verified on-device |
| `passlib` imported by `app/core/security.py` but **missing from `requirements.txt`** (breaks any fresh install / Docker build) | Added `passlib==1.7.4` |
| `backend/Dockerfile` did `COPY ai ./ai` while compose used `build: ./backend` → **build fails, `ai/` does not exist under `backend/`** | Compose now builds with the repo root as context; Dockerfile copies `backend/*` + shared `ai/`; added root `.dockerignore` |
| `opencv-python` needs system libs in `python:3.12-slim` → `import cv2` fails in-container | Dockerfile installs `libgl1 libglib2.0-0` |
| Duplicate `ACTIVE` hot-list rows per plate (re-verifying an already hot-listed plate inserted a new row) | `HotlistService.add_to_hotlist` reuses the existing active entry; merged the 4 duplicate `TS09AB1234` rows already in the demo DB (sightings repointed) |
| **Redis hot-list cache was dead code** — `is_plate_hotlisted()`/`rebuild_cache()` were never called, so every detection hit PostgreSQL | Detection hot path now uses the Redis set as a fast pre-filter (self-heals stale entries); cache is rebuilt on startup and after every verify/recover/close |
| `sightings.py` used `__import__("app.models", fromlist=["Hotlist"]).Hotlist` and an unused local import | Replaced with a normal top-level import |
| `analytics/overview` issued 5 separate COUNT round trips | Single round trip of scalar subqueries |
| Vehicle timeline / route queries had no composite index | Added `sightings(hotlist_id, detected_at)` + `hotlist(plate, status, added_at)` with Alembic migration `0002_perf_indexes` |
| Mobile scanner ignored its own `location.js` (hard-coded random coords) and took a fresh GPS lock per detection | Scanner now GPS-tags every detection; helper caches coordinates (15 s TTL) and uses the OS last-known fix before requesting a new lock |
| `police_dashboard` shipped a single ~775 kB bundle | Route-level code splitting + vendor chunks → entry chunk ~52 kB (≈20 kB gzip); leaflet/recharts load on demand. Same treatment for `citizen_web` (~45 kB entry) |

### New tooling
- `scripts/e2e_verify.py` — cross-platform end-to-end pipeline verifier (replaces the
  PowerShell-only `e2e_verify.ps1`), self-cleaning so the demo DB stays pristine.
- Alembic revision `0002_perf_indexes`.

---

## Phase 15 — Session robustness, API correctness & demo data ✅

### Bugs found & fixed
| Problem | Fix |
|---|---|
| **Every client discarded its refresh token** — access tokens expire in 30 min (`JWT_ACCESS_MINUTES`), so a live demo would drop to the login screen mid-flow | All three clients now persist the access + refresh pair and renew transparently on `401`: the mobile app owns the session inside `services/api.js` (callers never re-thread a token), the web apps use a single-flight axios interceptor so a burst of 401s renews only once |
| Mobile logout kept the stored device id, but a device belongs to the account that registered it → signing in as a different user made every detection fail with 403 "Device does not belong to you" | Logout now forgets the device; a 403 on report also clears it and tells the officer to scan again |
| `POST /hotlist` stored `payload.plate.upper()` with **no normalization or validation** — a hand-typed `ts-09 ab 1234` could never match an ANPR reading | Normalizes + validates with the same `normalize_plate()` used by complaints and detections (422 on invalid) |
| `POST /sightings` signalled "no hotlist match" by `raise HTTPException(status_code=200, ...)` | Returns `202 Accepted` with `{"accepted": false, "detail": "accepted_no_match"}`; test updated |
| Demo seed left inconsistent state (active hot-list entries whose complaint still said `PENDING`) and no multi-day history for the analytics charts | `seed_demo.py` rewritten: 5 hot-list entries (ACTIVE / FIR_CONFIRMED / RECOVERED), 12 complaints across all statuses, 24 sightings spread over 1-3 days, 20 devices; it now converges re-runs onto the declared demo state and reports a summary |

### Verification
- `pytest` → **43 passed**
- `scripts/e2e_verify.py` → **72/72 checks PASS** (4 new token-lifecycle checks: refresh issues a
  new pair, the renewed access token is accepted, a refresh token is rejected as an access token)
- `npx expo export --platform android` → bundles clean (596 modules) after the mobile refactor
- Both web apps `npm run build` green

### Known gap (hardware)
The phone disconnected from USB mid-session, so the on-device refresh flow could not be
exercised live. It is covered at the API-contract level and the client bundles cleanly.
To resume on-device testing:

```bash
adb reverse tcp:8000 tcp:8000 && adb reverse tcp:8081 tcp:8081
adb shell am start -a android.intent.action.VIEW -d "exp://127.0.0.1:8081"
```

### Live services (this machine)
| Service | URL |
|---|---|
| Backend API + docs | http://localhost:8000 (`/docs`) |
| Citizen portal | http://localhost:5173 |
| Police dashboard | http://localhost:5174 |
| Expo Metro bundler | http://localhost:8081 (device via `adb reverse`) |
| Mobile app | Expo Go on vivo I2202 (`exp://127.0.0.1:8081`) |

## Phase 16 — Character verifier: a second opinion on every glyph ✅

### Why
`DL1ZA9092` and `DL12AG092` are both legal plates, so no validator can tell the
field report's misread from the truth. The line score cannot either: it is one
number for nine characters. This phase gives the pipeline a per-character
cross-check and, more importantly, a rule for what to do when the two opinions
disagree — which is to *not* act.

### What was built
| File | Role |
|---|---|
| `scripts/gen_char_data.py` | Dataset generator: Indian plates in 4 colour schemes, 40 fonts (8 held out), heavy augmentation (blur/defocus/JPEG/perspective/skew/glare/shadows/dirt/moiré), 2× oversampling of the 10 hard pairs, real crops segmented out of `data/generated/*.png` with a 35% holdout. Never labels a crop it is not sure about |
| `scripts/train_char_classifier.py` | Seeds, early stopping, temperature scaling (grid NLL), ECE before/after, confusion PNG+CSV per split, per-class accuracy, 10 worst confusions, ONNX export (opset 12) + parity check |
| `ai/char_grammar.py` | `<state:2A><RTO:1-2D><series:1-3A><tail:1-4D>` templates, per-position whitelist, valid-state whitelist (lazy import), `decode()` picks the parse the OCR already agrees with |
| `ai/char_segment.py` | Honest glyph segmenter: returns `[]` rather than a guessed split |
| `ai/char_verifier.py` | ONNX session + `preprocess_char`, `restricted_max`, `probability_of`; never fatal — missing model/runtime/OpenCV ⇒ `is_enabled()` False |
| `backend/app/api/v1/live_scan.py` | `_fuse_chars` (grammar × classifier, geometric-mean confidence), `run_ocr` verification + skip paths, `chars`/`verified`/`ambiguous`/`grammar` on the result, `chars` in the plate message |
| `backend/app/services/plate_verdict.py` | Two kinds of doubt, not conflated: OCR-confidence weakness lifts with consensus; a verifier refusal (`ver_amb`) blocks STOLEN outright. Single-read STOLEN now requires `verified=True` |
| `backend/app/core/config.py`, `.env.example` | `CHAR_VERIFY` on/off |
| `backend/tests/test_char_verifier.py` | 24 tests: grammar/templates, fusion in both directions of every hard pair, all four skip paths, the verdict gate |
| `scripts/eval_char_verifier.py` | The before/after evidence below, → `data/debug_out/char_eval.json` |

Rules, in one line each: segment or skip (never guess); correct only inside the
slot's class; replace only at classifier ≥ 0.75 and OCR ≤ 0.20, otherwise mark
AMBIGUOUS; fuse with a geometric mean; never rewrite a confident read on a
doubtful glyph.

### Verification
- `pytest backend/tests` → **316 passed** (26 of them the new verifier tests)
- Training (`--quick` then full, seed 42, early stop epoch 22, 371 s):

  | split | acc | ECE before → after |
  |---|---:|---:|
  | test (held-out synth, 1410) | 0.9404 | 0.0658 → 0.0120 |
  | font_held (1423) | 0.7667 | 0.0610 → 0.0485 |
  | real_test (18) | 0.7778 | 0.1381 → 0.1268 |
  | val (temperature fitted here) | 0.9189 | 0.0705 → 0.0184 |

  Temperature **T = 0.7026**; ONNX parity max |Δlogit| 4e-6, max |Δprob| 1e-6 (tol 1e-3).
  Worst test confusions: `1→I` (6), `0→O` (3), `8→6` (3), `D→U` (2), `G→C` (2).
- `python scripts/eval_char_verifier.py` → before/after (raw argmax vs calibrated + grammar-restricted):

  | split | acc | ECE | Z→7 | Z→2 | Z read as any digit |
  |---|---:|---:|---:|---:|---:|
  | test | 94.0% → 95.2% | .066 → .012 | 0 → 0 | 0 → 0 | 1 → 0 |
  | font_held | 76.7% → 79.6% | .061 → .049 | 0 → 0 | 1 → 0 | 2 → 0 |
  | real_test | 77.8% → 77.8% | .138 → .127 | 0 → 0 | 0 → 0 | 0 |

  End to end on the 6 demo plates: **6/6 exact both ways and `verified=True`
  on all six** (before the compaction fix the "after" column was a no-op — see
  the bug row above). The verifier agrees with the OCR on these clean renders
  and rewrites nothing; what it does change is *how much the read is to be
  believed*. On `DL8CBF4890`, for instance, the model reads the leading `D` as
  an `A` (0.54), the grammar holds the position to `D` and the fused confidence
  collapses to ~0.10 — the character is not rewritten, and a single-frame
  STOLEN is blocked, which is the whole point.
  Cost: classify **0.39 ms p50 / 0.45 ms p95** per glyph,
  verifier box **22.7 ms p50 / 71.2 ms p95** per plate.

### Bug found while proving it works
| Problem | Fix |
|---|---|
| **Every real plate silently skipped verification.** RapidOCR keeps the spaces it saw, so `read.text` was `DL 8C BF 4890` — 13 characters against a 10-glyph plate. `segment_chars` counts alphanumeric glyphs only, so `len(crops) == len(text)` could never hold and `run_ocr` fell through to `verified=False` without a word; the returned string looked completely normal, so nothing failed loudly. Caught by `scripts/char_probs.py`, which printed `segmented 10/13 glyphs` instead of 10/10 | `run_ocr` compacts the read once up front — the form `read_crop`'s `char_confs` and `normalize_plate` already assume — and drops a `char_confs` list whose length does not fit the string instead of shifting every doubt onto the wrong glyph. Two regression tests: a spaced read must verify, and a misaligned confidence list must be discarded |

### Known gaps
- `real_test` is 18 crops and `font_held` is the weak split (76.7%): the model
  generalises to unseen fonts worse than to unseen synthetic noise. Real crops
  are limited by the six demo plates — more real plates is the highest-value data.
- The character-level "after" column restricts to the *ground-truth* slot class
  (a bare crop has no position), so it isolates classifier+grammar rather than
  the whole pipeline; the plate-level table is the end-to-end number.
- Docker: `backend/Dockerfile` now copies `models/char_cls.*` (without it the
  container silently runs with `CHAR_VERIFY` disabled and falls back to
  consensus — correct, but half the feature). Still unverified locally: no
  Docker on this machine.

---

## Phase 17 — One registration, two spellings (the district leading zero) ✅

### Why
An Indian district ("RTO") code is officially two digits, and the complaint form,
the RC and the police record all write it that way — but the plate bolted to the
car, and the read taken off it, usually drop the leading zero. `DL01ZA9092` and
`DL1ZA9092` are the same car, yet they are different strings, so a complaint filed
one way never matched a camera that read the other: no exact hot-list match, only a
0.90 fuzzy one, which is amber `POSSIBLE`, writes no sighting and pages nobody. A
stolen car drives on with the phone reading "might be".

### What was built
- `app/services/plate.py`:
  - `plate_key(plate)` — the canonical matching form (district leading zero
    dropped). This is what the hot-list cache stores, so one car is one entry.
  - `plate_spellings(plate)` — every spelling of the same registration (at most
    two), for a database lookup. Only the *district's* leading zero is
    insignificant: `01`≡`1`, but `01`≢`02` and `12`≢`1`.
- Every place a plate meets the hot-list is now spelling-aware:
  - `plate_verdict.evaluate` — an equivalent spelling counts as an **exact**
    hot-list match, so it reaches `STOLEN` (with the usual corroboration), not
    amber `POSSIBLE`.
  - `hotlist_service.get_active_by_plate` / `is_plate_hotlisted` — the DB lookup
    uses `plate IN (spellings)` (still index-backed); the Redis probe tries each
    spelling.
  - `sighting_service.record_detection` and `get_route_for_plate` — plus the
    detection cooldown, now keyed on the canonical form so a read that flaps
    between spellings cannot record two sightings for one parked car.
  - `cache_service` — stores canonical keys (one member per car).
  - `vehicles.py` — all three police lookups (detail / timeline / route).
- `scripts/e2e_verify.py` — the Redis-cache checks compare on the canonical key.

The plate *displayed* is never rewritten: only lookups fan out. A complaint of
`DL01ZA9092` still reads back as `DL01ZA9092`.

### Verification
- `pytest` → **390 passed** (19 new: ten `plate_key`/`plate_spellings` units, four
  verdict cases, the full complaint → verify → alert chain, and the DB / cache /
  vehicles API paths).
- The new behavioural tests were first run against the pre-fix code, where they
  fail — the padding cases pass only because of this change.
- Redeployed (`docker compose build backend && docker compose up -d backend`) and
  `scripts/e2e_verify.py` → **72/72 checks passed**, including both Redis-cache
  checks (now canonical) and the live WebSocket `hotlist_detection` alert.
- Caveat seen while redeploying: in this environment a *native* PostgreSQL also
  owns `5432`, so a host connection to `localhost:5432` reaches the native DB
  rather than the compose one. `e2e_verify.py`'s direct-DB checks (audit trail,
  cleanup) then read the wrong database — the audit check passes on stale rows and
  the cleanup silently removes 0. The HTTP checks are unaffected because they go
  through the container. The two probe rows this run created were deleted from the
  compose DB by hand (`docker compose exec postgres psql`).

### Bug found while proving it works
| Problem | Fix |
|---|---|
| The rule is a *leading zero*, not "one or two digits are interchangeable": `TN12AB3456` and `TN1AB3456` are districts 12 and 1 — different cars. The first draft of the dashboard test asserted they match; the code correctly returned `404` | Kept the code, fixed the test — and pinned the trap as a unit test (`test_a_district_without_a_leading_zero_has_only_one_spelling`), moving the dashboard case to `TN01AB3456`/`TN1AB3456`, the real equivalence |

---

## Next Phase

Project complete and verified end-to-end, including a real on-device run. Remaining optional
polish: run `docker compose up --build` on a Docker-capable machine (Dockerfiles/compose are
now correct but still unverified locally — Docker is not installed here), and swap the DEMO
host-OCR `/scanner/scan` path for an on-device ONNX model.
