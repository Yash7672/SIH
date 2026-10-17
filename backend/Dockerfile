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
# CPU-only PyTorch first. This demo has no GPU, but the default PyPI wheels pull
# in ~3 GB of nvidia-*/cudnn packages that are never used — that alone made the
# image ~10 GB and every rebuild re-downloaded the lot. `ultralytics` (below)
# then finds `torch` already satisfied and skips the CUDA set.
RUN pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu \
      torch torchvision
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/alembic.ini ./alembic.ini
COPY backend/alembic ./alembic
COPY backend/app ./app
COPY ai ./ai
# Trained detector weights (~5 MB) baked in; keep in sync with AI_MODEL_PATH.
COPY backend/models ./models
# Character-verifier ONNX + the temperature it was calibrated with (see
# requirements-train.txt / scripts/train_char_classifier.py; onnxruntime arrives
# with rapidocr-onnxruntime). Without these `ai.char_verifier.is_enabled()` is
# False inside the container and the verdict quietly falls back to demanding
# two agreeing reads - correct, but half the feature.
COPY models/char_cls.onnx ./models/char_cls.onnx
COPY models/char_cls.calib.json ./models/char_cls.calib.json
# Demo data seeding (scripts/seed_demo.py) is run via `python -m scripts.seed_demo`.
COPY scripts ./scripts

ENV STORAGE_LOCAL_PATH=/app/data/uploads
RUN mkdir -p /app/data/uploads

EXPOSE 8000

# Migrations run before the API comes up; demo users are seeded idempotently by
# the FastAPI lifespan (see app/main.py).
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]