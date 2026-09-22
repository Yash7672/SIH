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

## Quick Start

```bash
# 1. Environment
cp .env.example .env

# 2. Backend (requires PostgreSQL + Redis running locally or via docker-compose)
cd backend
pip install -r requirements.txt
alembic upgrade head
python -m app.services.seed   # optional demo data
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
npx expo start
# press 'a' for Android

# 6. Device simulator (feeds detection events to backend)
python scripts/device_simulator.py
```

## Docker Compose

```bash
docker compose up --build
```

Services: `postgres`, `redis`, `backend`, `citizen_web`, `police_dashboard`.

## Demo Credentials

Seeded on first backend start (see `docs/DEMO.md`):

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

```bash
cd backend
pytest
```

## License

MIT (hackathon demo — not production software).
