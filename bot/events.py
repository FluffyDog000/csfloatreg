"""Шина событий: рантайм -> веб-интерфейс (SSE) и консоль.

Хаб держит короткую историю, чтобы вкладка, открытая в середине прогона,
сразу показала актуальную картину, а не пустой экран.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any


class EventHub:
    def __init__(self, history: int = 400):
        self._subscribers: set[asyncio.Queue] = set()
        self._history: deque[dict] = deque(maxlen=history)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ── публикация ───────────────────────────────────────────
    def publish(self, kind: str, **payload: Any) -> None:
        event = {"kind": kind, "ts": time.time(), **payload}
        self._history.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def publish_threadsafe(self, kind: str, **payload: Any) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: self.publish(kind, **payload))
        else:
            self.publish(kind, **payload)

    # ── подписка ─────────────────────────────────────────────
    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def history(self) -> list[dict]:
        return list(self._history)


class HubLogHandler(logging.Handler):
    """Зеркалит логи в веб-интерфейс."""

    def __init__(self, hub: EventHub, level: int = logging.INFO):
        super().__init__(level)
        self.hub = hub
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.hub.publish_threadsafe("log", level=record.levelname, text=self.format(record))
        except Exception:  # noqa: BLE001 — логгер не имеет права ронять прогон
            pass


#: Глобальный хаб. CLI его просто не слушает.
hub = EventHub()
