FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# libgl1/libglib2.0-0 are required by opencv-python (imported by the ANPR pipeline).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Build context is the repository ROOT (see docker-compose.yml) so the shared
# `ai/` ANPR package and the trained model land next to `app/`.
# `ai.detector.resolve_model_path()` looks for <root>/models/<MODEL_NAME>, which
# is exactly where `backend/models` is copied to below.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/alembic.ini ./alembic.ini
COPY backend/alembic ./alembic
COPY backend/app ./app
COPY ai ./ai
# Trained detector weights (~5 MB) baked in; keep in sync with AI_MODEL_PATH.
COPY backend/models ./models
# Demo data seeding (scripts/seed_demo.py) is run via `python -m scripts.seed_demo`.
COPY scripts ./scripts

ENV STORAGE_LOCAL_PATH=/app/data/uploads
RUN mkdir -p /app/data/uploads

EXPOSE 8000

# Migrations run before the API comes up; demo users are seeded idempotently by
# the FastAPI lifespan (see app/main.py).
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]