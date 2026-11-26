from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    def __init__(self, max_total: int = 150, max_per_ip: int = 15) -> None:
        self._connections: dict[WebSocket, str] = {}
        self._ip_counts: dict[str, int] = {}
        self._max_total = max_total
        self._max_per_ip = max_per_ip
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, client_ip: str = "unknown") -> bool:
        async with self._lock:
            if len(self._connections) >= self._max_total:
                await websocket.close(code=1013, reason="Server busy")
                return False
            if self._ip_counts.get(client_ip, 0) >= self._max_per_ip:
                await websocket.close(code=1008, reason="Too many connections")
                return False
            await websocket.accept()
            self._connections[websocket] = client_ip
            self._ip_counts[client_ip] = self._ip_counts.get(client_ip, 0) + 1
            return True

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            ip = self._connections.pop(websocket, None)
            if ip is not None:
                new_cnt = self._ip_counts.get(ip, 1) - 1
                if new_cnt <= 0:
                    self._ip_counts.pop(ip, None)
                else:
                    self._ip_counts[ip] = new_cnt

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._connections.keys())
        if not targets:
            return

        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    ip = self._connections.pop(ws, None)
                    if ip is not None:
                        new_cnt = self._ip_counts.get(ip, 1) - 1
                        if new_cnt <= 0:
                            self._ip_counts.pop(ip, None)
                        else:
                            self._ip_counts[ip] = new_cnt
