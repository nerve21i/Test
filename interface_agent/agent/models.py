"""인터페이스 정의 및 메시지 로그 모델."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class AuthConfig(BaseModel):
    """대상 시스템 인증 설정. 비밀값은 ``${ENV_VAR}`` 형태로 환경변수를 참조할 수 있다."""

    type: Literal["none", "basic", "bearer", "api_key"] = "none"
    username: str | None = None
    password: str | None = None
    token: str | None = None
    api_key: str | None = None
    header_name: str = "X-API-Key"


class EndpointConfig(BaseModel):
    """연동 대상 엔드포인트.

    system:
      - ``sap``: SAP Gateway OData/REST (sap-client, CSRF 토큰 자동 처리)
      - ``serial``: 일련번호시스템 REST API
      - ``mock``: 테스트/데모용 메모리 커넥터
    """

    system: Literal["sap", "serial", "mock"]
    base_url: str = ""
    path: str = ""
    method: Literal["GET", "POST", "PUT", "PATCH"] = "POST"
    auth: AuthConfig = Field(default_factory=AuthConfig)
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 30.0
    # SAP 전용
    sap_client: str | None = None
    csrf: bool | None = None  # None이면 SAP 쓰기 요청에서 자동 사용
    # 조회(pull) 응답에서 레코드 목록 위치. 예: "d.results"(OData v2), "value"(OData v4)
    records_path: str | None = None


class FieldMap(BaseModel):
    """필드 매핑 규칙. source(점 표기 경로) 값을 transform 순서대로 변환해 target에 넣는다."""

    target: str
    source: str | None = None
    constant: Any = None
    default: Any = None
    required: bool = False
    transform: list[str] = Field(default_factory=list)


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_seconds: float = Field(default=2.0, ge=0)
    backoff_factor: float = Field(default=2.0, ge=1)


class InterfaceDef(BaseModel):
    """하나의 인터페이스(연동 흐름) 정의."""

    id: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    name: str
    description: str = ""
    direction: Literal["SAP_TO_SERIAL", "SERIAL_TO_SAP"]
    # push: 상대 시스템이 /inbound 로 호출 / pull: 에이전트가 source 를 주기적으로 조회
    mode: Literal["push", "pull"] = "push"
    source: EndpointConfig | None = None
    target: EndpointConfig
    mapping: list[FieldMap] = Field(default_factory=list)
    # 중복 전송 방지 키(매핑 후 target 필드명). 같은 키로 이미 성공한 건은 DUPLICATE 처리
    key_fields: list[str] = Field(default_factory=list)
    # True면 레코드 묶음을 한 번에 전송. batch_wrapper가 있으면 {wrapper: [...]} 형태
    batch: bool = False
    batch_wrapper: str | None = None
    schedule_seconds: int | None = Field(default=None, ge=5)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    enabled: bool = True

    @model_validator(mode="after")
    def _check_pull_source(self) -> "InterfaceDef":
        if self.mode == "pull" and self.source is None:
            raise ValueError("pull 모드 인터페이스는 source가 필요합니다")
        return self


class MessageStatus(str, Enum):
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    DUPLICATE = "DUPLICATE"


class MessageLog(BaseModel):
    id: str
    interface_id: str
    status: MessageStatus
    trigger: str
    message_key: str | None = None
    attempts: int = 0
    payload: Any = None
    mapped: Any = None
    response: Any = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class RunResult(BaseModel):
    interface_id: str
    total: int = 0
    success: int = 0
    failed: int = 0
    duplicate: int = 0
    message_ids: list[str] = Field(default_factory=list)
