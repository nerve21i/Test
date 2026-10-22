import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.config import Settings, load_interfaces
from agent.connectors import ConnectorError, MockConnector
from agent.engine import Engine
from agent.main import create_app
from agent.mapping import MappingError, apply_transform, map_record
from agent.models import EndpointConfig, FieldMap, InterfaceDef, MessageStatus
from agent.scheduler import Scheduler
from agent.store import Store

ROOT = Path(__file__).resolve().parents[1]


async def no_sleep(_):
    return None


@pytest.fixture(autouse=True)
def _reset_mock():
    MockConnector.reset()
    yield
    MockConnector.reset()


def dlv_interface(**kw) -> InterfaceDef:
    base = dict(
        id="DLV", name="출하", direction="SAP_TO_SERIAL", mode="push",
        target={"system": "mock", "base_url": "serial"},
        mapping=[
            {"target": "delivery_no", "source": "VBELN", "transform": ["alpha_out"], "required": True},
            {"target": "serial_no", "source": "SERNR", "transform": ["strip", "upper"], "required": True},
            {"target": "shipped_on", "source": "WADAT_IST", "transform": ["date:%Y%m%d>%Y-%m-%d"]},
        ],
        key_fields=["delivery_no", "serial_no"],
        retry={"max_attempts": 3, "backoff_seconds": 0},
    )
    base.update(kw)
    return InterfaceDef.model_validate(base)


# ---------- mapping ----------

def test_transforms():
    assert apply_transform("0000012345", "alpha_out") == "12345"
    assert apply_transform("0000", "alpha_out") == "0"
    assert apply_transform("ABC01", "alpha_out") == "ABC01"
    assert apply_transform("12345", "alpha_in:10") == "0000012345"
    assert apply_transform("20261022", "date:%Y%m%d>%Y-%m-%d") == "2026-10-22"
    assert apply_transform("00000000", "date:%Y%m%d>%Y-%m-%d") is None
    assert apply_transform("/Date(1792540800000)/", "sap_date") == "2026-10-21"
    assert apply_transform("RETURNED", "map:RETURNED=ZRET,SCRAPPED=ZSCR") == "ZRET"


def test_map_record_nested_and_required():
    rules = [FieldMap(target="hdr.no", source="a.b"), FieldMap(target="kind", constant="X")]
    assert map_record({"a": {"b": 1}}, rules) == {"hdr": {"no": 1}, "kind": "X"}
    with pytest.raises(MappingError):
        map_record({}, [FieldMap(target="x", source="missing", required=True)])


def test_pull_requires_source():
    with pytest.raises(ValueError):
        InterfaceDef(id="X", name="x", direction="SAP_TO_SERIAL", mode="pull", target={"system": "mock"})


def test_example_configs_are_valid():
    assert len(load_interfaces(ROOT / "interfaces.example.yaml")) == 4
    assert len(load_interfaces(ROOT / "interfaces.demo.yaml")) == 1


# ---------- engine ----------

def test_inbound_success_and_duplicate():
    store = Store()
    store.upsert_interface(dlv_interface())
    eng = Engine(store, sleep=no_sleep)
    rec = {"VBELN": "0080001234", "SERNR": " sn-001 ", "WADAT_IST": "20261022"}

    r1 = asyncio.run(eng.process_inbound("DLV", [rec]))
    assert (r1.success, r1.failed) == (1, 0)
    assert MockConnector.sent["serial"] == [
        {"delivery_no": "80001234", "serial_no": "SN-001", "shipped_on": "2026-10-22"}
    ]
    r2 = asyncio.run(eng.process_inbound("DLV", rec))
    assert r2.duplicate == 1
    assert len(MockConnector.sent["serial"]) == 1


def test_retry_then_success():
    store = Store()
    store.upsert_interface(dlv_interface())
    MockConnector.fail_next["serial"] = [ConnectorError("503", retryable=True)] * 2
    eng = Engine(store, sleep=no_sleep)
    r = asyncio.run(eng.process_inbound("DLV", {"VBELN": "1", "SERNR": "A"}))
    msg = store.get_message(r.message_ids[0])
    assert msg.status == MessageStatus.SUCCESS and msg.attempts == 3


