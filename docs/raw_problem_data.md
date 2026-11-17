# 원본 문제 이미지와 DB 연결: 중1부터

기존 API, `problem_search.py`, 공유 DB와 개인 작업 DB를 수정하지 않고 별도의 원천 연결 DB를 만듭니다.
기본 대상은 중1 전체이며, 원본 ZIP은 현재 위치에서 읽습니다. 이미지를 대량 복사하거나 ZIP을 다시 풀지 않습니다.

## 1. 연결과 학습/평가 분리

저장소 루트에서 실행합니다. 이 단계는 Python 표준 라이브러리만 사용합니다.

```powershell
python -B backend/scripts/prepare_raw_problem_data.py
```

기본 원천 폴더는 `수학 과목 문제생성 데이터_중등/`입니다. 다른 위치라면 `--dataset-root`를 지정합니다.

다음 파일을 `data/problem_bank/raw/`에 생성합니다. 생성물은 해당 폴더의 Git 제외 규칙으로 제외됩니다.

| 파일 | 내용 |
| --- | --- |
| `sources.sqlite3` | 원본 ZIP 경로·멤버·해시·이미지 크기와 문항/라벨 연결 |
| `train.jsonl` | 공식 Training에서 공통 OCR 50문항 및 같은 숫자 마스킹 본문 틀을 제외한 분류 입력 |
| `validation.jsonl` | 공식 Validation을 그대로 보존한 별도 평가 입력 |
| `topic_catalog.json` | 단원·유형·영역·인지 수준·응용 여부 목록 |
| `report.json` | 전체 연결·분리·영역 좌표 검증 결과 |

각 JSONL은 `input`(문제 본문·제공된 그림 설명)과 `label`(유형·영역 등)을 분리합니다.
모델 입력 파일에는 정답, 풀이 해설, 답안 이미지가 들어가지 않습니다.
학습에는 `label.source_topic_code`를 정답 라벨로만 사용하고 유형명·스킬·영역 정답을 입력에 섞지 않습니다.

공식 Validation을 학습으로 합치지 않습니다. 같은 본문 또는 같은 입력이 Training에 있는 경우를 별도 기록합니다.
동일 본문이라는 사실만으로 그림까지 같은 실제 중복 문항이라고 단정하지 않습니다.
수치 마스킹 틀은 보수적인 중복 방지용이며 모든 유사 문항을 찾아낸다는 보장은 없습니다.

다시 검증할 때:

```powershell
python -B backend/scripts/prepare_raw_problem_data.py --verify
```

모든 연결 이미지의 바이트·SHA-256·PNG 크기와 ZIP 체크섬을 다시 검사합니다.
기존 생성물을 덮어쓰지 않으므로 재생성할 때는 다른 폴더의 `--database`를 지정합니다.
중2·중3도 포함하려면 예를 들어 다음을 실행합니다.

```powershell
python -B backend/scripts/prepare_raw_problem_data.py --grades 1 2 3 --database data/problem_bank/raw_all/sources.sqlite3
```

## 2. 문항과 이미지 조회

```powershell
python -B backend/scripts/read_raw_problem.py --id M1_1_03_00041_45918.png --list-regions
```

내부 코드에서는 `app.db.raw_problem_data.RawProblemStore`의 `get_problem()`, `read_image()`, `get_regions()`를 사용합니다.
`read_image(id, kind="question")`은 ZIP에서 직접 원본 PNG를 읽고 등록한 해시를 검사합니다.
`kind="answer"`로 답안도 조회할 수 있으나 분류/OCR 입력에 답안 이미지를 넣지 않습니다.
원본 폴더를 옮겼다면 조회 시에도 같은 `--dataset-root`를 지정합니다.

공식 Validation 조회는 `--split Validation`을 추가합니다.

## 3. 좌표로 영역 자르기

실제 자르기와 모델 평가에는 선택 의존성을 설치합니다.

```powershell
python -m pip install -r backend/requirements-data-tools.txt
```

`--list-regions`에서 `block_index`, `region_index`, `region_type`, `bbox`, `text`를 확인한 뒤 선택한 영역을 저장합니다.

```powershell
python -B backend/scripts/read_raw_problem.py --id M1_1_02_00041_62188.png --region 6 --output data/problem_bank/raw/my-figure.png
```

