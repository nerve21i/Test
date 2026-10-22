"""SAP / 일련번호시스템 커넥터."""
from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Any, Callable

import httpx

from .mapping import get_path
from .models import EndpointConfig

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_env(value: str | None) -> str | None:
    """``${VAR}`` 를 환경변수 값으로 치환한다. 설정 파일에 비밀번호를 직접 쓰지 않기 위함."""
    if value is None:
        return None
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)


class ConnectorError(Exception):
    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.body = body


ClientFactory = Callable[[EndpointConfig], httpx.AsyncClient]


def default_client_factory(endpoint: EndpointConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=endpoint.timeout_seconds)


class HttpConnector:
    """일반 REST 커넥터 (일련번호시스템 등)."""

    def __init__(self, endpoint: EndpointConfig, client_factory: ClientFactory = default_client_factory):
        self.ep = endpoint
        self.client_factory = client_factory

    def _url(self, path_vars: dict[str, Any] | None = None) -> str:
        path = self.ep.path
        if path_vars:
            path = path.format_map(defaultdict(str, path_vars))
        return resolve_env(self.ep.base_url).rstrip("/") + "/" + path.lstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        headers.update({k: resolve_env(v) or "" for k, v in self.ep.headers.items()})
        auth = self.ep.auth
        if auth.type == "bearer":
            headers["Authorization"] = f"Bearer {resolve_env(auth.token)}"
        elif auth.type == "api_key":
            headers[auth.header_name] = resolve_env(auth.api_key) or ""
        return headers

    def _auth(self) -> httpx.BasicAuth | None:
        a = self.ep.auth
        if a.type == "basic":
            return httpx.BasicAuth(resolve_env(a.username) or "", resolve_env(a.password) or "")
        return None

    def _params(self, path_vars: dict[str, Any] | None = None) -> dict[str, str]:
        params = {k: resolve_env(v) or "" for k, v in self.ep.params.items()}
        if path_vars:
            params = {k: v.format_map(defaultdict(str, path_vars)) for k, v in params.items()}
        return params

    async def _prepare(self, client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> None:
        """쓰기 요청 직전 훅 (SAP CSRF 등)."""

    @staticmethod
    def _parse(resp: httpx.Response) -> Any:
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text[:2000]

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        # 408/429/5xx 는 일시 장애로 보고 재시도, 그 외 4xx 는 데이터/설정 오류
        retryable = resp.status_code in (408, 429) or resp.status_code >= 500
        raise ConnectorError(
            f"{self.ep.system} HTTP {resp.status_code}",
            retryable=retryable,
            status_code=resp.status_code,
            body=self._parse(resp),
        )

    async def send(self, payload: Any) -> Any:
        url = self._url(payload if isinstance(payload, dict) else None)
        headers = self._headers()
        try:
            async with self.client_factory(self.ep) as client:
                await self._prepare(client, url, headers)
                resp = await client.request(
                    self.ep.method, url, json=payload, headers=headers, params=self._params(), auth=self._auth()
                )
        except httpx.TransportError as exc:
            raise ConnectorError(f"{self.ep.system} 통신 오류: {exc!r}", retryable=True) from exc
        self._raise_for_status(resp)
        return self._parse(resp)

    async def fetch(self, path_vars: dict[str, Any] | None = None) -> list[Any]:
        url = self._url(path_vars)
        try:
            async with self.client_factory(self.ep) as client:
                resp = await client.get(
                    url, headers=self._headers(), params=self._params(path_vars), auth=self._auth()
                )
        except httpx.TransportError as exc:
            raise ConnectorError(f"{self.ep.system} 통신 오류: {exc!r}", retryable=True) from exc
        self._raise_for_status(resp)
        data = self._parse(resp)
        records = get_path(data, self.ep.records_path or self._default_records_path(data), None)
        if records is None:
            return []
        return records if isinstance(records, list) else [records]

    def _default_records_path(self, data: Any) -> str | None:
        return None


class SapConnector(HttpConnector):
    """SAP Gateway(OData) 커넥터: sap-client 파라미터와 X-CSRF-Token 처리를 자동으로 한다."""

    def _params(self, path_vars: dict[str, Any] | None = None) -> dict[str, str]:
        params = super()._params(path_vars)
        if self.ep.sap_client:
            params.setdefault("sap-client", self.ep.sap_client)
        return params

    async def _prepare(self, client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> None:
        use_csrf = self.ep.csrf if self.ep.csrf is not None else self.ep.method != "GET"
        if not use_csrf:
            return
        try:
            resp = await client.get(
                url, headers={**headers, "X-CSRF-Token": "Fetch"}, params=self._params(), auth=self._auth()
            )
        except httpx.TransportError as exc:
            raise ConnectorError(f"SAP CSRF 토큰 조회 실패: {exc!r}", retryable=True) from exc
        token = resp.headers.get("x-csrf-token")
        if not token or token.lower() == "required":
            raise ConnectorError(
                f"SAP CSRF 토큰을 받지 못했습니다 (HTTP {resp.status_code})",
                retryable=resp.status_code >= 500,
                status_code=resp.status_code,
            )
        headers["X-CSRF-Token"] = token
        # 세션 쿠키는 같은 client 의 cookie jar 에 유지되어 본 요청에 함께 전송된다

    def _default_records_path(self, data: Any) -> str | None:
        if isinstance(data, dict):
            if isinstance(data.get("d"), dict) and "results" in data["d"]:
                return "d.results"  # OData v2
            if "value" in data:
                return "value"  # OData v4
        return None


class MockConnector:
    """테스트/데모용. 시스템 이름(base_url)별로 송신 내역과 조회 데이터를 메모리에 둔다."""

    sent: dict[str, list[Any]] = defaultdict(list)
    records: dict[str, list[Any]] = defaultdict(list)
    fail_next: dict[str, list[ConnectorError]] = defaultdict(list)

    def __init__(self, endpoint: EndpointConfig, client_factory: ClientFactory | None = None):
        self.name = endpoint.base_url or "mock"

    @classmethod
    def reset(cls) -> None:
        cls.sent.clear()
        cls.records.clear()
        cls.fail_next.clear()

    async def send(self, payload: Any) -> Any:
        if self.fail_next[self.name]:
            raise self.fail_next[self.name].pop(0)
        self.sent[self.name].append(payload)
        return {"result": "OK", "seq": len(self.sent[self.name])}

    async def fetch(self, path_vars: dict[str, Any] | None = None) -> list[Any]:
        if self.fail_next[self.name]:
            raise self.fail_next[self.name].pop(0)
        out, self.records[self.name] = list(self.records[self.name]), []
        return out


def build_connector(endpoint: EndpointConfig, client_factory: ClientFactory = default_client_factory):
    if endpoint.system == "sap":
        return SapConnector(endpoint, client_factory)
    if endpoint.system == "serial":
        return HttpConnector(endpoint, client_factory)
    return MockConnector(endpoint)
