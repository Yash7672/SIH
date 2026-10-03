# Project Map
reviewed at commit 18479edfdc66a841de45259b531166082e144ab8

## Architecture Overview
The system is a "Privacy-First Crowdsourced ANPR Vehicle Tracking System" named RAKSHAK.
- **Backend**: FastAPI (Python), PostgreSQL (Relational DB), Redis (Caching/Messaging).
- **Citizen Web**: Vite + React, for citizens to file complaints.
- **Police Dashboard**: Vite + React, for police officers to view alerts, hotlists, and analytics.
- **Mobile App**: Expo (React Native), for volunteers/police to scan vehicles and number plates.
- **AI Engine**: YOLOv8 (vehicle/plate detection) and RapidOCR (plate text reading). Runs on the backend.

## What start.bat does
1. Invokes `scripts/start.ps1`.
2. Checks prerequisites (Node, Python, Docker, PostgreSQL, Redis).
3. Detects LAN IP to bind services and configure `.env` URLs.
4. Generates `.env` and `mobile_app/.env` (if missing).
5. Installs dependencies (`npm install`, `pip install`).
6. Runs Alembic migrations.
7. Starts Backend (`uvicorn`), Citizen Web (`vite`), Police Dashboard (`vite`), and Mobile App (`expo start`).

## Folder Purpose & Key Files
- `backend/`: FastAPI application (`app/main.py`), database models (`app/models/models.py`), endpoints (`app/api/`), websocket (`app/ws/`).
- `citizen_web/`: Citizen-facing React SPA.
- `police_dashboard/`: Police-facing React SPA.
- `mobile_app/`: React Native Expo app (`App.js`, `ScannerScreen.js`).
- `scripts/`: Utilities (`seed_demo.py`, `start.ps1`, `health_check_models.py`).
- `ai/`: ML models wrappers.

## API Route Table
- `/api/v1/health` (GET)
- `/api/v1/auth/login`, `/register`, `/me`
- `/api/v1/complaints` (POST/GET - Roles: Citizen, Cop, Admin)
- `/api/v1/hotlist` (POST/GET - Roles: Cop, Admin)
- `/api/v1/sightings` (POST/GET - Roles: Volunteer, Cop)
- `/api/v1/vehicles` (GET - search)
- `/api/v1/devices` (GET/POST)
- `/api/v1/analytics` (GET)
- `/api/v1/scanner/scan` (POST - Single image upload)

## WebSocket Endpoints
- `ws://.../api/v1/ws/police` - Dashboard real-time alerts. (Auth via token query param or initial auth message).

## Database & Alembic
- **Tables**: `users`, `devices`, `complaints`, `hotlist`, `sightings`, `audit_logs`.
- **Alembic**: `0001_initial` -> `0002_perf_indexes (head)`.

## Environment Variables
- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `DATABASE_URL`
- `REDIS_URL`
- `JWT_SECRET`, `JWT_EXPIRE_MINUTES`
- `DEMO_MODE`, `CORS_ALLOW_PRIVATE_NETWORK`

## Resolving API URL
- Clients read from `VITE_API_URL` (Web) or `EXPO_PUBLIC_API_URL` (Mobile), mapped at startup by `start.ps1` to the PC's LAN IP.

## Tests
- Backend tests in `backend/tests/`: `test_auth.py`, `test_complaints.py`, `test_health.py`, `test_plate.py`, `test_privacy.py`, `test_sightings.py`.

## Conventions
- Uses UUIDs for IDs.
- Time in UTC.
- Base64 image payload (to circumvent multiparts overhead on native).

## Found but not working or missing
- **Maps**: Missing (`traffic_cells` does not exist).
- **Event Bus**: Missing/empty (`app/services/bus/` is empty).
- **SDK 58**: Exists (Mobile is on Expo 58.0.2).
- **Base64 Scanner Upload**: Exists (`frameToBase64` in `mobile_app/src/services/api.js`).
- **State-code whitelist**: Exists (`VALID_STATE_CODES` in `plate.py`).
- **WS /ws/scan**: Missing (only POST `/scanner/scan` exists).
