from fastapi import APIRouter

from app.api.v1 import (
    alerts,
    analytics,
    auth,
    complaints,
    devices,
    health,
    hotlist,
    sightings,
    users,
    vehicles,
    ws,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(complaints.router, prefix="/complaints", tags=["complaints"])
api_router.include_router(hotlist.router, prefix="/hotlist", tags=["hotlist"])
api_router.include_router(sightings.router, prefix="/sightings", tags=["sightings"])
api_router.include_router(vehicles.router, prefix="/vehicles", tags=["vehicles"])
api_router.include_router(devices.router, prefix="/devices", tags=["devices"])
api_router.include_router(analytics.router, prefix="/analytics", tags=["analytics"])
api_router.include_router(alerts.router, prefix="/alerts", tags=["alerts"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(ws.router, prefix="/ws", tags=["ws"])
