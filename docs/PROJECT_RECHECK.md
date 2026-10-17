# RAKSHAK — Full Project Re-check & Validation Report

Date: 2026-10-17 · Commit: `dbfde70` (+ this session's uncommitted fixes) · Branch: `audit-speed`

Scope: every validation the project ships with — backend unit/integration suite,
live end-to-end API suite, model health check, plate-accuracy eval, both web
builds, the mobile test suite, and static analysis — plus a review of the
deployment environment the app is actually running in.

---

## 1. Executive summary

The application is in good shape and everything that can be validated on this
machine passes or passes-with-known-caveats:

| Validation | Result |
|---|---|
| Backend pytest (unit + integration) | **PASS** — 371 passed |
| Live e2e API suite (`e2e_verify.py`) | **70/72** (2 = environment artifact, see §5) |
| Model health check (`health_check_models.py`) | **PASS** (2 WARN, both known) |
| Plate-accuracy eval (`eval_plates.py`) | **PASS** — 6/6 exact, 100% chars |
| citizen_web `tsc` lint + build | **PASS** — 0 errors |
| police_dashboard `tsc` lint + build | **PASS** — 0 errors |
| mobile_app `node --test` | **PASS** — 59/59 |
| pyflakes (whole Python tree) | `ai/` clean; backend has pre-existing unused-import noise |
| compileall | **PASS** |

Three defects were found and **fixed** in this session (§4). The most important
one meant the two new plate models would have been **silently dead in the Docker
deployment**.

The biggest risks are **operational, not code** (§3, §5):

1. the running backend container is **16 h old and predates the latest commit** —
   it does not contain the new plate stack;
2. the machine is running **two parallel stacks** (a native PostgreSQL+Redis and
   the Docker one) that **shadow each other on the same ports**, so host tooling
   and the containerized backend talk to *different databases*.

---

## 2. What was verified (evidence)

Commands were run from the repo root with `.venv\Scripts\python.exe` unless noted.

### Backend unit/integration suite
```
cd backend && pytest -q -p no:randomly
-> 371 passed, 1 warning
```
21 test modules under `backend/tests/`, covering auth/RBAC, complaints, hotlist,
sightings, geo/heat, analytics, traffic grid, live-scan WS, the stolen-vehicle
chain, and the two new plate modules.

### Live end-to-end API suite (against the running backend on :8000)
```
python scripts/e2e_verify.py
-> RESULT: 70/72 checks passed
   failed: "hotlist plate cached in redis", "hotlist cache has a TTL"
```
Covers auth + RBAC, complaint → verify → hotlist, device registration,
detection → sighting, GPS route/timeline, analytics, alerts, admin, audit log,
the real `/scanner/scan` OCR path, and the police WebSocket alert.

The two failures are **not code defects** — see §5/§6.

### Model health check
```
python scripts/health_check_models.py
-> RESULT: PASS - all critical checks succeeded
   WARN 7: full pipeline detect_and_read  accuracy 6/6, avg 4.80s (slow)
   WARN 11: mobile-style degraded image   5/6 = 83%
```

### Plate-accuracy eval
```
python scripts/eval_plates.py
-> legacy detector 6/6 exact, char 100%
-> auto (new ONNX detector) 6/6 exact, char 100%, reads identical
```

### Frontends
```
cd citizen_web      && npm run lint && npm run build   -> tsc ok, vite ok (13.8 s)
cd police_dashboard && npm run lint && npm run build   -> tsc ok, vite ok (26.9 s)
```

### Mobile
```
cd mobile_app && node --test                            -> 59 pass / 0 fail
```

### Static analysis
```
python -m compileall -q ai backend scripts              -> exit 0
python -m pyflakes ai                                   -> clean
python -m pyflakes backend scripts                      -> pre-existing noise (see §5)
```

---

## 3. Environment (important — this changed since the last audit)

`docs/ENVIRONMENT_AUDIT.md` (2026-09-22) records **Docker not installed**. That is
no longer true: Docker Desktop was installed on 2026-10-16 and the whole RAKSHAK
stack is now running under `docker compose`, ~16 h uptime:

```
rakshak-backend-1          Up 16 hours (healthy)   0.0.0.0:8000->8000
rakshak-postgres-1         Up 16 hours (healthy)   0.0.0.0:5432->5432
rakshak-redis-1            Up 16 hours (healthy)   0.0.0.0:6379->6379
rakshak-citizen_web-1      Up 16 hours             0.0.0.0:5173->5173
rakshak-police_dashboard-1 Up 16 hours             0.0.0.0:5174->5174
```

**But the native stack from the earlier audit is still running too.** The host
therefore has two independent copies of the datastores on the same conventional
ports, and the *native* ones win on the host:

| Service | Host (`localhost:…`) | Docker (what the backend uses) |
|---|---|---|
| PostgreSQL 5432 | **native** Windows PG 17.11 (msvc) — `users=6, hotlist=21` | PG 17.11 alpine — `users=8, hotlist=5` |
| Redis 6379 | **native** Redis **3.0.504** — empty | Redis **7.4.11** — holds the live hotlist set |
| Backend 8000 | (docker proxy) | the Docker backend |

Consequences:

- `.env` has `DATABASE_URL=...@localhost:5432/rakshak` and
  `REDIS_URL=redis://localhost:6379/0`, which point at the **native** services.
  The containerized backend ignores them (compose overrides to the service names
  `postgres` / `redis`), so **host tooling and the backend use different data**.
- This is exactly why `e2e_verify.py`'s two Redis assertions fail: the script
  connects to `localhost:6379` (native, empty) while the backend writes the
  cache to the container's Redis (which *does* contain the plates — verified
  with `docker exec rakshak-redis-1 redis-cli smembers rakshak:hotlist:active_plates`
  → `TS78WX9194`, `TS63WX3788`, i.e. the e2e run's own plates). **The cache code
  is correct.**

Recommendation: stop the native `postgresql-x64-10` / `postgresql-x64-17` /
Redis services when running the Docker stack (or vice-versa), so `localhost`
means one thing.

---

## 4. Defects found and fixed this session

### D1 — New plate models were dead in the Docker image (high impact)
`ai/plate_detector.py` and `ai/plate_ocr.py` located their weights with a single
hard-coded path: `<root>/backend/models/<name>`. That is correct for a repo
checkout, but `backend/Dockerfile` copies `backend/models` to **`/app/models`**
while `ai/` lands at `/app/ai`, so inside the container the path resolved to
`/app/backend/models/...`, which **does not exist** (`docker exec … ls -d
/app/backend` → No such file or directory).

Effect: in the deployed container both new readers would have silently fallen
back (`is_enabled() == False`) — the ONNX detector would never run and the
fast-plate-ocr vote would never happen, with no error.

Fix: both modules now resolve the directory across both layouts, mirroring the
existing `ai.detector.resolve_model_path()`:

```python
root / "backend" / "models" / <name>   # repo checkout
root / "models" / <name>               # Docker image layout
```

Validated **inside the running container** (via `docker cp` of the new files):
```
model_dir = /app/models/open-image-models   enabled = True   present = True
detect(road_scene) -> box (435,515,555,551) conf 0.545   (matches local)
model_dir = /app/models/fast-plate-ocr      enabled = True
```
(The container was afterwards restored to its original image state.)

### D2 — `OcrHit` used in annotations without an import
`ai/plate_ocr.py` annotated four functions with `"OcrHit"` but only imported it
lazily inside one function body. Harmless at runtime (`from __future__ import
annotations` keeps them unevaluated) but a real latent defect for type checkers.
Fixed with a `TYPE_CHECKING` import.

### D3 — Duplicate `import os`
`ai/ocr_engine.py` ended up with a module-level `import os` and a second one in
the `except ImportError` fallback. Removed the redundant one.

Five regression tests were added for D1's resolution logic (2 in
`test_plate_ocr.py`, 3 in `test_plate_detector.py`). `ai/` is now pyflakes-clean.