def test_non_retryable_fails_then_reprocess():
    store = Store()
    store.upsert_interface(dlv_interface())
    MockConnector.fail_next["serial"] = [ConnectorError("400", retryable=False, status_code=400)]
    eng = Engine(store, sleep=no_sleep)
    r = asyncio.run(eng.process_inbound("DLV", {"VBELN": "1", "SERNR": "A"}))
    msg = store.get_message(r.message_ids[0])
    assert msg.status == MessageStatus.FAILED and msg.attempts == 1

    again = asyncio.run(eng.reprocess(msg.id))
    assert again.status == MessageStatus.SUCCESS
    assert again.trigger == "inbound+reprocess"
    with pytest.raises(ValueError):
        asyncio.run(eng.reprocess(msg.id))


def test_mapping_error_is_logged_as_failed():
    store = Store()
    store.upsert_interface(dlv_interface())
    r = asyncio.run(Engine(store, sleep=no_sleep).process_inbound("DLV", {"VBELN": "1"}))
    msg = store.get_message(r.message_ids[0])
    assert msg.status == MessageStatus.FAILED and "필수 필드 누락" in msg.error
    assert MockConnector.sent["serial"] == []


def test_pull_batch():
    store = Store()
    store.upsert_interface(InterfaceDef.model_validate(dict(
        id="MAT", name="자재", direction="SAP_TO_SERIAL", mode="pull",
        source={"system": "mock", "base_url": "sap"}, target={"system": "mock", "base_url": "serial"},
        mapping=[{"target": "material", "source": "Product", "transform": ["alpha_out"]}],
        batch=True, batch_wrapper="items",
    )))
    MockConnector.records["sap"] = [{"Product": "000000000000000010"}, {"Product": "000000000000000020"}]
    r = asyncio.run(Engine(store, sleep=no_sleep).run_pull("MAT"))
    assert r.success == 1
    assert MockConnector.sent["serial"] == [{"items": [{"material": "10"}, {"material": "20"}]}]
    assert store.get_state("MAT")["last_success_at"] is not None


# ---------- HTTP connectors (SAP CSRF / OData) ----------

def test_sap_connector_csrf_and_odata_pull():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.path.endswith("/A_Product"):
            assert req.url.params["sap-client"] == "100"
            assert "1970-01-01T00:00:00" in req.url.params["$filter"]
            return httpx.Response(200, json={"d": {"results": [{"Product": "0000000042"}]}})
        if req.method == "GET" and req.headers.get("x-csrf-token") == "Fetch":
            return httpx.Response(200, headers={"x-csrf-token": "TOKEN123", "set-cookie": "SAP_SESSIONID=abc; Path=/"})
        if req.method == "POST":
            assert req.headers["x-csrf-token"] == "TOKEN123"
            assert "SAP_SESSIONID=abc" in req.headers.get("cookie", "")
            assert req.headers["authorization"].startswith("Basic ")
            return httpx.Response(201, json={"d": {"Matnr": json.loads(req.content)["Matnr"]}})
        return httpx.Response(404)

    factory = lambda ep: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = Store()
    sap = {"system": "sap", "base_url": "https://sap.example", "sap_client": "100",
           "auth": {"type": "basic", "username": "u", "password": "p"}}
    store.upsert_interface(InterfaceDef.model_validate(dict(
        id="P", name="p", direction="SERIAL_TO_SAP", mode="pull",
        source={**sap, "path": "/sap/opu/odata/sap/API_PRODUCT_SRV/A_Product", "method": "GET",
                "params": {"$filter": "LastChangeDateTime gt datetimeoffset'{last_success_sap}Z'"}},
        target={**sap, "path": "/sap/opu/odata/sap/ZSERIAL_SRV/SerialSet"},
        mapping=[{"target": "Matnr", "source": "Product", "transform": ["alpha_out", "alpha_in:18"]}],
    )))
    r = asyncio.run(Engine(store, factory, sleep=no_sleep).run_pull("P"))
    assert r.success == 1, store.get_message(r.message_ids[0]).error
    assert [c.method for c in calls] == ["GET", "GET", "POST"]


