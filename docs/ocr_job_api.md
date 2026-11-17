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
