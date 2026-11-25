# 팀 공통 문제은행 DB — pull 후 사용

**데이터가 이미 들어 있는 SQLite DB를 Git으로 공유합니다.**
DB 서버 설치, ZIP 압축 해제, JSON 재적재 없이 사용할 수 있습니다.
이 문서는 이전 DB 구축 문서의 재생성 방식에 더해, 완성된 DB를 받는 최종 사용 방법을 설명합니다.

## 1. 포함되는 완성 DB

`data/problem_bank/problem_bank.seed.sqlite3` (약 45.8 MB)

- 전체 문제 30,339개, 단원 24개, 수학 유형 1,165개
- 공통 비교 50문항, 문제·답안 이미지 100개, 선정 순서와 평가 데이터 해시
- 업로드 사진·OCR 결과·유형 분류 결과·후보 유형 저장 테이블 (처음에는 비어 있음)

이 파일은 일반 SQLite DB이므로 pull 후 바로 열어 SQL로 조회할 수 있습니다.
`data/problem_bank/.gitignore`에서 이 파일 하나만 상위 제외 규칙의 예외로 지정했고,
`.gitattributes`로 바이너리 취급을 지정했습니다. Git LFS나 추가 다운로드는 사용하지 않습니다.
파일 SHA-256과 기대 문항 수는 같은 폴더의 `problem_bank.seed.json`에 기록했습니다.

## 2. 로컬 개발: pull 후 한 번 실행

Python 3.12 이상에서 저장소 루트를 기준으로 실행합니다. 추가 패키지는 필요 없습니다.

```powershell
git pull
python -B backend/scripts/prepare_problem_bank.py
```

1. 공유 DB 파일의 크기·SHA-256·무결성·테이블별 문항 수를 검사합니다.
2. 공유 DB를 개인 작업용 `data/problem_bank/didgo.sqlite3`로 복사합니다.
3. 기존 작업용 DB가 있으면 내용을 보존하고 호환 여부만 검증합니다.

사진/OCR/분류 결과는 **개인 작업용 DB에만** 저장합니다.
개인 DB와 SQLite 저널 파일은 기존 Git 제외 규칙에 따라 Git에 올라가지 않습니다.
이전 API의 health 확인만으로 만들어진 빈 DB는 자동으로 초기화합니다.
다른 데이터가 있는 호환되지 않는 DB는 덮어쓰지 않고 오류를 반환합니다.
새 문제은행 버전 배포 시에도 기존 개인 데이터가 자동 덮어써지지는 않습니다.

다른 경로에 개인 DB를 준비할 수도 있습니다.

```powershell
python -B backend/scripts/prepare_problem_bank.py --database data/problem_bank/my-work.sqlite3
```

로컬에서 기존 FastAPI를 실행할 때는 출력된 `database_url`을 `DATABASE_URL`로 지정합니다.
예를 들어 저장소 루트에서 실행한다면 다음과 같습니다. API 실행 의존성은 기존 백엔드 환경을 사용합니다.

```powershell
$env:DATABASE_URL = 'sqlite:///data/problem_bank/didgo.sqlite3'
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

기존 API는 `/health`만 제공합니다. 이 설정은 실제 문제은행 DB에 연결하며,
문항 검색·업로드·OCR·유형 분류 API 자체는 이후 구현할 부분입니다.

## 3. Docker 개발: 초기 복사와 DB 연결을 한 명령으로

기존 파일을 수정하지 않고 새 Compose 설정을 함께 사용합니다.
실행 중인 기존 기본 API가 있다면 먼저 종료합니다. 볼륨 삭제 옵션은 사용하지 않습니다.

```powershell
docker compose down
docker compose -f compose.yaml -f compose.problem-bank.yaml up --build --wait
```

초기화 서비스가 공유 DB를 읽기 전용으로 마운트하고,
기존 `sqlite_data` 볼륨의 `/var/lib/didgo/dev.db`에 준비한 뒤 API가 실행됩니다.
그 결과 기존 API의 `DATABASE_URL`이 실제 문항이 들어 있는 DB를 가리킵니다.
재실행해도 기존 사진·OCR·분류 데이터는 보존합니다.
호환되지 않는 기존 DB는 API 실행 전에 오류로 중단합니다.

```powershell
curl.exe --fail http://localhost:8000/health
docker compose -f compose.yaml -f compose.problem-bank.yaml down
```

일반 `docker compose up`만 사용하면 새 초기화 서비스가 실행되지 않습니다.
위 명령은 SQLite용이며 `compose.postgres.yaml`과 함께 사용하지 않습니다.
현재 작업 환경에는 Docker 실행 파일이 없어 실제 컨테이너 실행 검증은 하지 못했습니다.
초기화 Python 스크립트는 별도의 새 폴더 및 빈 DB에서 검증했습니다.

## 4. 바로 조회할 수 있는 SQL

```sql
SELECT COUNT(*) FROM questions; -- 30339
SELECT COUNT(*) FROM topics;    -- 1165

SELECT grade, semester, unit_name, source_topic_code, source_topic_name, question_count
FROM problem_topic_catalog
ORDER BY grade, semester, unit_code, source_topic_code;

SELECT position, id, question_text, source_topic_code, source_topic_name
FROM ocr50_questions
ORDER BY position;
```

SQLite에 데이터를 쓰는 서비스는 연결마다 `PRAGMA foreign_keys = ON`을 실행합니다.

## 5. 검증과 재생성

공유 DB만 검증 (원본 ZIP 불필요):

```powershell
python -B backend/scripts/prepare_problem_bank.py --verify-seed
```

실제 공유 DB와 새 폴더에서의 초기화, 기존 개인 데이터 보존을 검사:

```powershell
python -B -m unittest discover -s backend/tests -p test_problem_distribution_db.py -v
```

원본 ZIP부터 내용을 재검증하려면 기존 검증 스크립트를 사용합니다.

```powershell
python -B backend/scripts/setup_problem_classification.py --database data/problem_bank/problem_bank.seed.sqlite3 --verify
```

기본 DB 구조: `docs/problem_bank_db.md`

사진·OCR·분류 데이터 연결: `docs/problem_classification_db.md`

## 6. Git 공유 대상

완성 DB만 복사하는 새 사용 경로는 원본 ZIP에 의존하지 않습니다.
이 작업에서 추가한 파일 전체를 함께 커밋하면 조회·개발용 복사·재생성·테스트가 모두 가능합니다.
특히 다음 파일들이 빠지지 않아야 합니다.

- `data/problem_bank/.gitignore`, `.gitattributes`
- `data/problem_bank/problem_bank.seed.sqlite3`, `problem_bank.seed.json`
- `backend/scripts/prepare_problem_bank.py`, `compose.problem-bank.yaml`, 이 문서
- 기존에 추가한 문제은행/분류 SQL·스크립트·테스트·문서와 원본 ZIP 사본

현재 작업은 공유 파일 준비까지이며 Git 커밋·푸시는 실행하지 않았습니다.
이 파일들이 원격 저장소에 커밋·푸시된 후 팀원이 pull하면 위 방식으로 사용할 수 있습니다.
