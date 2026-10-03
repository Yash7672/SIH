from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.complaints import router as complaints_router
from app.api.v1.devices import router as devices_router
from app.api.v1.health import router as health_router
from app.api.v1.hotlist import router as hotlist_router
from app.api.v1.ingest import router as ingest_router
from app.api.v1.sightings import router as sightings_router

api_router = APIRouter()
api_router.include_router(health_router, prefix="/health", tags=["health"])
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(complaints_router, prefix="/complaints", tags=["complaints"])
api_router.include_router(devices_router, prefix="/devices", tags=["devices"])
api_router.include_router(hotlist_router, prefix="/hotlist", tags=["hotlist"])
api_router.include_router(sightings_router, prefix="/sightings", tags=["sightings"])
api_router.include_router(ingest_router, prefix="/ingest", tags=["ingest"])
