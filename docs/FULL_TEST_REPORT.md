# RAKSHAK Full Test Report

## Executive summary

The backend and core API flow are functioning in the current environment, and the police dashboard router issue was fixed. The application is not fully validated for a real-world mobile camera stream or Docker-based deployment here because the current machine does not have Docker installed and the test environment cannot capture a live device camera/GPS feed from the phone beyond app startup.

## Acceptance matrix

| Feature | Test | Result | Evidence | Issue | Fixed |
|--------|------|--------|----------|-------|-------|
| Backend unit/integration tests | `cd backend && pytest -q` | PASS | `43 passed in 7.15s` | None found | N/A |
| Backend startup | `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` | PASS | Uvicorn reported application startup complete and service stayed running on `127.0.0.1:8000` | None found | N/A |
| Health/auth API | live login and health endpoint calls | PASS | `GET /api/v1/healthz` and `POST /api/v1/auth/login` returned valid JSON responses | None found | N/A |
| Citizen complaint flow | complaint submission and cop verification via API | PASS | complaint was created, verified, and converted into a hotlist entry | None found | N/A |
| Hotlist matching detection | live sighting creation against hotlist plate | PASS | detection returned a sighting record with `hotlist_id` and matched `TS09AB1234` | None found | N/A |
| Citizen frontend build | `npm run build` in `citizen_web` | PASS | Vite build completed successfully | None found | N/A |
| Police dashboard build | `npm run build` in `police_dashboard` | PASS | Vite build completed successfully | None found | N/A |
| Police dashboard runtime routing | browser load to `http://localhost:5174/login` | PASS | app rendered the login screen after router fix | Missing `BrowserRouter` around `Routes` | Yes |
| Mobile app startup | Expo start on connected device | PASS | Metro started and Android device reported a running app session | Initial app startup was not fully validated beyond launch | N/A |
| Docker Compose validation | `docker compose up` | NOT TESTABLE | `docker` command is not installed in this environment | Missing Docker runtime | Not possible here |
| Real mobile camera/GPS privacy pipeline | physical camera and GPS validation on device | PARTIAL | Metro app launched; actual camera/GPS capture not executed from this machine | Requires device-side interaction and live sensor access | Pending manual verification |
| WebSocket police alert live delivery | browser/socket validation | SIMULATED | backend eventing logic exists and is wired, but a live browser socket session was not exercised in this environment | Live browser/client socket not connected here | Pending manual verification |
| AI/ANPR acccuracy | sample plate dataset and OCR checks | SIMULATED | AI modules exist and project includes image generation/test scripts, but no measured model accuracy run was performed here | No numerical accuracy benchmark available | Pending measured validation |

## Notes

- The police dashboard bug was caused by `Routes` being rendered without `BrowserRouter` in `police_dashboard/src/main.tsx`.
- The fix is in [police_dashboard/src/main.tsx](../police_dashboard/src/main.tsx).
- The live backend flow was validated with a real login, complaint creation, cop verification, device registration, and sighting generation to the hotlist.
- This environment cannot run Docker Compose because Docker is not installed, so Docker verification remains outside the current local test scope.