---

## 5. Defects / risks found and **not** fixed (recommendations)

| # | Severity | Finding | Recommendation |
|---|---|---|---|
| R1 | **High** | The running `rakshak-backend-1` was built ~16 h ago and has **no `ai/plate_detector.py` / `ai/plate_ocr.py`** — the deployed app does **not** run the latest commit. | `docker compose up -d --build backend`. Note: `backend/requirements.txt` changed, so the pip layer reinstalls (slow, ~GB). |
| R2 | **High** | Two parallel stacks on the same ports (§3); host tools vs container backend use different DBs/Redis. | Stop the native services when using Docker (or the reverse); align `.env`. |
| R3 | Med | `e2e_verify.py` reads `settings.REDIS_URL` directly, so it cannot see the container's Redis and always reports the 2 cache checks as failed when the backend is containerized. | Have the script (or a new check) read the cache through the API, or document the Docker caveat. |
| R4 | Med | `plate_model_source/` is a **gitlink (mode 160000) with no `.gitmodules`** — a broken embedded repo. A fresh `git clone` yields an empty directory. | Remove it from the index (`.gitignore` it) or add a real `.gitmodules` entry. |
| R5 | Low | Root scratch files `commit-msg.txt` and `tree.txt` are **tracked**. | `git rm --cached` them and add to `.gitignore`. |
| R6 | Low | `mobile_app/package.json` pins `react-native@0.88.0-rc.3` (a **release candidate**) and `expo@^58`, but `README.md` says "Expo SDK 53 / `react-native@0.79.6`". Doc drift + an RC dependency in the demo path. | Update the README; consider a stable RN unless the RC is deliberate. |
| R7 | Low | police_dashboard `vendor-charts` chunk is **381 kB (gzip 105 kB)** — the largest asset by far. | Lazy-load `recharts` per route, or accept for a demo. |
| R8 | Med | Model health `WARN`: full `detect_and_read` avg **4.80 s/plate**, and a phone-degraded image read **5/6**. This is the known p95/throughput weak point. | The new ONNX detector addresses the *detection* leg; the remaining cost is OCR. Consider the existing `-Light` mode / fine-tuning on Indian plates. |
| R9 | Low | pyflakes noise in pre-existing backend code: unused imports in `alembic/env.py` and several `tests/*` (`tests.geo_helpers.grid`, `tests.test_live_scan_ws.register_device` redefinitions). | Cheap cleanup. |
| R10 | Low | If the app is ever run **natively**, it uses **Redis 3.0.504** (ancient) — fine for a plain cache, but do not rely on newer Redis features. | Keep using plain keys; or point native runs at the Docker Redis once R2 is resolved. |

