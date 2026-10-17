# RAKSHAK — Privacy-First Crowdsourced ANPR Vehicle Tracking System

RAKSHAK is a hackathon demo application for crowdsourced vehicle tracking using
Automatic Number Plate Recognition (ANPR), with privacy-by-design: only plate
number, GPS coordinates, timestamp, and confidence are transmitted — never raw
video streams.

## Architecture

- **backend/** — FastAPI + SQLAlchemy + Alembic + PostgreSQL + Redis (cache) + WebSocket
- **citizen_web/** — React + TypeScript + Vite + TailwindCSS (citizen portal)
- **police_dashboard/** — React + TypeScript + Vite + TailwindCSS + Leaflet (police console)
- **mobile_app/** — React Native + Expo + TypeScript (volunteer scanner)
- **ai/** — Plate detection / OCR abstractions (YOLO + OCR adapters)
- **scripts/** — Demo data generation, device simulator, seeding

See `docs/` for full documentation.

## Quick Start (`start.bat` — one command)

```powershell
.\start.bat          # Windows: Postgres + Redis + backend + web apps + Expo
```
```bash
./scripts/start.sh   # Linux/macOS (same stack)
```

Stop with `.\stop.bat` / `./scripts/stop.sh`; Docker is auto-detected (`-Mode docker` to force it).

### Stream mode (fixed camera / phone as IP camera) — 3-line quick start

```powershell
# 1. Put your camera URL in config/stream_sources.json (kind: mjpeg | rtsp | usb | video)
python scripts/stream_camera.py --list-sources
# 2. Phone as IP camera: run an MJPEG app (e.g. "IP Webcam") on the phone, note its http://<phone-ip>:<port>/video URL, add it to config/stream_sources.json as phone_ip_camera
# 3. Stream it into WS /ws/scan (prints SOURCE CONFIRMED, then WS /ws/scan CONFIRMED, then STOLEN/POSSIBLE/CLEAR verdicts):
python scripts/stream_camera.py --source phone_ip_camera --username volunteer@example.com --password "Volunteer@123"
```

`stream_camera.py` latest-frame-wins (drops stale frames while the detector is busy), reconnects a dropped WS with backoff, and the MJPEG/RTSP reader auto-reconnects a camera that drops. Frames are memory-only: nothing here is persisted.

## Manual setup

```bash
# 1. Environment
cp .env.example .env

# 2. Backend (requires PostgreSQL + Redis running locally or via docker-compose)
cd backend
pip install -r requirements.txt
alembic upgrade head
# demo users are auto-seeded on backend startup (see app/main.py lifespan)
uvicorn app.main:app --reload --port 8000

# 3. Citizen Web
cd citizen_web
npm install
npm run dev        # http://localhost:5173

# 4. Police Dashboard
cd police_dashboard
npm install
npm run dev        # http://localhost:5174

# 5. Mobile (on connected Android device / emulator)
cd mobile_app
npm install
# forward host ports over USB so the phone can reach the backend and Metro
adb reverse tcp:8000 tcp:8000
adb reverse tcp:8081 tcp:8081
npx expo start --host localhost
# then open exp://127.0.0.1:8081 on the device (Expo Go must be installed)
# or press 'a' for Android

# 6. Device simulator (feeds detection events to backend)
python scripts/device_simulator.py
```

> Mobile note: the app must run against the same Expo SDK runtime Expo Go ships.
> `npm install` pins `expo@^58` / `react@19.3.0` / `react-native@0.88.0-rc.3` (see
> `mobile_app/package.json`, which is the source of truth); mismatched React
> versions crash inside React Native's Animated implementation. The React Native
> pin is a release candidate on purpose - it is the version Expo SDK 58 expects.

## Docker Compose

```bash
docker compose up --build
```

Services: `postgres`, `redis`, `backend`, `citizen_web`, `police_dashboard`.

## Demo Credentials

Seeded on first backend start (see `docs/PROGRESS.md`):

| Role | Email | Password |
|---|---|---|
| Citizen | citizen@example.com | Citizen@123 |
| Volunteer | volunteer@example.com | Volunteer@123 |
| Cop | cop@example.com | Police@123 |
| Admin | admin@example.com | Admin@123 |

> These are development-only credentials. Never use in production.

## Environment Variables

See `.env.example`. Never commit real secrets.

## Testing

Backend unit/integration suite (isolated `rakshak_test` DB + Redis db 15):

```bash
cd backend
pytest
```

End-to-end pipeline verification against a **running** backend (auth/RBAC → complaint →
police verify → hotlist → Redis cache → device → detection → sighting → GPS route →
web socket alert → analytics → admin → audit log → on-host ANPR OCR):

```bash
python scripts/e2e_verify.py                 # 72 checks, self-cleaning
python scripts/e2e_verify.py --keep-data     # keep the rows it creates
```

Demo data (idempotent — safe to re-run; fills hot-list entries, complaints in every
status, and multi-day sighting history for the charts):

```bash
python scripts/seed_demo.py
python scripts/device_simulator.py --video data/generated/demo_road.mp4
```

## Sessions

Access tokens live for `JWT_ACCESS_MINUTES` (30) and refresh tokens for `JWT_REFRESH_DAYS` (7).
Every client stores both and renews automatically on `401` — mobile inside `services/api.js`,
web via a single-flight axios interceptor — so a long demo never bounces to the login screen.

## Performance Notes

- Detection hot path answers the common "not on the hot list" case from the Redis set,
  falling back to PostgreSQL (source of truth) only when the cache is cold.
- Dashboard KPIs are computed as scalar subqueries in a single round trip.
- Composite indexes (`hotlist(plate,status,added_at)`, `sightings(hotlist_id,detected_at)`)
  back the hot-list lookup and vehicle timeline/route queries.
- Both web apps lazy-load routes and split vendor chunks, so the dashboard shell is
  ~52 kB (≈20 kB gzipped) instead of a single ~775 kB bundle.

## License

MIT (hackathon demo — not production software).
