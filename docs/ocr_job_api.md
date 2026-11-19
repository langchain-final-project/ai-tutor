# 사진 업로드 후 OCR 작업 시작·상태 확인·스트리밍

현재 사진 API는 로컬 개발용 고정 계정과 토큰을 사용합니다.
작업은 한 프로세스의 작업 스레드 하나에서 순서대로 실행합니다.

## 실행

저장소 최상위 폴더의 PowerShell에서 실행합니다.
기존 API가 실행 중이면 `Ctrl+C`로 종료한 뒤 다시 실행합니다.

```powershell
$env:DIDGO_API_TOKEN = 'local-test-token'
$env:DIDGO_DEV_USER_ID = 'local-student'
$env:DIDGO_QWEN_URL = 'http://127.0.0.1:8081/v1'
$env:DIDGO_OCR_TIMEOUT = '300'

.\.venv\api312\Scripts\python.exe -B -m uvicorn app.submission_api:app --app-dir backend --host 127.0.0.1 --port 8001 --workers 1
```

`DIDGO_QWEN_URL`은 실제 실행 중인 llama.cpp 서버 주소에 맞춥니다.
`8001`은 사진 API의 포트이며 Qwen 모델 서버의 포트와는 별개입니다.
모델 서버를 실행하는 명령은 모델 파일 경로와 실행 환경에 따라 다릅니다.
모델 서버가 없으면 OCR 작업은 `failed`가 되고 실패 이력을 남깁니다.

### 사진 품질 검사와 재촬영

사진 업로드 시 기본 품질 지표와 Qwen의 문제 내용 가시성 검사를 실행합니다.
선명도는 긴 변을 최대 1280픽셀로 축소한 사본에서 측정하며,
기존 해상도·밝기 조건을 만족할 때 Laplacian 분산이 30 미만이면 흐림으로 안내합니다.
이 기준은 같은 문제의 사진 5장으로 검증한 초기 기준입니다.

- `possibly_blurred`: 흐림으로 판독이 어려울 수 있어 재촬영이 필요합니다.
- `possibly_occluded`: 손이나 물건이 본문·수식·선택지·필요한 그림을 가립니다.
- `possibly_cropped`: 문제 내용이 사진 경계에서 잘려 있습니다.
- 여백만 가리거나 종이 테두리가 보이지 않는 경우에는 재촬영을 요구하지 않습니다.

위 세 경고 중 하나라도 감지되면 `POST /submissions/photo`는 `422`를 반환합니다.
`detail.code`는 `photo_retake_required`이며 `detail.photo_quality`에 사유와
`retake_required: true`가 포함됩니다. 해당 사진과 제출 기록을 저장하지 않고
제출 ID도 발급하지 않으므로 OCR을 시작할 수 없습니다.
화면은 사진과 사유를 유지하고 재촬영 또는 다른 사진 선택을 제공합니다.
새 사진을 올리면 다시 검사하며, 통과한 경우 범위 선택으로 진행합니다.

검사 서버 연결 실패나 잘못된 응답은 `quality_check_unavailable` 경고로 구분합니다.
이 경우 품질 불량으로 확정하지 않고 `retake_required: false`를 반환하며,
사용자가 사진을 직접 확인한 뒤 계속할 수 있습니다.
수락된 사진의 응답은 기존처럼 `201`이며 `photo_quality`를 포함합니다.

### 사진 방향 자동 보정

사진 API는 EXIF 보정 후 `PP-LCNet_x1_0_doc_ori`로 90도 단위 방향을 판별하고,
보정한 사진을 Qwen에 전달합니다. 실행 환경을 처음 준비할 때 설치합니다.

```powershell
.\.venv\api312\Scripts\python.exe -m pip install -r backend/requirements-orientation.txt
.\.venv\api312\Scripts\python.exe -B backend/scripts/setup_orientation_model.py
```

모델은 `.venv/ocr-models/PP-LCNet_x1_0_doc_ori`에 저장하며, 공식 리비전과
파일 해시를 검사합니다. API 프로세스에서 한 번 로딩한 모델을 재사용합니다.
요청 처리 중에 모델을 자동 다운로드하지 않습니다. 설치 후 API를 재시작합니다.

네 방향의 분류 결과를 원래 좌표로 환산해 모두 일치하고, 평균 점수 0.8 이상,
차순위와의 점수 차이 0.25 이상일 때만 자동 회전합니다. 이는 초기 운용 규칙이며
실제 촬영 문제에 대한 정확도 확률로 해석하면 안 됩니다. 작은 수식 조각·저대비 사진,
불확실한 결과·모델 미설치에는 자동 회전을 적용하지 않고 확인 필요 정보를 남깁니다.
미세한 기울기, 원근 왜곡, 도형·표 영역 분리는 이 기능의 대상이 아닙니다.

