# RAKSHAK Test Commands

## Commands executed during validation

```
cd E:/PROGRAM_PROJECTS/Smart_india_hack/backend
pytest -q
```

```
cd E:/PROGRAM_PROJECTS/Smart_india_hack/backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```
Invoke-WebRequest -Uri http://127.0.0.1:8000/api/v1/healthz -Method Get
```

```
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/auth/login -Method Post -ContentType 'application/json' -Body '{"email":"citizen@example.com","password":"Citizen@123"}'
```

```
Set-Location "E:/PROGRAM_PROJECTS/Smart_india_hack/citizen_web"
npm install --silent
npm run build
```

```
Set-Location "E:/PROGRAM_PROJECTS/Smart_india_hack/police_dashboard"
npm install --silent
npm run build
```

```
adb reverse tcp:8000 tcp:8000
adb reverse tcp:8081 tcp:8081
```

```
Set-Location "E:/PROGRAM_PROJECTS/Smart_india_hack/mobile_app"
npm install --silent
npx expo start --localhost --android
```

```
Set-Location "E:/PROGRAM_PROJECTS/Smart_india_hack"
python scripts/validate_demo_flow.py
```

## Notes

- `docker` was not installed on this machine, so `docker compose up`/`down` could not be executed here.
- The backend live flow was verified using the real API endpoints, not only the mocked test suite.
