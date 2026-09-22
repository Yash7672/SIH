from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.core.security import decode_token
from app.db.session import SessionLocal
from app.models import Role, User
from app.ws.manager import alert_manager
from uuid import UUID

router = APIRouter()


@router.websocket("/police")
async def police_ws(websocket: WebSocket):
    """Police dashboard real-time alerts.

    Auth: send {"type":"auth","token":"<access_token>"} as first message,
    OR pass ?token= access token as query param.
    """
    token = websocket.query_params.get("token")
    if not token:
        await websocket.accept()
        try:
            first = await websocket.receive_text()
            import json

            data = json.loads(first)
            if data.get("type") == "auth":
                token = data.get("token")
        except Exception:
            token = None
        if not token:
            await websocket.close(code=4401)
            return
    else:
        await websocket.accept()

    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise ValueError("wrong type")
        role = payload.get("role")
        if role not in (Role.COP.value, Role.ADMIN.value):
            await websocket.close(code=4403)
            return
        user_id = payload.get("sub")
    except Exception:
        try:
            await websocket.close(code=4401)
        except Exception:
            pass
        return

    await alert_manager.connect(websocket, user_id=user_id, role=role)
    try:
        while True:
            # Keep connection open; client may send pings
            await websocket.receive_text()
    except WebSocketDisconnect:
        alert_manager.disconnect(websocket)
    except Exception:
        alert_manager.disconnect(websocket)
