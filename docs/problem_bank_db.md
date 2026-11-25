# 딛고 문제은행 DB 구축 및 조회

이 작업은 기존 API, DB 연결 코드, Compose 설정을 수정하지 않고 새 파일만 추가합니다.
기본 개발 환경에 맞춘 **SQLite 전용 DB**입니다. PostgreSQL 적용과 API 조회 기능은 별도 작업입니다.

## 1. 입력 데이터와 적재 결과

제공받은 ZIP 두 개를 수정하지 않고 다음 경로에 복사했습니다.

- `data/problem_bank/sources/problem_bank.zip`: `딛고_문제은행.zip` 원본 사본
- `data/problem_bank/sources/ocr50.zip`: `중1_OCR_비교_50문항.zip` 원본 사본
- `data/problem_bank/sources/manifest.json`: 두 ZIP의 SHA-256 및 파일 크기

| 구분 | 수량 |
| --- | ---: |
| 전체 문항 | 30,339 |
| 중1 / 중2 / 중3 | 10,674 / 10,236 / 9,429 |
| 단원 / 세부 유형 | 24 / 1,165 |
| 비교용 문항 | 50 (1학기 25, 2학기 25) |
| 비교용 문제 / 답안 이미지 | 50 / 50 |
| 비교용 50문항을 제외한 목록 | 30,289 |

문항 ID 중복이 없으며, 비교용 JSON 50개는 전체 문제은행의 해당 레코드와 모든 필드가 일치합니다.
선정목록 CSV의 행 순서를 평가 순서 `position` 1~50으로 저장했습니다.
중2 JSON이 ZIP의 `m1/` 폴더에 들어 있지만, 학년은 문항의 실제 `grade` 값으로 구분합니다.

원본 22개 필드를 보존합니다. 단원·유형·원본 ID의 앞자리 0, LaTeX 문자열, 배열 순서, `null`을 유지합니다.
`skill_tags`, `answer_box_texts`는 JSON 문자열로 저장하므로 Python에서 사용할 때 `json.loads()`로 복원합니다.
원본 정답이 없는 `M1_2_07_00041_86343.png`의 `answer`는 `NULL`로 남겼습니다.
원본 난이도는 **1=상, 2=중, 3=하**이며 검수 상태도 변경하지 않았습니다.
적재 검증은 원본과의 일치 여부를 검사하며 정답의 수학적 정확성을 추가 검수하지는 않습니다.

## 2. 스키마

스키마 파일: `backend/migrations/001_problem_bank.sqlite.sql`

| 테이블 / 뷰 | 역할 |
| --- | --- |
| `dataset_sources` | 원본 ZIP 두 개의 해시와 크기 |
| `units` | 학년·학기·단원 코드와 이름 |
| `topics` | 세부 유형 코드와 이름 |
| `questions` | 원본 22개 필드, ZIP 내부 출처 경로, 문항 내용 해시 |
| `benchmark_assets` | 비교용 PNG 100개의 실제 바이트와 해시 |
| `benchmark_sets` | 평가 세트 `ocr50-v1` 및 내용 해시 |
| `benchmark_items` | 평가 순서, 문항 ID, 선정 유형·이유, 이미지 연결, CSV 원본 열 |
| `ocr50_questions` (뷰) | 비교용 50문항과 문제은행 정보를 연결한 조회 결과 |
| `training_questions` (뷰) | 비교용 문항을 제외한 문제은행 조회 결과 |

문항·단원·유형·평가 이미지 관계에는 외래키를 적용했습니다.
학년·학기·단원, 세부 유형, 난이도, 대표 개념에는 조회 인덱스를 추가했습니다.
**DB를 직접 열어 데이터를 쓸 때는 연결마다 `PRAGMA foreign_keys = ON`을 실행해야 합니다.**

전체 문제은행 ZIP에는 이미지가 없습니다. 비교용 50문항만 이미지 바이트를 보관하며,
나머지 문항의 `question_filename`, `answer_filename`은 원천 이미지를 나중에 연결할 파일명입니다.
`training_questions` 뷰는 향후 학습 데이터 선택을 돕는 목록입니다. 실제 학습이나 모델 평가는 실행하지 않았습니다.

