"""Live feed: every connection is bound to a user; a frame goes to the owner
of the job it describes (and to admins who asked for everything).

Same two frame shapes as agent2's dashboard:
  {"channel": "session", "session_id", "task", "kind", "timestamp", "data"}
  {"channel": "job", "job_id", "method", "status", "task", "event"}
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

from fastapi import WebSocket

log = logging.getLogger("swarn_backend.ws")


class LiveManager:
    def __init__(self) -> None:
        self._by_user: dict[str, set[WebSocket]] = defaultdict(set)
        self._firehose: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, user_id: str, firehose: bool = False) -> None:
        await ws.accept()
        async with self._lock:
            self._by_user[user_id].add(ws)
            if firehose:
                self._firehose.add(ws)

    async def disconnect(self, ws: WebSocket, user_id: str) -> None:
        async with self._lock:
            self._by_user[user_id].discard(ws)
            if not self._by_user[user_id]:
                self._by_user.pop(user_id, None)
            self._firehose.discard(ws)

    async def send_to_user(self, user_id: str, payload: dict) -> None:
        text = json.dumps(payload, default=str)
        targets = set(self._by_user.get(user_id, ())) | self._firehose
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001 — a dead socket is dropped, never raised
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    for conns in self._by_user.values():
                        conns.discard(ws)
                    self._firehose.discard(ws)

    def connections(self) -> int:
        return sum(len(c) for c in self._by_user.values())


_manager: LiveManager | None = None


def get_live() -> LiveManager:
    global _manager
    if _manager is None:
        _manager = LiveManager()
    return _manager
