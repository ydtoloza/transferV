from __future__ import annotations

import asyncio
from typing import Any


class EventBroker:
    """Minimal in-process pub/sub used to push real-time updates via SSE."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: str, data: Any = None) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait({"event": event, "data": data})
            except asyncio.QueueFull:
                pass


broker = EventBroker()
