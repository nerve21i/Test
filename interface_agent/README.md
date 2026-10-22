# SAP ↔ 일련번호시스템 인터페이스 통합관리 에이전트

SAP와 일련번호(시리얼) 관리 시스템 사이의 REST 인터페이스를 한 곳에서 정의·실행·모니터링하는 에이전트입니다.

## 주요 기능

| 기능 | 설명 |
|---|---|
| 인터페이스 레지스트리 | YAML 또는 REST API로 인터페이스(방향, 엔드포인트, 필드 매핑, 재시도 정책)를 등록/수정/활성·비활성 |
| 두 가지 실행 방식 | **push**: 상대 시스템이 `POST /api/v1/inbound/{id}` 호출 · **pull**: 에이전트가 source를 주기적으로 조회 (증분 조회용 `{last_success}` 변수 제공) |
| SAP 커넥터 | Gateway OData v2/v4, `sap-client`, Basic 인증, **X-CSRF-Token 자동 처리**(쿠키 세션 유지), `d.results`/`value` 자동 파싱 |
| 일련번호시스템 커넥터 | REST(JSON), API Key / Bearer / Basic 인증 |
| 필드 매핑 | 점 표기 경로, 필수값 검증, SAP ALPHA 변환(`alpha_in`/`alpha_out`), 날짜 변환, OData `/Date()/`, 코드 치환 |
| 신뢰성 | 408/429/5xx·네트워크 오류는 지수 백오프 재시도, 4xx·매핑 오류는 즉시 실패 처리 |
| 중복 방지 | `key_fields` 기준으로 이미 성공한 건은 `DUPLICATE` 처리 (예: 납품번호+품목+일련번호) |
| 모니터링 | 메시지 단위 로그(원본/변환본/응답/오류/시도횟수), 인터페이스별 통계, **실패 건 재처리**, 웹 대시보드(`/`) |
| 보안 | `AGENT_API_KEY` 설정 시 API 키 인증, 설정 파일 비밀값은 `${ENV_VAR}` 참조, 조회 시 평문 비밀값 마스킹 |

## 빠른 실행 (데모)

```bash
cd interface_agent
pip install -r requirements.txt
AGENT_CONFIG=interfaces.demo.yaml uvicorn --factory agent.main:create_app --port 8000
```

- 대시보드: http://localhost:8000/
- API 문서(Swagger): http://localhost:8000/docs

```bash
# SAP가 출하 일련번호를 보내는 상황을 흉내
curl -X POST localhost:8000/api/v1/inbound/DEMO_DLV_SERIAL -H 'Content-Type: application/json' \
  -d '{"VBELN":"0080000001","POSNR":"000010","MATNR":"000000000000100200","SERNR":"sn-001","WADAT_IST":"20261022"}'
```

## 운영 설정

`interfaces.example.yaml`에 실제 연동을 위한 템플릿 4종이 있습니다.

| ID | 방향 | 방식 | 내용 |
|---|---|---|---|
| `SAP_DLV_SERIAL` | SAP → 일련번호 | push | 납품(출고) 확정 시 일련번호 출하 이력 등록 |
| `SAP_MATERIAL_SYNC` | SAP → 일련번호 | pull 30분 | `API_PRODUCT_SRV` 자재마스터 변경분 일괄 동기화 |
| `SERIAL_PRODUCED_TO_SAP` | 일련번호 → SAP | pull 5분 | 생산완료 일련번호를 SAP 커스텀 OData(`ZSERIAL_SRV`)에 반영 |
| `SERIAL_STATUS_TO_SAP` | 일련번호 → SAP | push | 반품/폐기 등 상태변경 이벤트 SAP 반영 |

URL, 서비스명, 필드명은 실제 시스템에 맞게 수정하세요. `ZSERIAL_SRV`는 예시용 커스텀 서비스 이름입니다.

```bash
export SAP_BASE_URL=https://sap-gw.example.com:44300 SAP_USER=IF_USER SAP_PASSWORD=...
export SERIAL_BASE_URL=https://serial.example.com SERIAL_API_KEY=...
export AGENT_API_KEY=change-me AGENT_DB_PATH=/var/lib/interface-agent/agent.db
AGENT_CONFIG=interfaces.example.yaml uvicorn --factory agent.main:create_app --host 0.0.0.0 --port 8000
```

| 환경변수 | 설명 | 기본값 |
|---|---|---|
| `AGENT_CONFIG` | 시작 시 로드할 인터페이스 YAML | 없음 |
| `AGENT_DB_PATH` | SQLite 로그 DB 경로 | `interface_agent.db` |
| `AGENT_API_KEY` | API 키 (설정 시 `X-API-Key` 헤더 필수) | 없음(인증 없음) |
| `AGENT_SCHEDULER` | pull 스케줄러 사용 여부 | `true` |

### pull 증분 조회 변수

source의 `path`/`params`에 다음 변수를 쓸 수 있습니다. 마지막 성공 실행 시각(UTC) 기준입니다.

- `{last_success}`: ISO 8601 (`2026-10-22T09:00:00+00:00`)
- `{last_success_sap}`: `2026-10-22T09:00:00` (OData `datetimeoffset'...Z'`용)
- `{last_success_date}`: `20261022`

## REST API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/v1/interfaces` | 인터페이스 목록 + 실행 상태 |
| PUT | `/api/v1/interfaces/{id}` | 인터페이스 등록/수정 |
| DELETE | `/api/v1/interfaces/{id}` | 삭제 |
| POST | `/api/v1/interfaces/{id}/enable` · `/disable` | 활성/비활성 |
| POST | `/api/v1/interfaces/{id}/run` | pull 인터페이스 즉시 실행 |
| POST | `/api/v1/inbound/{id}` | push 수신 (단건 객체 또는 배열) |
| GET | `/api/v1/messages?interface_id=&status=` | 메시지 로그 조회 |
| GET | `/api/v1/messages/{id}` | 메시지 상세 (원본/변환본/응답) |
| POST | `/api/v1/messages/{id}/reprocess` | 실패 메시지 재처리 (현재 매핑 정의로 재변환) |
| GET | `/api/v1/stats` | 인터페이스별 상태 건수 |

## 테스트

```bash
cd interface_agent && pytest
```

## 구조

```
agent/
  models.py      인터페이스/메시지 모델
  mapping.py     필드 매핑·변환
  connectors.py  SAP(OData, CSRF) / 일련번호시스템 / mock 커넥터
  engine.py      실행 엔진 (매핑 → 중복체크 → 전송·재시도 → 로그)
  scheduler.py   pull 주기 실행
  store.py       SQLite 저장소
  main.py        FastAPI 앱
  dashboard.html 관리 대시보드
```
