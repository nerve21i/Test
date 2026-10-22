"""인터페이스 통합관리 에이전트 REST API."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse

from .config import Settings, load_interfaces
from .connectors import ClientFactory, ConnectorError, default_client_factory
from .engine import Engine, InterfaceNotFound
from .models import InterfaceDef, MessageLog, MessageStatus, RunResult
from .scheduler import Scheduler
from .store import Store

log = logging.getLogger("interface_agent")
_DASHBOARD = Path(__file__).with_name("dashboard.html")
_SECRET_FIELDS = ("password", "token", "api_key")


def _masked(d: InterfaceDef) -> dict[str, Any]:
    """조회 응답에서 평문 비밀값을 가린다 (${ENV} 참조는 그대로 노출)."""
    data = d.model_dump()
    for ep in (data.get("source"), data.get("target")):
        if not ep:
            continue
        for f in _SECRET_FIELDS:
            v = ep["auth"].get(f)
            if v and not str(v).startswith("${"):
                ep["auth"][f] = "******"
    return data


def create_app(settings: Settings | None = None, client_factory: ClientFactory = default_client_factory,
               engine: Engine | None = None) -> FastAPI:
    settings = settings or Settings()
    store = engine.store if engine else Store(settings.db_path)
    engine = engine or Engine(store, client_factory)
    scheduler = Scheduler(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.config_path:
            for d in load_interfaces(settings.config_path):
                store.upsert_interface(d)
            log.info("인터페이스 정의 로드: %s", settings.config_path)
        if settings.scheduler_enabled:
            scheduler.start()
        yield
        await scheduler.stop()

    app = FastAPI(title="SAP ↔ 일련번호시스템 인터페이스 통합관리 에이전트", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.scheduler = scheduler

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        if settings.api_key and x_api_key != settings.api_key:
            raise HTTPException(401, "유효하지 않은 API 키")

    def get_interface(interface_id: str) -> InterfaceDef:
        d = store.get_interface(interface_id)
        if d is None:
            raise HTTPException(404, f"인터페이스 없음: {interface_id}")
        return d

    def connector_http_error(exc: ConnectorError) -> HTTPException:
        return HTTPException(502, {"error": str(exc), "status_code": exc.status_code, "body": exc.body})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> str:
        return _DASHBOARD.read_text(encoding="utf-8")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    api = [Depends(require_key)]

    # ---- 인터페이스 관리 ----
    @app.get("/api/v1/interfaces", dependencies=api)
    def list_interfaces() -> list[dict[str, Any]]:
        return [{**_masked(d), "state": store.get_state(d.id)} for d in store.list_interfaces()]

    @app.get("/api/v1/interfaces/{interface_id}", dependencies=api)
    def read_interface(interface_id: str) -> dict[str, Any]:
        d = get_interface(interface_id)
        return {**_masked(d), "state": store.get_state(d.id)}

    @app.put("/api/v1/interfaces/{interface_id}", dependencies=api)
    def put_interface(interface_id: str, d: InterfaceDef) -> dict[str, Any]:
        if d.id != interface_id:
            raise HTTPException(400, "경로와 본문의 id가 다릅니다")
        store.upsert_interface(d)
        return _masked(d)

    @app.delete("/api/v1/interfaces/{interface_id}", status_code=204, dependencies=api)
    def delete_interface(interface_id: str) -> None:
        if not store.delete_interface(interface_id):
            raise HTTPException(404, f"인터페이스 없음: {interface_id}")

    @app.post("/api/v1/interfaces/{interface_id}/enable", dependencies=api)
    def enable(interface_id: str) -> dict[str, Any]:
        d = get_interface(interface_id)
        d.enabled = True
        store.upsert_interface(d)
        return {"id": d.id, "enabled": True}

    @app.post("/api/v1/interfaces/{interface_id}/disable", dependencies=api)
    def disable(interface_id: str) -> dict[str, Any]:
        d = get_interface(interface_id)
        d.enabled = False
        store.upsert_interface(d)
        return {"id": d.id, "enabled": False}

    @app.post("/api/v1/interfaces/{interface_id}/run", dependencies=api)
    async def run(interface_id: str) -> RunResult:
        get_interface(interface_id)
        try:
            return await engine.run_pull(interface_id, trigger="manual")
        except ConnectorError as exc:
            raise connector_http_error(exc)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    # ---- 수신 (SAP / 일련번호시스템 → 에이전트) ----
    @app.post("/api/v1/inbound/{interface_id}", dependencies=api)
    async def inbound(interface_id: str, payload: Any = Body(...)) -> RunResult:
        d = get_interface(interface_id)
        if d.mode != "push":
            raise HTTPException(409, f"{interface_id} 는 push 인터페이스가 아닙니다")
        try:
            return await engine.process_inbound(interface_id, payload)
        except (InterfaceNotFound, ValueError) as exc:
            raise HTTPException(409, str(exc))

    # ---- 모니터링 ----
    @app.get("/api/v1/messages", dependencies=api)
    def list_messages(
        interface_id: str | None = None, status: MessageStatus | None = None,
        limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
    ) -> list[MessageLog]:
        return store.list_messages(interface_id, status, limit=limit, offset=offset)

    @app.get("/api/v1/messages/{message_id}", dependencies=api)
    def read_message(message_id: str) -> MessageLog:
        msg = store.get_message(message_id)
        if msg is None:
            raise HTTPException(404, "메시지 없음")
        return msg

    @app.post("/api/v1/messages/{message_id}/reprocess", dependencies=api)
    async def reprocess(message_id: str) -> MessageLog:
        try:
            return await engine.reprocess(message_id)
        except LookupError:
            raise HTTPException(404, "메시지 또는 인터페이스 없음")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/v1/stats", dependencies=api)
    def stats() -> dict[str, Any]:
        counts = store.stats()
        return {
            d.id: {
                "name": d.name, "direction": d.direction, "mode": d.mode, "enabled": d.enabled,
                "counts": counts.get(d.id, {}), **store.get_state(d.id),
            }
            for d in store.list_interfaces()
        }

    return app

