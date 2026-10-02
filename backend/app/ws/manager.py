import asyncio
import json
from typing import Any

from fastapi import WebSocket


class PoliceAlertManager:
    """Tracks connected police dashboard WebSocket clients and pushes alert events."""

    def __init__(self) -> None:
        self._connections: dict[WebSocket, dict[str, Any]] = {}

    async def connect(self, websocket: WebSocket, user_id: str, role: str) -> None:
        self._connections[websocket] = {"user_id": user_id, "role": role}

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.pop(websocket, None)

    @property
    def count(self) -> int:
        return len(self._connections)

    async def broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        message = json.dumps({"type": event_type, "payload": payload})
        dead: list[WebSocket] = []
        for ws in list(self._connections.keys()):
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


alert_manager = PoliceAlertManager()