`GET /submissions/{id}`의 `image_rotation_ccw`, `image_orientation`으로 결과를
확인합니다. OCR 실행 이력에도 판별 근거와 적용 각도가 남습니다.
학생 수정·확인 후에도 최초 OCR 결과의 방향 정보를 유지합니다.

- `GET /submissions/{id}/photo`: 업로드한 원본 바이트
- `GET /submissions/{id}/photo?corrected=true`: EXIF와 OCR 회전을 적용한 PNG 표시본

원본 파일은 덮어쓰지 않습니다. 수동 회전이나 명시적인 영역 좌표가 지정된
`OCRPipeline` 호출에서는 자동 방향 판별을 생략해 좌표가 바뀌는 것을 방지합니다.

`004`, `005` 마이그레이션을 적용한 개인 작업용 DB를 사용합니다.
부분 영역 OCR을 사용할 때는 `006`도 적용합니다. 화면 테스트 서버
`backend/scripts/run_review_api.py`는 시작 시 기존 기록을 유지하면서 자동 적용합니다.
다른 작업용 DB는 다음 명령으로 준비합니다.

```powershell
.\.venv\api312\Scripts\python.exe -B backend/scripts/setup_problem_ocr_regions.py
```

### 선택 영역 OCR과 저장

`POST /submissions/{id}/ocr`에 다음 본문을 전달하면 선택 영역만 인식합니다.
본문을 생략한 기존 호출은 사진 전체를 인식합니다.

```json
{
  "region": {
    "coordinate_space": "exif_transposed",
    "image_size": [3000, 4000],
    "bbox": [300, 800, 1800, 2400]
  }
}
```

`bbox`는 EXIF 방향을 반영한 원본 사진의 `[왼쪽, 위, 오른쪽, 아래]` 정수
픽셀 좌표이며 오른쪽·아래 경계는 포함하지 않습니다. 사진 크기 불일치,
경계 밖 좌표, 빈 영역은 작업 생성 전에 `422`로 거절합니다.

선택 영역은 작업 생성과 함께 `problem_ocr_jobs.selected_region_json`에 저장하고
변경을 금지합니다. 같은 영역의 중복 요청은 실행 중인 작업을 반환하고,
다른 영역의 요청은 `409`로 거절합니다. 대기 작업도 서버 재시작 후 같은 범위로
재개합니다. 영역을 먼저 자른 뒤 해당 문제에 자동 방향 보정과 OCR을 적용하므로
영역 밖의 다른 문제가 모델 입력에 포함되지 않습니다.

최종 OCR 원문·결과에는 서버가 검증한 `selected_region`이 함께 저장됩니다.
`GET /submissions/{id}`에서 수정·확인 이후에도 같은 좌표를 조회할 수 있습니다.

- `GET /submissions/{id}/photo`: 원본 업로드 바이트
- `GET /submissions/{id}/photo?selected=true`: 선택한 범위의 PNG
- `GET /submissions/{id}/photo?selected=true&corrected=true`: 선택한 범위에 OCR 회전을 반영한 PNG

프런트엔드는 마지막 표시본을 사용해 완료 직후와 새로고침 후 같은 사진 범위를
복원합니다. 원본 사진은 덮어쓰지 않으며, 영역은 원문 OCR 결과를 통해 입력
수정 버전과 연결됩니다. 학생 검토가 시작된 제출의 OCR 범위는 변경할 수 없습니다.

### 영역 검출 결과 저장·조회

API의 `qwen-api-v4-layout` 파이프라인은 선택 범위를 자르고 방향을 보정한 뒤
`PP-DocLayout_plus-L`로 영역 후보를 검출합니다. 검출기와 모델은 API 프로세스에서
재사용하며, 기본 모델 경로는 `.venv/layout-model-cache/official_models/PP-DocLayout_plus-L`입니다.
`DIDGO_LAYOUT_MODEL_DIR`로 별도 설치 경로를 지정할 수 있습니다. 모델은 사전에
준비해야 하며 요청 처리 중 자동 다운로드하지 않습니다.

검출 결과는 `layout_detection`으로 OCR 원문의 `raw_response_json`에 함께 저장됩니다.
`GET /submissions/{id}`와 OCR 실행 이력에서 조회할 수 있고, 학생 수정·확인 뒤에도
원래 OCR 결과를 참조해 같은 분석 결과를 복원합니다. 기존 저장 구조를 사용하므로
영역 분석용 추가 DB 마이그레이션은 필요하지 않습니다. 이전 OCR 기록은
`layout_detection: null`로 조회됩니다.

- `status`: `detected`, `not_detected`, `unavailable`
- `layout`: `ProblemLayout`. 좌표는 선택 영역 자르기·방향 보정 후 이미지 기준입니다.
- `report`: 원본 검출 후보, 병합 이력, 처리 규칙의 한계

