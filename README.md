# ai-tutor : didgo

딛고는 단계별 질문과 힌트를 주는 AI 튜터

## 로컬 개발 환경

팀 내부 실험 및 검증 진행할 때 공통된 환경에서 진행하기 위해 Docker Compose 로 진행 
저장소 루트에서 다음 명령을 실행하면 API와 SQLite를 사용 가능 (로컬 개발 환경에서 검증 후 PostgreSQL로 전환 가능 compose.postgres.yaml)

```bash
docker compose up --build --wait
curl --fail http://localhost:8000/health
docker compose run --rm --no-deps api pytest -q
docker compose down
```

`/health` 응답은 API와 실제 DB 연결을 확인하며, API 문서는 `http://localhost:8000/docs`
기본 포트가 사용 중이면 `.env.example`을 `.env`로 복사하고 `API_PORT`를 변경
Docker가 빌드 경로에 대해 `fs.read` 권한 오류를 표시하면 이미지를 직접 빌드한 뒤 Compose로 실행

```bash
docker build -t ai-tutor-api ./backend
docker compose -p ai-tutor up --no-build --wait
```

같은 이미지를 PostgreSQL 검증에 쓸 때도 아래 시작 명령의 `--build`를 `--no-build`로 바꾸면 됩니다.

## PostgreSQL 통합 환경

기본 환경을 종료한 다음, 같은 API를 PostgreSQL로 실행

```bash
docker compose -f compose.yaml -f compose.postgres.yaml up --build --wait
curl --fail http://localhost:8000/health
docker compose -f compose.yaml -f compose.postgres.yaml down
```

두 환경은 DB 연결 설정만 다르게 사용합니다. PostgreSQL은 Compose 내부에서 접속하며 로컬 검증용 계정을 사용합니다. 현재 단계의 검증 대상은 API 기동과 DB 연결입니다. 문항·세션 테이블과 마이그레이션은 첫 저장 기능과 함께 추가합니다.

## 폴더 역할

| 위치 | 역할 |
| --- | --- |
| `backend/app/tutor/` | 일차방정식 파서, 중간식 판정, 검산, 힌트 로직 |
| `backend/app/db/`, `backend/migrations/` | DB 연결, 테이블 정의, 스키마 변경 이력 |
| `backend/tests/` | API 및 알고리즘 테스트와 재현용 입력 |
| `backend/experiments/` | 모델 비교 스크립트와 실행 방법 |
| `frontend/` | React/Vite 화면 |
| `data/examples/`, `docs/` | 공개 문항 예제와 팀 개발 문서 |