실제 인덱스는 해당 원본 라벨의 조회 결과를 기준으로 선택합니다. `--kind answer`로 답안 영역도 자를 수 있습니다.
소수 좌표는 바깥쪽 정수로 확장하고 이미지 경계를 넘는 부분은 잘라내며, 적용 좌표와 `clamped`를 반환합니다.
기존 출력 PNG는 덮어쓰지 않습니다.

`question_regions`와 `answer_regions` 뷰에 줄·문단·그림·정답 영역을 보존합니다.
줄 좌표에 텍스트가 없는 경우가 많으므로, 줄 인식 학습용 정답으로 임의 생성하지 않습니다.
그림 설명은 문항 전체에 대한 설명이며 여러 그림 중 어느 영역에 대응하는지 자동 확정하지 않습니다.

## 4. 추가 메타데이터

`raw_problems`에 `sector1`, `sector2`, `question_step`, 원본 `question_info` JSON을 보관합니다.
`raw_topic_catalog`는 공식 split별 유형과 메타데이터를 조회합니다.
Validation에만 있는 유형도 보존하며 현재 문제은행의 기존 유형으로 강제로 바꾸지 않습니다.
영역·인지 수준은 추천용 정보 또는 보조 학습 목표로 쓸 수 있지만 사용자 문제에 이미 주어진 정답 정보로 취급하지 않습니다.

## 5. 공식 Validation으로 고정 분류기 평가

```powershell
python -B backend/experiments/evaluate_raw_classifier.py
```

기존 실험에서 정한 TF-IDF + LinearSVC 설정을 고정하고, 공식 Validation으로 하이퍼파라미터를 조정하지 않습니다.
본문만 사용한 경우와 제공된 그림 설명을 더한 경우를 각각 기록합니다.
전체 결과, 동일 입력 제외 결과, 숫자 마스킹 틀의 근접도 0.95 이상인 문항도 제외한 결과를 함께 제공합니다.
근접도 필터는 유사 문항 점검을 위한 근사 기준이지 새 문제임을 보장하는 기준은 아닙니다.
미학습 유형은 별도로 명시하며 정답률 계산에서 숨겨 제외하지 않습니다.

이 평가는 **제공된 텍스트/그림 설명을 입력으로 한 유형 분류**입니다. 사진 OCR이나 손글씨 인식 성능이 아닙니다.
평가 결과는 `raw/evaluation_v1/`에 저장하며 재실행 시 다른 `--output-dir`를 지정합니다.
해당 Validation을 보고 반복적으로 모델을 선택하면 별도 최종 테스트셋이 필요합니다.

## 범위

현재 작업은 원본 연결, 평가 분리, 영역 추출, 메타데이터 보존 및 고정 분류기 평가입니다.
실제 사진 OCR 모델 실행, 학생 손글씨 오류 진단, API 연결, Git 커밋·푸시는 포함하지 않습니다.
`SbL.zip`의 독립 추가 문항이나 전체 초·중·고 메타데이터를 자동으로 학습 데이터에 합치지 않습니다.

## 이번 실행에서 확인한 결과

중1 Training 10,674문항과 공식 Validation 1,152문항을 연결했고,
문제·답안 이미지 23,652개를 모두 읽어 해시와 PNG 크기를 확인했습니다.
학습용 내보내기는 공통 50문항과 같은 숫자 마스킹 틀을 제외한 10,540문항입니다.
공식 Validation에는 Training에 없는 유형 `7101043`이 있어 미학습 유형으로 기록했습니다.

| 입력 | 공식 Validation 1순위 | 상위 3개 | 상위 5개 |
| --- | ---: | ---: | ---: |
| 본문 | 72.9% | 87.1% | 91.2% |
| 본문 + 제공된 그림 설명 | 75.3% | 89.7% | 93.5% |

본문+그림 설명 평가에서 근접한 숫자 마스킹 틀도 제외한 1,084문항의 1순위 정답률은 75.3%였습니다.
이전 임시 그룹 분할 실험과 학습/평가 문항이 다르므로 이전 수치와 직접적인 개선 폭으로 비교하지 않습니다.
실제 OCR을 거친 텍스트·그림 설명에서도 같은 결과가 나오는지는 후속 검증이 필요합니다.

`raw/previews/`에 문제 문단, 수직선 그림, 답안 문단의 실제 자르기 샘플과 적용 좌표를 저장했습니다.