분석 실패·잘못된 좌표·저장할 수 없는 부가 데이터가 있으면 `unavailable`로 기록하고
기존 Qwen OCR은 계속 진행합니다. 검출 후보는 아직 주 OCR 입력을 나누거나 본문을
대체하는 데 사용하지 않습니다. `stage: layout`과 `layout_complete` 이벤트로
부가 분석의 진행 상태를 확인할 수 있습니다. 화면의 영역 박스 표시는 별도 연결 단계입니다.

서버 시작 시 작업 관리자가 시작되고, 종료 시 진행 중인 작업을 기다린 뒤 정리합니다.
재시작하면 대기 중이던 작업은 재개하고 중단됐던 실행 작업은 실패로 기록합니다.
같은 DB를 사용하는 이 API는 `--workers 1`로 실행합니다.

## 직접 확인

1. `http://127.0.0.1:8001/docs`에 접속합니다.
2. **Authorize**에 `local-test-token`을 입력합니다.
3. `POST /submissions/photo`로 문제 사진 한 장을 올리고 응답의 `id`를 확인합니다.
4. **OCR 작업**의 `POST /submissions/{submission_id}/ocr`에 그 번호를 넣고 실행합니다.
5. 응답 코드 `202`와 작업 `id`를 확인합니다.
6. `GET /ocr-jobs/{job_id}`에 작업 `id`를 넣고 조회합니다.
7. 완료되면 `GET /submissions/{submission_id}`로 원문과 확인 대기 상태를 확인합니다.

작업 상태는 `queued` → `running` → `completed` 또는 `failed`입니다.
빠르게 끝나면 작업 시작 응답에서 이미 완료·실패 상태가 보일 수도 있습니다.
`completed`는 OCR과 입력 버전 1 저장 완료이며, 학생 확인 완료를 뜻하지 않습니다.

## 수정·확인

OCR이 완료된 제출의 `latest_input.version_number`를 확인합니다.
`POST /submissions/{submission_id}/confirm`에 다음 형식으로 요청합니다.
`content_text`에는 실제 확인·수정한 문제 내용을 넣습니다.

```json
{
  "content_text": "-3x = 9",
  "expected_version_number": 1
}
```

응답의 `review_status`가 `confirmed`인지 확인합니다.
수정 내용만 저장하려면 `PATCH /submissions/{submission_id}/input`을 사용합니다.

### 확인 완료 후 내부 유형 분류

`POST /submissions/{submission_id}/confirm`은 학생 수정본과 확인 완료 상태를
먼저 저장하고, 저장된 최신 입력 버전으로 유형을 자동 분류합니다.
현재 대상은 중1 1학기 3단원 문자와 식입니다.
사용자 화면에는 유형 검색·선택 버튼을 표시하지 않습니다.

- 동일 문항이 없어도 기존 검색·수식 규칙으로 찾은 1순위 유형을 내부 참고값으로 선택합니다.
- 응답과 제출 재조회에 `type_match`가 포함됩니다. 결과는 입력 버전 ID와 본문 해시에 연결됩니다.
- `purpose: tutoring_reference`는 단계별 안내를 위한 참고 결과임을 나타냅니다.
- 동일 본문 매칭 조건을 충족하면 `method: exact_match`, `is_provisional: false`입니다.
- 유사 유형을 선택한 경우 `matched_question_id: null`, `is_provisional: true`입니다.
  후보의 유사도는 정답 확률이 아니며, 풀이 대상은 학생이 확인한 문제입니다.
- 입력 부족·판독 불가·미지원 결과에는 유형을 임의로 만들지 않습니다.
- 분류 실행에 실패하면 `type_match.error_code`가 `internal_type_classification_failed`이고
  유형 상태는 `needs_review`입니다. 학생 수정본과 `review_status: confirmed`는 보존됩니다.
- 수정본이 새 버전으로 바뀌면 이전 유형을 사용하지 않으며, 다시 확인 완료할 때 새로 분류합니다.

`POST /submissions/{submission_id}/type-match`는 내부 재실행용으로 유지합니다.
확인 완료한 최신 `expected_version_number`와 인증이 필요하며, 같은 문제은행·분류 정책의
결과는 재사용합니다. 실패한 분류는 이 API로 다시 실행할 수 있습니다.
기존 수동 선택 API인 `/submissions/{submission_id}/type-match/confirm`은 제거했습니다.

## OCR 스트리밍

`GET /ocr-jobs/{job_id}/events`는 `text/event-stream` 형식의 SSE 응답입니다.
작업을 시작한 뒤 반환된 작업 ID를 사용합니다. 문제 제출 번호와 작업 ID는 서로 다릅니다.
인증은 다른 API와 동일하게 `Authorization: Bearer ...` 헤더를 사용합니다.

