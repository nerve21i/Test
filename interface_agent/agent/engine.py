"""인터페이스 실행 엔진: 매핑 → 중복체크 → 전송(재시도) → 로그."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from .connectors import ClientFactory, ConnectorError, build_connector, default_client_factory
from .mapping import MappingError, get_path, map_record
from .models import InterfaceDef, MessageLog, MessageStatus, RunResult
from .store import Store, now

log = logging.getLogger("interface_agent")


class InterfaceNotFound(LookupError):
    pass


class Engine:
    def __init__(self, store: Store, client_factory: ClientFactory = default_client_factory,
                 sleep=asyncio.sleep):
        self.store = store
        self.client_factory = client_factory
        self._sleep = sleep
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, interface_id: str) -> asyncio.Lock:
        return self._locks.setdefault(interface_id, asyncio.Lock())

    def _get(self, interface_id: str) -> InterfaceDef:
        d = self.store.get_interface(interface_id)
        if d is None:
            raise InterfaceNotFound(interface_id)
        return d

    @staticmethod
    def message_key(d: InterfaceDef, mapped: Any) -> str | None:
        if not d.key_fields or not isinstance(mapped, dict):
            return None
        values = [get_path(mapped, f, None) for f in d.key_fields]
        raw = json.dumps(values, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    # ---- 외부 진입점 ----
    async def process_inbound(self, interface_id: str, payload: Any, trigger: str = "inbound") -> RunResult:
        """상대 시스템이 보낸 데이터(단건 또는 목록)를 처리한다."""
        d = self._get(interface_id)
        records = payload if isinstance(payload, list) else [payload]
        started = now()
        result = await self._process(d, records, trigger)
        ok = result.failed == 0
        self.store.record_run(interface_id, started_at=started, ok=ok, error=None if ok else f"{result.failed}건 실패")
        return result

    async def run_pull(self, interface_id: str, trigger: str = "manual") -> RunResult:
        """source 시스템에서 데이터를 조회해 target으로 전송한다."""
        d = self._get(interface_id)
        if d.mode != "pull" or d.source is None:
            raise ValueError(f"{interface_id} 는 pull 인터페이스가 아닙니다")
        async with self._lock(interface_id):
            started = now()
            state = self.store.get_state(interface_id)
            last = state["last_success_at"]
            last_dt = datetime.fromisoformat(last) if last else datetime(1970, 1, 1)
            path_vars = {
                "last_success": last_dt.isoformat(),
                "last_success_sap": last_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "last_success_date": last_dt.strftime("%Y%m%d"),
            }
            try:
                records = await build_connector(d.source, self.client_factory).fetch(path_vars)
            except ConnectorError as exc:
                self.store.record_run(interface_id, started_at=started, ok=False, error=str(exc))
                raise
            result = await self._process(d, records, trigger)
            ok = result.failed == 0
            self.store.record_run(interface_id, started_at=started, ok=ok,
                                  error=None if ok else f"{result.failed}건 실패")
            return result

    async def reprocess(self, message_id: str) -> MessageLog:
        """실패 메시지를 현재 인터페이스 정의로 다시 매핑/전송한다."""
        msg = self.store.get_message(message_id)
        if msg is None:
            raise LookupError(message_id)
        if msg.status != MessageStatus.FAILED:
            raise ValueError(f"FAILED 상태만 재처리할 수 있습니다 (현재 {msg.status.value})")
        d = self._get(msg.interface_id)
        msg.trigger = msg.trigger if msg.trigger.endswith("+reprocess") else msg.trigger + "+reprocess"
        if d.batch:
            await self._handle_batch(d, msg, msg.payload)
        else:
            await self._handle_record(d, msg, msg.payload)
        return msg

    # ---- 내부 처리 ----
    async def _process(self, d: InterfaceDef, records: list[Any], trigger: str) -> RunResult:
        if not d.enabled:
            raise ValueError(f"{d.id} 인터페이스가 비활성화 상태입니다")
        result = RunResult(interface_id=d.id)
        if not records:
            return result
        if d.batch:
            msg = self.store.create_message(d.id, trigger, records)
            await self._handle_batch(d, msg, records)
            msgs = [msg]
        else:
            msgs = []
            for rec in records:
                msg = self.store.create_message(d.id, trigger, rec)
                await self._handle_record(d, msg, rec)
                msgs.append(msg)
        for m in msgs:
            result.total += 1
            result.message_ids.append(m.id)
            if m.status == MessageStatus.SUCCESS:
                result.success += 1
            elif m.status == MessageStatus.DUPLICATE:
                result.duplicate += 1
            else:
                result.failed += 1
        return result

    async def _handle_record(self, d: InterfaceDef, msg: MessageLog, record: Any) -> None:
        try:
            msg.mapped = map_record(record, d.mapping)
        except MappingError as exc:
            return self._fail(msg, f"매핑 오류: {exc}")
        msg.message_key = self.message_key(d, msg.mapped)
        if msg.message_key and self.store.has_success(d.id, msg.message_key, exclude_id=msg.id):
            msg.status = MessageStatus.DUPLICATE
            msg.error = "이미 성공 처리된 키입니다"
            self.store.save_message(msg)
            return
        await self._send(d, msg, msg.mapped)

    async def _handle_batch(self, d: InterfaceDef, msg: MessageLog, records: list[Any]) -> None:
        try:
            items = [map_record(r, d.mapping) for r in records]
        except MappingError as exc:
            return self._fail(msg, f"매핑 오류: {exc}")
        msg.mapped = {d.batch_wrapper: items} if d.batch_wrapper else items
        await self._send(d, msg, msg.mapped)

    async def _send(self, d: InterfaceDef, msg: MessageLog, body: Any) -> None:
        connector = build_connector(d.target, self.client_factory)
        delay = d.retry.backoff_seconds
        for attempt in range(1, d.retry.max_attempts + 1):
            msg.attempts += 1
            try:
                msg.response = await connector.send(body)
            except ConnectorError as exc:
                msg.response = exc.body
                msg.error = str(exc)
                log.warning("[%s] 전송 실패 (%d/%d): %s", d.id, attempt, d.retry.max_attempts, exc)
                if not exc.retryable or attempt == d.retry.max_attempts:
                    return self._fail(msg, msg.error)
                await self._sleep(delay)
                delay *= d.retry.backoff_factor
                continue
            msg.status = MessageStatus.SUCCESS
            msg.error = None
            self.store.save_message(msg)
            return

    def _fail(self, msg: MessageLog, error: str) -> None:
        msg.status = MessageStatus.FAILED
        msg.error = error
        self.store.save_message(msg)
