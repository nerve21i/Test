"""환경변수 설정 및 YAML 인터페이스 정의 로딩."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import InterfaceDef


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: os.environ.get("AGENT_DB_PATH", "interface_agent.db"))
    config_path: str | None = field(default_factory=lambda: os.environ.get("AGENT_CONFIG"))
    # 설정 시 모든 API 요청에 X-API-Key 헤더 필요
    api_key: str | None = field(default_factory=lambda: os.environ.get("AGENT_API_KEY") or None)
    scheduler_enabled: bool = field(
        default_factory=lambda: os.environ.get("AGENT_SCHEDULER", "true").lower() in ("1", "true", "yes")
    )


def load_interfaces(path: str | Path) -> list[InterfaceDef]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [InterfaceDef.model_validate(item) for item in data.get("interfaces", [])]