PowerShell의 별도 터미널에서 확인할 수 있습니다.

```powershell
$jobId = '작업 시작 응답의 id'
curl.exe -N -H "Authorization: Bearer local-test-token" "http://127.0.0.1:8001/ocr-jobs/$jobId/events"
```

이벤트는 다음 형식으로 전달됩니다. `delta`의 내용은 확인 전 임시 출력입니다.

```text
id: 12
event: delta
data: {"type":"delta","text":"3x"}

```

- `queued`, `running`: 작업 대기·실행 시작
- `stage`, `region_start`, `region_complete`: 모델·영역 처리 진행
- `delta`: 인식 중인 임시 글자
- `completed`: OCR 원문과 입력 버전 1이 DB에 저장됨. 문제 조회 API로 최종 내용을 가져옴
- `failed`: 작업 실패. 작업 상태와 OCR 이력으로 원인 확인
- `stream_error`: 전송을 위한 DB 조회 오류. OCR 실패와 구분하며 마지막 이벤트 번호로 재연결

저장된 과거 이벤트를 먼저 전송하고 새 이벤트를 기다립니다.
완료·실패하면 남은 이벤트를 모두 보낸 뒤 연결을 종료합니다.
화면은 `completed` 또는 `failed`를 받으면 재연결을 멈춰야 합니다.
학생의 편집·확인은 `completed` 이후 최종 내용을 조회한 다음 시작합니다.

연결이 끊겨도 OCR 작업은 계속 진행합니다. 마지막으로 처리한 `id`가 12라면:

```powershell
curl.exe -N -H "Authorization: Bearer local-test-token" -H "Last-Event-ID: 12" "http://127.0.0.1:8001/ocr-jobs/$jobId/events"
```

또는 `.../events?after_id=12`로 요청합니다. 두 값이 함께 있으면 `Last-Event-ID`를 사용합니다.
그 번호를 초과한 이벤트만 재생하며, 재연결 자체는 OCR 작업을 새로 만들지 않습니다.
작업이 조용한 동안에는 10초마다 `: keep-alive` 주석을 보냅니다.
이미 완료된 작업도 저장된 이벤트를 재생해서 확인할 수 있습니다.

브라우저 화면에서는 Bearer 헤더를 전달할 수 있는 `fetch` 스트림을 사용합니다.
기본 `EventSource` 생성자는 임의의 Authorization 헤더를 지원하지 않습니다.

## 중복·실패·권한

- 대기·실행 중인 같은 제출을 다시 시작하면 기존 작업 ID를 반환합니다.
- 실패한 작업은 같은 제출로 다시 요청할 수 있습니다.
- 입력 버전 1이 만들어진 뒤 새 OCR을 요청하면 `409`로 거부합니다.
- 토큰이 없거나 다르면 `401`, 다른 계정의 제출·작업은 `404`로 응답합니다.
- 실패 상세 내용은 `GET /submissions/{submission_id}/ocr/history`에서도 확인할 수 있습니다.

## 자동 검증

```powershell
.\.venv\api312\Scripts\python.exe -B -m pytest backend/tests/test_ocr_job_api.py backend/tests/test_ocr_stream_api.py -q -p no:cacheprovider
```

테스트는 임시 DB와 테스트용 OCR을 사용하며 개인 DB와 실제 모델을 사용하지 않습니다.
테스트는 완료 전의 실시간 전송, 200개를 넘는 이벤트 재생, 끊김·재연결,
권한 검사, 실패 종료, heartbeat를 포함합니다. 화면 연결은 이후 단계입니다.

실행 중인 Qwen 서버까지 포함해서 검사하려면:

```powershell
.\.venv\api312\Scripts\python.exe -B backend/scripts/check_live_ocr_stream.py --qwen-url http://127.0.0.1:8081/v1
```

이 스크립트는 메모리에서 만든 검증용 수식 사진과 별도 임시 DB를 사용합니다.
실제 모델의 delta 이벤트·최종 저장·재연결 재생을 확인하고 임시 DB를 삭제합니다.
개인 DB를 변경하지 않으며, 본인 사진으로 검사하려면 `--image '사진 경로'`를 추가합니다.
모델 서버가 꺼져 있으면 다음처럼 로컬 파일을 지정할 수도 있습니다.

```powershell
.\.venv\api312\Scripts\python.exe -B backend/scripts/check_live_ocr_stream.py --qwen-model '모델.gguf 경로' --qwen-mmproj 'mmproj.gguf 경로'
```

이 방식은 검증 중에만 별도 임시 llama.cpp 서버를 실행하고, 검증 후 종료합니다.
