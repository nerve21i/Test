"""pull 인터페이스 주기 실행 스케줄러."""
from __future__ import annotations

import asyncio
import logging
import time

from .engine import Engine

log = logging.getLogger("interface_agent")


class Scheduler:
    def __init__(self, engine: Engine, tick_seconds: float = 1.0):
        self.engine = engine
        self.tick = tick_seconds
        self._next_run: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._running: set[str] = set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def due(self, now: float | None = None) -> list[str]:
        """지금 실행해야 할 인터페이스 ID 목록을 반환하고 다음 실행 시각을 갱신한다."""
        now = time.monotonic() if now is None else now
        out = []
        for d in self.engine.store.list_interfaces():
            if not (d.enabled and d.mode == "pull" and d.schedule_seconds):
                self._next_run.pop(d.id, None)
                continue
            nxt = self._next_run.get(d.id)
            if nxt is None or now >= nxt:
                self._next_run[d.id] = now + d.schedule_seconds
                if d.id not in self._running:
                    out.append(d.id)
        return out

    async def _run_one(self, interface_id: str) -> None:
        self._running.add(interface_id)
        try:
            r = await self.engine.run_pull(interface_id, trigger="schedule")
            if r.total:
                log.info("[%s] 스케줄 실행: 총 %d / 성공 %d / 실패 %d", interface_id, r.total, r.success, r.failed)
        except Exception as exc:  # 스케줄러는 개별 실패로 멈추지 않는다
            log.error("[%s] 스케줄 실행 오류: %s", interface_id, exc)
        finally:
            self._running.discard(interface_id)

    async def _loop(self) -> None:
        while True:
            for interface_id in self.due():
                asyncio.create_task(self._run_one(interface_id))
            await asyncio.sleep(self.tick)
