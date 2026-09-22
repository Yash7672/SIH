# RAKSHAK — Environment Audit

Audit date: 2026-09-22
Auditor: opencode agent (Phase 0)

## Operating System

| Item | Value |
|---|---|
| OS | Microsoft Windows 11 Home Single Language |
| Version | 10.0.26200.0 (build 26200) |
| Shell | PowerShell 5.1 (PSVersion 5.1.26100.9444) |
| Admin rights | NO (non-elevated session) |

## Hardware

| Item | Value |
|---|---|
| CPU | 11th Gen Intel Core i5-1155G7 @ 2.50GHz (8 logical, 4 cores) |
| RAM | 7.8 GB |
| GPU | Intel Iris Xe Graphics (Driver 31.0.101.5333) — NO discrete/NVIDIA GPU |
| Architecture | x64 / AMD64 |
| DISK C: | 23.4 GB free |
| DISK E: | 87.5 GB free (project drive) |
| Total disk | 1x Phison ESO512GMLCT-E9C (477 GB) |

Note: no `nvidia-smi` (integrated GPU only). AI inference on CPU only.

## Software Detected / Versions

| Tool | Version | Status |
|---|---|---|
| Python | 3.12.0 (PATH), 3.12.5 available via pyenv, 3.13.7 in pyenv | Installed |
| pip | 25.3 | Installed |
| Node.js | v24.13.0 | Installed |
| npm | 11.6.2 | Installed |
| Git | 2.50.1.windows.1 | Installed |
| Git config | user.name=Yash7672, user.email=ryashwanth173@gmail.com | Configured |
| Java (JDK) | 25.0.1 LTS (JAVA_HOME=C:\Program Files\Java\jdk-25) | Installed |
| PostgreSQL | 17.11 (C:\Program Files\PostgreSQL\17) | Installed + RUNNING (port 5432) |
| Redis | 3.0.504 (Windows port, C:\Program Files\Redis) | Installed + RUNNING (port 6379) |
| Docker | NOT installed | MISSING |
| Docker Compose | NOT installed | MISSING |
| Android SDK (ANDROID_HOME) | %LOCALAPPDATA%\Android\Sdk | Installed |
| - platforms | android-31, 33, 34, 35, 36, 36.1, 37.0 | Installed |
| - build-tools | 30.0.3, 34.0.0, 35.0.0, 36.0.0 | Installed |
| - platform-tools / adb | 37.0.0 | Installed |
| - emulator | present | Installed |
| AVDs | Pixel_6 | Created |
| Android device (USB) | vivo I2202 — Android 14, SDK 34, 1080x2400, serial 10BC9F1PGB000D3 | CONNECTED |
| Expo CLI | 57.0.24 global package (CLI reports 57.0.26) | Installed |
| VS Code | 1.137.0 | Installed |
| winget | 1.29.380 | Installed |
| choco / scoop | not installed | — |
| WSL | installed (v2 default), NO distributions | Partial |
| Hyper-V / VirtualMachinePlatform | cannot verify without elevation | Unknown |

## Ports

| Port | Service | Listening |
|---|---|---|
| 5432 | PostgreSQL | YES (PID 16280) |
| 6379 | Redis | YES (PID 8276) |

## Redis Details

- `redis_version` = 3.0.504 (older Windows build)
- Auth: no `requirepass` configured (empty)
- `maxmemory` = 0 (unlimited)
- Ping test: PONG. Basic set/get verified.

## PostgreSQL Details

- Installed at `C:\Program Files\PostgreSQL\17`
- Service installed and RUNNING
- `pg_hba.conf` uses `scram-sha-256` auth for host connections — a password will be required
- Superuser `postgres` requires a password (not known during audit)

## Docker Gap

- `docker`, `docker compose` NOT available
- winget found Docker Desktop 4.91.0; download reached `%LOCALAPPDATA%\Temp\WinGet\Docker.DockerDesktop.4.91.0`
- Install requires ADMIN ELEVATION (blocked in this session — UAC prompt cannot be accepted non-interactively)
- WSL: present but no distro; enabling WSL feature / installing distro requires elevation

Fallback decision for Phase 1+: use NATIVE PostgreSQL + Redis (already running) for the primary
demo path. `docker-compose.yml` will still be provided for machines that have Docker, but local
verification will use the native services.

## Python Environment Notes

- `python` on PATH = Python 3.12.0 (C:\Users\yashw\AppData\Local\Programs\Python\Python312)
- `python3` (pyenv shim) = 3.12.5
- pyenv-win also provides 3.13.7
- No ANPR/AI packages installed globally yet (opencv/ultralytics/torch not present) — will be
  pinned in Phase 10 under `backend`/`ai` virtualenv

## Global npm packages

- freebuff@0.0.173
- opencode-ai@1.18.32
- expo@57.0.24

## Missing / Required Software

| Software | Status | Recommended |
|---|---|---|
| Docker + Compose | MISSING (needs admin) | Docker Desktop 4.91.0 — defer; native services used for demo |
| PostgreSQL client access password | REQUIRED | Provide superuser password or create `rakshak` role |
| opencv / ultralytics / torch | MISSING | Pin in Py side during Phase 2/10 builds |
| Expo Go on phone (optional) | Not confirmed | For USB run, use `expo run:android` dev build instead |

## Verification Commands (run and passed)

```
python --version              -> Python 3.12.0
node --version                -> v24.13.0
npm --version                 -> 11.6.2
git --version                 -> 2.50.1.windows.1
java -version                 -> 25.0.1 LTS
"C:\Program Files\PostgreSQL\17\bin\psql" --version -> psql (PostgreSQL) 17.11
"C:\Program Files\Redis\redis-server.exe" --version -> 3.0.504
"C:\Program Files\Redis\redis-cli.exe" ping      -> PONG
adb devices -l                -> vivo I2202 device attached (Android 14)
code --version                -> 1.137.0
expo --version (via node bin) -> 57.0.26
winget --version              -> 1.29.380
```

## Warnings

1. **Docker not installable non-interactively** (admin required). Native PostgreSQL + Redis used for
   the demo; Docker files provided but not locally verifiable unless run elevated.
2. **RAM 7.8 GB** — keep Docker out of the demo loop; run services natively to avoid memory pressure.
3. **No discrete GPU** — ANPR/OCR runs on CPU. Use lightweight ONNX/OpenCV-DNN models; design for
   ~1-4 fps plate scanning on-device.
4. **Redis 3.0.504 is old** — adequate for a cache; do not rely on Redis modules/streams features
   that may be missing. Use plain keys + optional Pub/Sub.
5. **PostgreSQL auth is scram-sha-256** — need credentials for the `rakshak` database. Will create
   dedicated role+db during Phase 2.
6. **Java 25** — very new; may be incompatible with some Android Gradle Plugin versions. Expo/RN
   builds typically want JDK 17-21. If Gradle fails, install JDK 21 (no admin needed via portable zip).