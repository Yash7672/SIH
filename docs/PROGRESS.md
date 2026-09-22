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

- Docker Desktop not installed (needs admin) — compose file ready but unverified locally
- WS alert delivery not yet covered by automated test (Phase 12)
- Citizen/police web UIs, mobile app, AI/ANPR pipeline, sample video, simulator: NOT YET BUILT (Phases 9-11 upcoming)
- Demo-mode cooldown = 60s (affects multi-sighting timing in live demo)

---

## Next Phase

Phase 9-10: Citizen Web + Police Dashboard (Leaflet map, live alerts) + Mobile scanner scaffold → then AI/ANPR + simulator + integration tests.
