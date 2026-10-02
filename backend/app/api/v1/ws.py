import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.security import decode_token
from app.models import Role
from app.ws.manager import alert_manager

router = APIRouter()

# How long an unauthenticated socket may sit waiting for its first message.
# Without a bound, a client that connects and never sends anything holds the
# connection - and a slot in the manager - indefinitely.
AUTH_HANDSHAKE_TIMEOUT_S = 10

# Close codes. 4401 = credentials problem (refresh and retry), 4403 = the
# account is authenticated but not allowed to read these alerts.
CODE_UNAUTHENTICATED = 4401
CODE_FORBIDDEN = 4403


def _reason(reason: str) -> str:
    """A close reason is a short UTF-8 string; keep it well under the limit."""
    return reason[:120]


@router.websocket("/police")
async def police_ws(websocket: WebSocket):
    """Police dashboard real-time alerts.

    Auth: pass ?token=<access_token> as a query param, OR send
    ``{"type":"auth","token":"<access_token>"}`` as the first message.

    No Origin check is performed here. The browser's same-origin policy already
    decides who may *read* a response, and an Origin allow-list would have to be
    re-derived every time the laptop changed network - precisely the failure this
    endpoint keeps hitting. CORS (explicit origins plus the private-LAN regex in
    main.py) governs the HTTP surface.

    ``accept()`` is called exactly once on the way in, so the handshake either
    succeeds or fails with a code the client can act on.
    """
    token = websocket.query_params.get("token")

    # Accept on both paths, exactly once, before any auth decision.
    await websocket.accept()

    if not token:
        try:
            first = await asyncio.wait_for(
                websocket.receive_text(), timeout=AUTH_HANDSHAKE_TIMEOUT_S
            )
        except (asyncio.TimeoutError, WebSocketDisconnect):
            await websocket.close(
                code=CODE_UNAUTHENTICATED, reason=_reason("no auth message received")
            )
            return

        try:
            data = json.loads(first)
        except Exception:
            data = None

        if not isinstance(data, dict) or data.get("type") != "auth" or not data.get("token"):
            await websocket.close(
                code=CODE_UNAUTHENTICATED,
                reason=_reason('expected {"type":"auth","token":"..."} as the first message'),
            )
            return
        token = data["token"]

    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise ValueError("not an access token")
        role = payload.get("role")
        if role not in (Role.COP.value, Role.ADMIN.value):
            raise PermissionError("role may not read police alerts")
    except Exception as exc:
        # Distinguish "refresh and retry" from "this account is not allowed", so
        # the dashboard can recover from the first and log out on the second.
        forbidden = isinstance(exc, PermissionError)
        code = CODE_FORBIDDEN if forbidden else CODE_UNAUTHENTICATED
        reason = (
            "role may not read police alerts"
            if forbidden
            else f"token rejected: {type(exc).__name__}"
        )
        try:
            await websocket.close(code=code, reason=_reason(reason))
        except Exception:
            pass
        return

    user_id = payload.get("sub")
    await alert_manager.connect(websocket, user_id=user_id, role=role)
    try:
        while True:
            # Drain client frames. The dashboard sends a ping every 25 s, which
            # also stops intermediaries from dropping an idle socket.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        # Always deregister: a socket that dies without a clean disconnect would
        # otherwise stay in the manager and be broadcast to forever.
        alert_manager.disconnect(websocket)