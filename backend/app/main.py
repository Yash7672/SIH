import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.health import healthz
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import setup_logging, get_logger
from app.db.session import SessionLocal, get_db
from app.jobs.expiry import hotlist_expiry_worker
from app.services.hotlist_service import HotlistService
from app.services.seed import seed_demo_users

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("Starting RAKSHAK backend (DEMO_MODE=%s)", settings.DEMO_MODE)
    try:
        seed_demo_users()
        logger.info("Demo users ensured")
    except Exception as exc:
        logger.warning("Seed skipped/failed: %s", exc)
    try:
        db = SessionLocal()
        try:
            count = HotlistService(db).rebuild_cache()
            logger.info("Hotlist cache rebuilt (%d active plates)", count)
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Hotlist cache rebuild skipped: %s", exc)
    task = asyncio.create_task(hotlist_expiry_worker())
    yield
    task.cancel()


app = FastAPI(
    title="RAKSHAK API",
    description="Privacy-First Crowdsourced ANPR Vehicle Tracking System",
    version="1.0.0",
    lifespan=lifespan,
)

# Origins on loopback or a private LAN address: 10/8, 172.16/12, 192.168/16.
# Public addresses are deliberately absent - this widens the demo/dev surface,
# not the production one.
_PRIVATE_LAN_ORIGIN_RE = (
    r"^https?://(localhost|127\.0\.0\.1|\[::1\]|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d+)?$"
)

# Demo/dev only. With it off the explicit CORS_ORIGINS list is the whole policy.
_allow_private_lan = settings.DEMO_MODE and settings.CORS_ALLOW_PRIVATE_NETWORK

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_origin_regex=_PRIVATE_LAN_ORIGIN_RE if _allow_private_lan else None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if _allow_private_lan:
    logger.info(
        "CORS: explicit origins %s plus any loopback/private-LAN origin (demo mode)",
        settings.cors_origins_list,
    )


@app.get("/health")
async def health_root(db=Depends(get_db)):
    return healthz(db)


app.include_router(api_router, prefix="/api/v1")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Privacy guard: reject any request body that looks like a video/stream upload
@app.middleware("http")
async def privacy_guard(request: Request, call_next):
    content_type = (request.headers.get("content-type") or "").lower()
    if any(t in content_type for t in ("video/", "multipart/form-data")) and request.url.path not in (
        "/api/v1/complaints",
    ):
        # Complaints may upload proof; detection endpoints must never receive video.
        if request.url.path.startswith("/api/v1/sightings") or "/devices" in request.url.path:
            return JSONResponse(status_code=415, content={"detail": "Video uploads are not permitted on this endpoint"})
    return await call_next(request)