def test_http_5xx_is_retried():
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(503 if n["c"] < 2 else 200, json={"ok": True})

    factory = lambda ep: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = Store()
    store.upsert_interface(dlv_interface(target={"system": "serial", "base_url": "https://sn.example", "path": "/s"}))
    r = asyncio.run(Engine(store, factory, sleep=no_sleep).process_inbound("DLV", {"VBELN": "1", "SERNR": "A"}))
    assert r.success == 1 and n["c"] == 2


def test_env_secret_resolution(monkeypatch):
    from agent.connectors import HttpConnector
    monkeypatch.setenv("SN_KEY", "secret")
    c = HttpConnector(EndpointConfig(system="serial", auth={"type": "api_key", "api_key": "${SN_KEY}"}))
    assert c._headers()["X-API-Key"] == "secret"


# ---------- scheduler ----------

def test_scheduler_due():
    store = Store()
    store.upsert_interface(InterfaceDef.model_validate(dict(
        id="S", name="s", direction="SERIAL_TO_SAP", mode="pull", schedule_seconds=60,
        source={"system": "mock"}, target={"system": "mock"},
    )))
    store.upsert_interface(dlv_interface())
    sch = Scheduler(Engine(store))
    assert sch.due(now=0) == ["S"]
    assert sch.due(now=30) == []
    assert sch.due(now=61) == ["S"]


# ---------- REST API ----------

@pytest.fixture
def client(tmp_path):
    settings = Settings(db_path=str(tmp_path / "t.db"), config_path=str(ROOT / "interfaces.demo.yaml"),
                        api_key="k", scheduler_enabled=False)
    with TestClient(create_app(settings)) as c:
        c.headers["X-API-Key"] = "k"
        yield c


def test_api_flow(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/v1/interfaces", headers={"X-API-Key": "bad"}).status_code == 401
    assert [i["id"] for i in client.get("/api/v1/interfaces").json()] == ["DEMO_DLV_SERIAL"]

    r = client.post("/api/v1/inbound/DEMO_DLV_SERIAL", json=[
        {"VBELN": "0080000001", "POSNR": "000010", "MATNR": "000000000000100200", "SERNR": "sn1", "WADAT_IST": "20261022"},
        {"VBELN": "0080000001", "POSNR": "000010", "MATNR": "000000000000100200"},
    ]).json()
    assert (r["success"], r["failed"]) == (1, 1)

    failed = client.get("/api/v1/messages", params={"status": "FAILED"}).json()
    assert len(failed) == 1
    assert client.post(f"/api/v1/messages/{failed[0]['id']}/reprocess").status_code == 200  # 여전히 매핑 오류
    assert client.get(f"/api/v1/messages/{failed[0]['id']}").json()["status"] == "FAILED"

    stats = client.get("/api/v1/stats").json()["DEMO_DLV_SERIAL"]
    assert stats["counts"] == {"SUCCESS": 1, "FAILED": 1}

    assert client.post("/api/v1/interfaces/DEMO_DLV_SERIAL/disable").json()["enabled"] is False
    assert client.post("/api/v1/inbound/DEMO_DLV_SERIAL", json={"VBELN": "1"}).status_code == 409
    assert client.post("/api/v1/interfaces/DEMO_DLV_SERIAL/run").status_code == 409
    assert "인터페이스 통합관리" in client.get("/").text


def test_api_masks_plain_secrets(client):
    body = dlv_interface(id="SEC", target={"system": "serial", "base_url": "https://x",
                                           "auth": {"type": "bearer", "token": "plain-secret"}}).model_dump()
    assert client.put("/api/v1/interfaces/SEC", json=body).status_code == 200
    assert client.get("/api/v1/interfaces/SEC").json()["target"]["auth"]["token"] == "******"
    assert client.delete("/api/v1/interfaces/SEC").status_code == 204
    assert client.get("/api/v1/interfaces/SEC").status_code == 404