---

## 6. Component status

| Component | Status | Notes |
|---|---|---|
| `backend/` (FastAPI) | ✅ tests pass, live API pass | 45 modules, 15 routers |
| `ai/` (ANPR) | ✅ + fixed | 9 modules; detector/OCR both layouts now resolvable |
| `citizen_web/` | ✅ lint + build | React 18.3.1 |
| `police_dashboard/` | ✅ lint + build | large charts chunk (R7) |
| `mobile_app/` | ✅ 59/59 tests | RC dependency + README drift (R6) |
| `scripts/` | ✅ | 34 scripts; e2e caveat (R3) |
| `docker-compose.yml` | ⚠️ runs stale code | rebuild needed (R1) |
| `docs/` | ✅ | 11 reports incl. this one |
| Git hygiene | ⚠️ | R4 (broken submodule), R5 (tracked scratch) |

---

## 7. Recommended order of work

1. **Deploy the new code**: `docker compose up -d --build backend` (R1), then re-run
   `python scripts/e2e_verify.py` and `python scripts/eval_plates.py`.
2. **Resolve the dual-stack port shadowing** (R2) so `localhost` is unambiguous.
3. **Fix the e2e Redis check** so the suite is green (R3).
4. **Repo hygiene**: broken submodule + tracked scratch files (R4, R5).
5. **Docs**: mobile README drift (R6).
6. **Optional**: OCR throughput / degraded-image accuracy (R8); pyflakes cleanup (R9).

---

## 8. Reproduce

```powershell
# from the repo root
.\.venv\Scripts\python.exe -m pytest -q -p no:randomly            # backend, needs backend/ as cwd
.\.venv\Scripts\python.exe scripts\e2e_verify.py                  # needs the backend running
.\.venv\Scripts\python.exe scripts\health_check_models.py
.\.venv\Scripts\python.exe scripts\eval_plates.py
.\.venv\Scripts\python.exe -m pyflakes ai backend scripts
cd citizen_web;      npm run lint; npm run build
cd police_dashboard; npm run lint; npm run build
cd mobile_app;       node --test
```

Caveat: the OCR/model checks are CPU-heavy. Run them **serially** — running the
e2e suite concurrently with the model health check caused the `/scanner/scan`
call to exceed its timeout and fail spuriously (it passed when run alone).
