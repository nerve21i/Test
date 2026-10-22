"""레코드 필드 매핑/변환.

지원 transform:
  strip, upper, lower, int, float, str
  alpha_out          SAP ALPHA 변환 해제 (선행 0 제거: "000000000012345" -> "12345")
  alpha_in:<길이>    SAP ALPHA 변환 (선행 0 채움: "12345" -> "000000000012345")
  date:<입력>><출력>  날짜 형식 변환. 예: date:%Y%m%d>%Y-%m-%d
  sap_date           OData v2 "/Date(1700000000000)/" -> "YYYY-MM-DD"
  map:<a=b,c=d>      코드값 치환. 예: map:A=ACTIVE,B=BLOCKED
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .models import FieldMap

_MISSING = object()
_ODATA_DATE = re.compile(r"/Date\((-?\d+)(?:[+-]\d+)?\)/")


class MappingError(ValueError):
    pass


def get_path(data: Any, path: str | None, default: Any = _MISSING) -> Any:
    """점 표기 경로로 값 조회. 리스트는 숫자 인덱스 사용 (예: items.0.sernr)."""
    if path is None or path == "":
        return data
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return default
    return cur


def set_path(data: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def apply_transform(value: Any, spec: str) -> Any:
    if value is None:
        return None
    name, _, arg = spec.partition(":")
    if name == "strip":
        return str(value).strip()
    if name == "upper":
        return str(value).upper()
    if name == "lower":
        return str(value).lower()
    if name == "str":
        return str(value)
    if name == "int":
        return int(str(value).strip() or 0)
    if name == "float":
        return float(str(value).strip() or 0)
    if name == "alpha_out":
        s = str(value).strip()
        if not s.isdigit():
            return s
        return s.lstrip("0") or "0"
    if name == "alpha_in":
        s = str(value).strip()
        return s.zfill(int(arg)) if s.isdigit() else s
    if name == "date":
        fmt_in, sep, fmt_out = arg.partition(">")
        if not sep:
            raise MappingError(f"date 변환 형식 오류: {spec}")
        s = str(value).strip()
        if not s or s.strip("0") == "":  # SAP 초기 날짜 00000000
            return None
        return datetime.strptime(s, fmt_in).strftime(fmt_out)
    if name == "sap_date":
        m = _ODATA_DATE.fullmatch(str(value))
        if not m:
            return value
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    if name == "map":
        table = dict(pair.split("=", 1) for pair in arg.split(",") if "=" in pair)
        return table.get(str(value), value)
    raise MappingError(f"알 수 없는 transform: {spec}")


def map_record(record: Any, rules: list[FieldMap]) -> Any:
    """매핑 규칙이 없으면 원본을 그대로 전달한다."""
    if not rules:
        return record
    out: dict[str, Any] = {}
    for rule in rules:
        if rule.constant is not None:
            value = rule.constant
        else:
            value = get_path(record, rule.source, None)
            for spec in rule.transform:
                try:
                    value = apply_transform(value, spec)
                except MappingError:
                    raise
                except (ValueError, TypeError) as exc:
                    raise MappingError(f"{rule.target}: '{value}' 변환 실패({spec}): {exc}") from exc
        if value is None or value == "":
            value = rule.default
        if rule.required and (value is None or value == ""):
            raise MappingError(f"필수 필드 누락: {rule.target} (source={rule.source})")
        set_path(out, rule.target, value)
    return out