## 3. 같은 DB 재생성

저장소 루트에서 Python 3.12 이상으로 실행합니다. 외부 패키지나 Docker 설치가 필요하지 않습니다.

```powershell
python -B backend/scripts/build_problem_bank.py
```

기본 생성 위치는 `data/problem_bank/didgo.sqlite3`입니다.
스크립트는 다음 순서로 실행됩니다.

1. ZIP 크기와 SHA-256을 manifest와 비교합니다.
2. 24개 JSON의 필드·ID·단원·유형을 검증합니다.
3. 선정목록 순서대로 50문항의 원본 일치 여부와 이미지 100개를 확인합니다.
4. 새 DB에 스키마를 만들고 전체 문항과 평가 데이터를 한 트랜잭션으로 적재합니다.
5. 모든 문항의 22개 필드, 이미지 바이트, 선정목록, 외래키, DB 무결성을 원본과 대조하고 커밋합니다.

기존 DB는 덮어쓰지 않습니다. 새 결과가 필요하면 다른 경로를 지정합니다.

```powershell
python -B backend/scripts/build_problem_bank.py --database data/problem_bank/didgo-copy.sqlite3
```

기존 DB 전체를 읽기 전용으로 재검증할 수 있습니다.

```powershell
python -B backend/scripts/build_problem_bank.py --verify
```

검증 테스트:

```powershell
python -B -m unittest discover -s backend/tests -p test_problem_bank_db.py -v
```

## 4. 문항 조회

아래는 DB를 연 다음 실행할 SQL 예시입니다.

```sql
-- 중1 1학기 문자와 식 문항
SELECT id, question_text, answer, source_topic_code, source_topic_name
FROM questions
WHERE grade = 1 AND semester = 1 AND unit_code = '03'
ORDER BY id;

-- 모든 팀원이 같은 순서로 사용하는 50문항
SELECT position, id, recognition_type, question_text, answer,
       question_asset, answer_asset
FROM ocr50_questions
ORDER BY position;

-- 평가 문항과 겹치지 않는 목록
SELECT id, question_text, answer FROM training_questions ORDER BY id;

-- 이미지 바이트는 이 테이블에서 조회
SELECT filename, mime_type, content
FROM benchmark_assets
WHERE source_member = ?;
```

OCR 모델에는 `question_asset`에 연결된 문제 이미지 `content`만 입력하고,
`question_text`는 비교할 정답 텍스트로 사용합니다.
`answer`, `answer_text`, 답안 이미지는 입력에 포함하지 않습니다.
동일한 평가를 위해 세트 ID, 내용 해시, `position`을 함께 기록합니다.

이 버전의 평가 내용 해시:

```text
ocr50-v1
393e7c7c905074979c43012cb83e779fe2265cc12355bd950765c386cf027207
```

해시는 선정 순서·CSV 메타데이터·문항 22개 필드·문제/답안 이미지 해시를 포함합니다.
DB 파일 전체의 해시가 아니라 평가 내용의 해시입니다.

## 5. API 연결 및 Git 공유 범위

현재 생성한 DB는 독립 파일입니다. 기존 API의 기본 DB(`didgo.sqlite3`)나
Compose의 볼륨 DB(`/var/lib/didgo/dev.db`)에 자동 연결하지 않았습니다.
이 작업에는 API 코드 수정, PostgreSQL 서버 구축, Git 커밋·푸시가 포함되지 않습니다.

기존 `.gitignore`가 `*.sqlite3`을 제외하므로 DB 바이너리는 Git에 잡히지 않습니다.
팀에서는 새로 추가한 스키마, 스크립트, 테스트, 이 문서,
`data/problem_bank/sources/`의 ZIP 사본 2개와 manifest를 공유하면 동일한 DB를 재생성할 수 있습니다.
원본 ZIP 사본은 합계 약 7.3 MB입니다. 원본 위치의 파일은 수정하지 않았습니다.
